"""The two pages, both strictly read-only.

A request never reaches the network and never classifies a build; it reads what scripts/refresh.py
left behind. That is structural rather than a convention: the routes are handed
false_positive.cached_classifier, which has no History to ask and reports an unclassified build as
unclassified. Anything the refresh has not caught up with therefore shows as a gap on the page
instead of as a slow request.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from flask import Flask, Response, g, redirect, render_template, request, url_for

from ews_dashboard import config, db, results, suites
from ews_dashboard.analysis import convictions, escapes, false_positive, freshness, trend
from ews_dashboard.web import chart, formatting, links

DEFAULT_WINDOW_DAYS = 7
WINDOW_CHOICES = (7, 14, 30, 60, 90)
BUILDS_SHOWN = 200

# The freshness banner is dismissed by a request rather than in the browser, because this app ships
# no JavaScript and a dismissal has to hold on the next page too. The cookie holds the signature of
# what was dismissed, so a banner saying something new is shown again.
FRESHNESS_COOKIE = 'freshness_dismissed'
FRESHNESS_DISMISSAL_SECONDS = 30 * 86400

# How many days each point of the trend line averages over. The window drives the chart's span, so
# this is the only thing left to choose: a short average shows every spike, a long one shows whether
# the level moved.
ROLLING_CHOICES = (3, 7, 14, 28)

SUITE_CHOICES = tuple(suite.name for suite in suites.SUITES)

# A rule's own page lists deeply; the overview lists enough to see the worst offenders and says how
# many it is holding back, because three full lists put a thousand rows between a reader and the
# bottom of the page.
RULE_TESTS_PREVIEWED = 10
RULE_TESTS_LISTED = 500


class Classified:
    """Shared by both panes: the state a page shows for a build, which is not the same as its bucket.

    A build that showed its author no failures has no bucket and is nonetheless fully classified, so
    reporting a missing bucket as unclassified would send a reader to the refresh over a build there
    is nothing to refresh.
    """

    classification: Optional[false_positive.Classification]

    @property
    def state(self) -> Optional[str]:
        if self.classification is None:
            return None
        return self.classification.bucket or formatting.NO_SURFACED


@dataclass(frozen=True)
class BuildSummary(Classified):
    """One failing build as a row in the builds pane.

    Holds the row rather than copying it because the detail pane needs every column the classifier
    reads, and a page that listed a build must be able to open it without a second query.
    """

    build: sqlite3.Row
    classification: Optional[false_positive.Classification]

    @property
    def surfaced_total(self) -> Optional[int]:
        return self.classification.surfaced_total if self.classification else None


@dataclass(frozen=True)
class BuildFilter:
    """What the builds pane is narrowed to inside the window and the scope.

    An unknown state name and an unreadable bound are dropped rather than refused, because both
    arrive from a hand-edited URL as readily as from the form; a minimum above the maximum is left
    alone, so it narrows to nothing and says so.
    """

    states: tuple
    min_shown: Optional[int]
    max_shown: Optional[int]

    @property
    def narrowing(self) -> bool:
        return bool(self.states) or self.min_shown is not None or self.max_shown is not None

    def matches(self, summary: BuildSummary) -> bool:
        if self.states and (summary.state or formatting.UNCLASSIFIED) not in self.states:
            return False
        shown = summary.surfaced_total or 0
        if self.min_shown is not None and shown < self.min_shown:
            return False
        if self.max_shown is not None and shown > self.max_shown:
            return False
        return True


@dataclass(frozen=True)
class BuildDetail(Classified):
    """One build's own pane: what it showed its author, and why each test landed where it did."""

    build: sqlite3.Row
    configuration: results.Configuration
    classification: Optional[false_positive.Classification]
    undetermined_reason: Optional[str]
    tests: list
    matching: list


@dataclass(frozen=True)
class RuleSection:
    """One flakiness rule's convicted tests, and how deeply this page is listing them."""

    name: str
    description: str
    convicted: convictions.Convictions
    limit: int


