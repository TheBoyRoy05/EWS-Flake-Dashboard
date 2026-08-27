"""The two pages, both strictly read-only.

A request never reaches the network and never classifies a build; it reads what scripts/refresh.py
left behind. That is structural rather than a convention: the routes are handed
false_positive.cached_classifier, which has no History to ask and reports an unclassified build as
unclassified. Anything the refresh has not caught up with therefore shows as a gap on the page
instead of as a slow request.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from flask import Flask, g, render_template, request

from ews_dashboard import config, db, results, suites
from ews_dashboard.analysis import convictions, false_positive, freshness, trend
from ews_dashboard.web import chart, formatting, links

DEFAULT_WINDOW_DAYS = 7
WINDOW_CHOICES = (1, 7, 14, 30)
TREND_DAYS = 30
BUILDS_SHOWN = 200

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
    """A whole number of UTC days ending at the end of today, matching the trend's buckets."""

    days: int
    since: int
    until: int

    @classmethod
    def of_days(cls, days: int) -> 'Window':
        until = trend.day_bounds(trend.today())[1]
        return cls(days=days, since=until - days * 86400, until=until)


def _chosen(name: str, choices: tuple, default: Optional[str] = None) -> Optional[str]:
    """A query argument restricted to a known set, so a hand-edited URL selects nothing unknown."""
    value = request.args.get(name)
    return value if value in choices else default


def _window() -> Window:
    try:
        days = int(request.args.get('days', DEFAULT_WINDOW_DAYS))
    except ValueError:
        days = DEFAULT_WINDOW_DAYS
    return Window.of_days(days if days in WINDOW_CHOICES else DEFAULT_WINDOW_DAYS)


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

    return app


def _landing_context(open_connection: sqlite3.Connection, window: Window) -> dict:
    classifier = false_positive.cached_classifier(open_connection)
    points = trend.daily(open_connection, classifier, trend.today(), days=TREND_DAYS)
    return dict(
        window=window,
        window_choices=WINDOW_CHOICES,
        counts=false_positive.rate(open_connection, classifier, window.since, window.until),
        by_rule=convictions.by_rule(open_connection, window.since, window.until),
        rule_descriptions=config.RULE_DESCRIPTIONS,
        builds_queried=convictions.builds_queried(open_connection, window.since, window.until),
        query_failures=convictions.query_failures(open_connection, window.since, window.until),
        chart=chart.of_trend(points, trend.deployments_within(points)),
        trend_days=TREND_DAYS,
        rolling_days=trend.ROLLING_DAYS,
        threshold_pct=config.PRE_EXISTING_THRESHOLD_PCT,
        freshness=freshness.current(open_connection),
        links=links,
    )


def _explore_context(open_connection: sqlite3.Connection, window: Window) -> dict:
    suite = _chosen('suite', tuple(each.name for each in suites.SUITES))
    activity = convictions.queue_activity(open_connection, window.since, window.until, suite=suite)
    builder = _chosen('builder', tuple(queue.builder for queue in activity))
    test_filter = request.args.get('test') or None
    rule = _chosen('rule', config.FLAKINESS_RULES)
    classifier = false_positive.cached_classifier(open_connection)
    rows = false_positive.failing_builds(
        open_connection, window.since, window.until,
        suite=suite, builder=builder, limit=BUILDS_SHOWN,
    )
    return dict(
        window=window,
        window_choices=WINDOW_CHOICES,
        builder=builder,
        suite=suite,
        suite_choices=tuple(each.name for each in suites.SUITES),
        queue_activity=activity,
        builds=[BuildSummary(row, classifier(row)) for row in rows],
        builds_total=false_positive.failing_build_count(
            open_connection, window.since, window.until, suite=suite, builder=builder,
        ),
        detail=_build_detail(open_connection, classifier, test_filter),
        test_filter=test_filter,
        verdict_descriptions=false_positive.VERDICT_DESCRIPTIONS,
        reason_descriptions=false_positive.REASON_DESCRIPTIONS,
        counts=false_positive.rate(
            open_connection, classifier, window.since, window.until, suite=suite, builder=builder,
        ),
        rules=_rule_sections(open_connection, window, suite, builder, rule),
        rule=rule,
        freshness=freshness.current(open_connection),
        links=links,
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