@dataclass(frozen=True)
class Window:
    """The span a page counts over: the last `days` days, ending now.

    Rolling rather than anchored to UTC midnight, which made the shortest window mean "today so
    far" — an hour of EWS at 01:00 UTC, and a page that read as having no data at all. The trend
    chart is unaffected, since it buckets by UTC day over TREND_DAYS and never consults a Window.
    """

    days: int
    since: int
    until: int

    @classmethod
    def of_days(cls, days: int) -> 'Window':
        until = int(time.time())
        return cls(days=days, since=until - days * 86400, until=until)


def _chosen(name: str, choices: tuple, default: Optional[str] = None) -> Optional[str]:
    """A query argument restricted to a known set, so a hand-edited URL selects nothing unknown."""
    value = request.args.get(name)
    return value if value in choices else default


def _chosen_number(name: str, choices: tuple, default: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except ValueError:
        return default
    return value if value in choices else default


def _bound(name: str) -> Optional[int]:
    """One end of a range, absent when the field was left empty or holds something that is not a
    number."""
    try:
        return int(request.args[name])
    except (KeyError, ValueError):
        return None


def _build_filter() -> BuildFilter:
    return BuildFilter(
        states=tuple(value for value in request.args.getlist('state')
                     if value in formatting.STATE_CHOICES),
        min_shown=_bound('min_shown'),
        max_shown=_bound('max_shown'),
    )


def _window() -> Window:
    return Window.of_days(_chosen_number('days', WINDOW_CHOICES, DEFAULT_WINDOW_DAYS))


def create_app(database_path: Optional[str] = None) -> Flask:
    app = Flask(__name__)
    resolved_path = database_path or config.database_path()
    formatting.register(app)

    def open_database() -> sqlite3.Connection:
        if 'connection' not in g:
            g.connection = db.connect(resolved_path)
        return g.connection

    @app.teardown_appcontext
    def close_connection(exception: Optional[BaseException]) -> None:
        open_connection = g.pop('connection', None)
        if open_connection is not None:
            open_connection.close()

    @app.route('/')
    def landing() -> str:
        return render_template('landing.html', **_landing_context(open_database(), _window()))

    @app.route('/explore')
    def explore() -> str:
        return render_template('explore.html', **_explore_context(open_database(), _window()))

    @app.route('/escapes')
    def escaped_regressions() -> str:
        return render_template('escapes.html', **_escapes_context(open_database(), _window()))

    @app.route('/dismiss-freshness')
    def dismiss_freshness() -> Response:
        response = redirect(_internal_target(request.args.get('next')))
        response.set_cookie(FRESHNESS_COOKIE, request.args.get('state', ''),
                            max_age=FRESHNESS_DISMISSAL_SECONDS, samesite='Lax')
        return response

    return app


def _internal_target(target: Optional[str]) -> str:
    """Where a dismissal returns to. The target arrives in a query parameter, so anything that is not
    a path on this app sends the reader back to the overview rather than off the site."""
    if not target or not target.startswith('/') or target.startswith('//') or '\\' in target:
        return url_for('landing')
    return target


def _freshness_context(open_connection: sqlite3.Connection) -> dict:
    current = freshness.current(open_connection)
    return {
        'freshness': current,
        'freshness_dismissed': request.cookies.get(FRESHNESS_COOKIE) == current.signature,
    }


@dataclass(frozen=True)
class Scope:
    """What both pages narrow by, and the queues a reader can pick from.

    The queue list comes from the window, so `builder` is validated against the queues that have
    builds in it — a stale link to a queue EWS has since retired narrows nothing rather than
    emptying the page.
    """

    suite: Optional[str]
    builder: Optional[str]
    queue_activity: list


def _scope(open_connection: sqlite3.Connection, window: Window) -> Scope:
    suite = _chosen('suite', SUITE_CHOICES)
    activity = convictions.queue_activity(open_connection, window.since, window.until, suite=suite)
    return Scope(
        suite=suite,
        builder=_chosen('builder', tuple(queue.builder for queue in activity)),
        queue_activity=activity,
    )


def _landing_context(open_connection: sqlite3.Connection, window: Window) -> dict:
    scope = _scope(open_connection, window)
    rolling = _chosen_number('rolling', ROLLING_CHOICES, trend.ROLLING_DAYS)
    classifier = false_positive.cached_classifier(open_connection)
    points = trend.daily(open_connection, classifier, trend.today(), days=window.days,
                         rolling=rolling, suite=scope.suite, builder=scope.builder)
    return dict(
        window=window,
        window_choices=WINDOW_CHOICES,
        suite=scope.suite,
        suite_choices=SUITE_CHOICES,
        builder=scope.builder,
        queue_activity=scope.queue_activity,
        rolling=rolling,
        rolling_choices=ROLLING_CHOICES,
        counts=false_positive.rate(open_connection, classifier, window.since, window.until,
                                   suite=scope.suite, builder=scope.builder),
        by_rule=convictions.by_rule(open_connection, window.since, window.until,
                                    suite=scope.suite, builder=scope.builder),
        rule_descriptions=config.RULE_DESCRIPTIONS,
        builds_queried=convictions.builds_queried(open_connection, window.since, window.until,
                                                  suite=scope.suite, builder=scope.builder),
        query_failures=convictions.query_failures(open_connection, window.since, window.until,
                                                  suite=scope.suite, builder=scope.builder),
        chart=chart.of_trend(points, trend.deployments_within(points)),
        threshold_pct=config.PRE_EXISTING_THRESHOLD_PCT,
        links=links,
        **_freshness_context(open_connection),
    )


def _explore_context(open_connection: sqlite3.Connection, window: Window) -> dict:
    scope = _scope(open_connection, window)
    suite, builder = scope.suite, scope.builder
    test_filter = request.args.get('test') or None
    rule = _chosen('rule', config.FLAKINESS_RULES)
    build_filter = _build_filter()
    classifier = false_positive.cached_classifier(open_connection)
    builds, builds_matched = _filtered_builds(open_connection, window, suite, builder, classifier,
                                              build_filter)
    return dict(
        window=window,
        window_choices=WINDOW_CHOICES,
        builder=builder,
        suite=suite,
        suite_choices=SUITE_CHOICES,
        queue_activity=scope.queue_activity,
        builds=builds,
        builds_total=false_positive.failing_build_count(
            open_connection, window.since, window.until, suite=suite, builder=builder,
        ),
        builds_matched=builds_matched,
        build_filter=build_filter,
        state_choices=formatting.STATE_CHOICES,
        detail=_build_detail(open_connection, classifier, test_filter),
        test_filter=test_filter,
        verdict_descriptions=false_positive.VERDICT_DESCRIPTIONS,
        reason_descriptions=false_positive.REASON_DESCRIPTIONS,
        counts=false_positive.rate(
            open_connection, classifier, window.since, window.until, suite=suite, builder=builder,
        ),
        rules=_rule_sections(open_connection, window, suite, builder, rule),
        rule=rule,
        links=links,
        **_freshness_context(open_connection),
    )


def _filtered_builds(
    open_connection: sqlite3.Connection,
    window: Window,
    suite: Optional[str],
    builder: Optional[str],
    classifier: false_positive.Classifier,
    build_filter: BuildFilter,
) -> tuple:
    """The builds pane's page, and how many builds in the whole window the filter matched.

    A narrowed pane has to narrow before it takes its page: narrowing the newest `BUILDS_SHOWN`
    instead read as "0 of 13,934" over 90 days for a state that only older builds are in. Fetching
    the window unlimited to do it is affordable because the refresh has already classified every one
    of these builds, so the classifier reads its answers back rather than deciding them. An
    unnarrowed pane keeps the cheap page, since matching every build against a filter that matches
    everything would classify thousands of rows to change nothing.
    """
    if not build_filter.narrowing:
        rows = false_positive.failing_builds(
            open_connection, window.since, window.until,
            suite=suite, builder=builder, limit=BUILDS_SHOWN,
        )
        return _build_summaries(rows, classifier, build_filter), None
    rows = false_positive.failing_builds(
        open_connection, window.since, window.until, suite=suite, builder=builder,
    )
    matched = _build_summaries(rows, classifier, build_filter)
    return matched[:BUILDS_SHOWN], len(matched)


def _build_summaries(
    rows: list,
    classifier: false_positive.Classifier,
    build_filter: BuildFilter,
) -> list:
    """The builds pane's rows, narrowed here rather than in the template.

    Not narrowed in SQL: a row's state is the classification's bucket read through `Classified`,
    which turns a missing bucket into `no_surfaced` and a missing classification into unclassified,
    and none of those three are a column.
    """
    return [
        summary for summary in (BuildSummary(row, classifier(row)) for row in rows)
        if build_filter.matches(summary)
    ]


def _escapes_context(open_connection: sqlite3.Connection, window: Window) -> dict:
    """The escape page: what main did with each convicted test after the change landed.

    Read-only like the others. Deciding an escape needs results.webkit.org and a checkout, so this
    page shows what the refresh has already decided and says how much it could not.
    """
    scope = _scope(open_connection, window)
    verdict_shown = _chosen('verdict', escapes.VERDICTS, escapes.ESCAPED)
    drilled = escapes.convictions(open_connection, window.since, window.until, verdict_shown,
                                  suite=scope.suite, builder=scope.builder)
    return dict(
        window=window,
        window_choices=WINDOW_CHOICES,
        suite=scope.suite,
        suite_choices=SUITE_CHOICES,
        builder=scope.builder,
        queue_activity=scope.queue_activity,
        tally=escapes.tally(open_connection, window.since, window.until,
                            suite=scope.suite, builder=scope.builder),
        escaped_verdict=escapes.ESCAPED,
        convictions=drilled,
        verdict_shown=verdict_shown,
        sentence=escapes.sentence,
        verdict_descriptions=escapes.VERDICT_DESCRIPTIONS,
        verdicts=escapes.VERDICTS,
        window_days=config.ESCAPE_WINDOW_DAYS,
        failure_pct=config.ESCAPE_FAILURE_PCT,
        links=links,
        **_freshness_context(open_connection),
    )


def _rule_sections(
    open_connection: sqlite3.Connection,
    window: Window,
    suite: Optional[str],
    builder: Optional[str],
    rule: Optional[str],
) -> tuple:
    """The convicted-test tables this page shows: one rule listed deeply, or every rule previewed."""
    wanted = (rule,) if rule else config.FLAKINESS_RULES
    limit = RULE_TESTS_LISTED if rule else RULE_TESTS_PREVIEWED
    return tuple(
        RuleSection(
            name=each,
            description=config.RULE_DESCRIPTIONS[each],
            convicted=convictions.convicted_tests(
                open_connection, each, window.since, window.until,
                suite=suite, builder=builder, limit=limit,
            ),
            limit=limit,
        )
        for each in wanted
    )


def _build_detail(
    open_connection: sqlite3.Connection,
    classifier: false_positive.Classifier,
    test_filter: Optional[str],
) -> Optional[BuildDetail]:
    """The selected build, looked up by id rather than found in the listed page, so a link to a build
    outside the current window or filter still opens it.

    `tests` stays whole and `matching` carries the filter, so a filter that matches nothing reads as
    a filter that matched nothing rather than as a build that surfaced nothing.
    """
    try:
        build_id = int(request.args['build'])
    except (KeyError, ValueError):
        return None
    row = false_positive.failing_build(open_connection, build_id)
    if row is None:
        return None
    surfaced = false_positive.explain(open_connection, row)
    return BuildDetail(
        build=row,
        configuration=results.Configuration.of_build(row),
        classification=classifier(row),
        undetermined_reason=false_positive.undetermined_reason(row),
        tests=surfaced,
        matching=[test for test in surfaced if not test_filter or test_filter in test.name],
    )
