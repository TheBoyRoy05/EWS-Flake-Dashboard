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
from typing import Optional, Union

from flask import Flask, Response, g, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict

from ews_dashboard import config, db, queues, results, suites
from ews_dashboard.analysis import convictions, escapes, false_positive, filters, freshness, trend
from ews_dashboard.web import chart, formatting, links

DEFAULT_WINDOW_DAYS = 7
WINDOW_CHOICES = (7, 14, 30, 60, 90)
BUILDS_SHOWN = 200

# The freshness banner is dismissed by a request rather than in the browser, because this page needs
# no JavaScript and a dismissal has to hold on the next page too. The cookie holds the signature of
# what was dismissed, so a banner saying something new is shown again.
FRESHNESS_COOKIE = 'freshness_dismissed'
FRESHNESS_DISMISSAL_SECONDS = 30 * 86400

# How many days each point of the trend line averages over. The window drives the chart's span, so
# this is the only thing left to choose: a short average shows every spike, a long one shows whether
# the level moved.
ROLLING_CHOICES = (3, 7, 14, 28)

SUITE_CHOICES = tuple(suite.name for suite in suites.SUITES)

# The column a page of convicted tests reads in when a request asked for no order of its own.
DEFAULT_SORT = 'convictions'

# The column a page of escape convictions reads in when a request asked for no order of its own: the
# landings that worsened a test most, bounded, which is what the page exists to surface rather than the
# newest landing it used to hard-code. Derived on read through the registered sqlite function, so the
# order and the printed figure are one definition.
ESCAPES_DEFAULT_SORT = 'increase'

# What those pages read in instead on a category that prints no rate increase. The increase is printed
# only for the verdicts in the merged ESCAPED bucket: a CONTAINED row has no failures after the landing,
# and a NO_RUNS or TREE_DIVERGED row has no runs on one side to take a bound from at all. Ordering those
# by the increase put an arrow on a column of em dashes and sorted the rows by a figure the reader could
# not see, so they read by landing time, which they do print.
ESCAPES_WITHOUT_INCREASE_SORT = 'landed'

# Which page of the escapes listing to show. Named `page` rather than an offset because it is a URL a
# reader can read, and the offset is derived from it against the page size the query actually used.
PAGE_ARGUMENT = 'page'

# The submit buttons that grow the filter and sort chip rows by one blank chip apiece. Named rather
# than a shared name with different values, so a request can carry at most one of them and the route
# never has to guess which row a bare "yes" was about.
ADD_FILTER_ARGUMENT = 'add_filter'
ADD_SORT_ARGUMENT = 'add_sort'

# And their opposites: the submit buttons that drop one chip row. A clause already in the query is
# removed by a plain link subtracting it, but a row a reader has only just added is in no URL to
# subtract from — so it takes the same shape "+ filter" already takes, a named submit, with the row's
# own index as the value. Named per kind for the same reason the add buttons are: one press, one
# meaning, and no guessing which row a bare "yes" was about.
REMOVE_FILTER_ARGUMENT = 'remove_filter'
REMOVE_SORT_ARGUMENT = 'remove_sort'

# What a chip's kind renders its value control as, when its column has no fixed vocabulary. Anything
# not listed here is free text, `filters.TEXT` included.
CHIP_INPUT_TYPES = {filters.INTEGER: 'number', filters.TIMESTAMP: 'date'}


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

    `defaulted` marks a `states` that came from `formatting.DEFAULT_STATES` rather than from the
    request, whether because no `state` argument was given or because none of the given ones were
    recognized; the pane reads it to stay closed on the quiet, default view.
    """

    states: tuple
    min_shown: Optional[int]
    max_shown: Optional[int]
    defaulted: bool

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


@dataclass(frozen=True)
class FilterChip:
    """One row of the filter surface: the column, operator and value a request committed at this
    index, with what its column allows already resolved, so the template does no registry lookup of
    its own.

    A chip with no column yet — a blank one a reader has not filled in — offers every operator the
    registry knows rather than one column's own, since no column means no kind to look one set of
    operators up under; `condition` still drops whichever of those does not belong to the column a
    reader eventually picks.
    """

    index: int
    column: Optional[str]
    operator: Optional[str]
    value: str
    values: tuple
    many_values: bool
    operators: tuple
    vocabulary: Optional[tuple]
    input_type: str
    removal: Optional[str] = None


def _filter_chip(index: int, condition: Optional[filters.Condition],
                 clause: Optional[str] = None,
                 removal: Optional[str] = None) -> FilterChip:
    """One committed condition as a chip, or a blank chip where there is none.

    The value comes from `clause` — the reader's own text — rather than `condition.values`, which
    for `has`/`starts`/`ends` holds the LIKE pattern `prepare` wrapped it in and would show a chip's
    value box a pattern nobody typed.

    `many_values` is the committed operator's own arity, not the column's kind: a vocabulary column
    like `suite` takes both a one-value operator (`eq`) and a many-value one (`in`), and only the
    operator a reader actually chose says which control the value belongs in. `values` is `value`
    split back into the elements a many-value operator's own vocabulary select preselects.

    `removal` is where this one clause's delete link points: this request minus this clause and
    nothing else. A blank chip has none, because there is no committed clause behind it to remove.
    """
    if condition is None:
        return FilterChip(index=index, column=None, operator=None, value='', values=(),
                          many_values=False, operators=filters.ALL_OPERATORS, vocabulary=None,
                          input_type='text')
    column = condition.column
    value = filters.filter_clause_value(clause) if clause is not None else ''
    many_values = condition.operator.arity == filters.MANY_VALUES
    values = tuple(value.split(filters.LIST_SEPARATOR)) if many_values else ()
    return FilterChip(index=index, column=column.name, operator=condition.operator.name,
                      value=value, values=values, many_values=many_values,
                      operators=tuple(column.operators.values()),
                      vocabulary=column.vocabulary,
                      input_type=CHIP_INPUT_TYPES.get(column.kind, 'text'),
                      removal=removal)


def _filter_chips(asked: filters.Requested, extra: int, removals: tuple = ()) -> tuple:
    """Every filter chip the surface shows: one per committed condition, then `extra` blank ones."""
    chips = [_filter_chip(index, condition, clause,
                          removals[index] if index < len(removals) else None)
             for index, (condition, clause)
             in enumerate(zip(asked.conditions, asked.filter_clauses))]
    for _ in range(extra):
        chips.append(_filter_chip(len(chips), None))
    return tuple(chips)


@dataclass(frozen=True)
class SortChip:
    """One row of the sort surface: the column and direction a request committed at this index, or
    the blanks of one not yet filled in.

    `removal` is this one clause's delete link, absent on a blank chip for the same reason it is on a
    blank filter chip: there is no committed clause behind it.
    """

    index: int
    column: Optional[str]
    direction: Optional[str]
    removal: Optional[str] = None


def _sort_chip(index: int, key: Optional[filters.SortKey],
               removal: Optional[str] = None) -> SortChip:
    if key is None:
        return SortChip(index=index, column=None, direction=None)
    return SortChip(index=index, column=key.column.name,
                    direction=filters.DESCENDING if key.descending else filters.ASCENDING,
                    removal=removal)


def _sort_chips(asked: filters.Requested, extra: int, removals: tuple = ()) -> tuple:
    """Every sort chip the surface shows: one per committed key, then `extra` blank ones."""
    chips = [_sort_chip(index, key, removals[index] if index < len(removals) else None)
             for index, key in enumerate(asked.sort_keys)]
    for _ in range(extra):
        chips.append(_sort_chip(len(chips), None))
    return tuple(chips)


def _clause_removals(endpoint: str, table: filters.Table, asked: filters.Requested,
                     anchor: str) -> tuple:
    """`(filter_urls, sort_urls)`: for each committed clause, this request minus that one clause.

    A delete has to be a plain link, because every other control on this surface works with the
    script blocked and a scripted button would be the one that does not. So the whole of it is a URL
    the server already knows how to answer: the clauses are respelled in `asked`'s own written order
    with one index left out, which is what keeps removing the second of three from reordering the
    first and the third.

    Everything else the request carries is kept, apart from four arguments a delete has no business
    forwarding: the `+filter`/`+sort` presses and the `remove` presses, each of which asked the page
    that rendered to grow or shrink by one row, and `page`, since a narrower filter is a different set
    and row 201 of it is not row 201 of this one. A clause that did not parse is not carried either —
    `asked.filter_clauses` holds only what committed — which is the same judgment every other link on
    these pages makes.
    """
    filter_argument = filters.filter_argument(table)
    sort_argument = filters.sort_argument(table)
    filter_stem = f'{filter_argument}{filters.CLAUSE_SEPARATOR}'
    sort_stem = f'{sort_argument}{filters.CLAUSE_SEPARATOR}'
    dropped = (filter_argument, sort_argument, ADD_FILTER_ARGUMENT, ADD_SORT_ARGUMENT,
               REMOVE_FILTER_ARGUMENT, REMOVE_SORT_ARGUMENT, PAGE_ARGUMENT)
    kept = {name: values for name, values in _carried_arguments().items()
            if name not in dropped
            and not name.startswith(filter_stem) and not name.startswith(sort_stem)}
    filter_clauses, sort_clauses = list(asked.filter_clauses), list(asked.sort_clauses)

    def target(filters_asked: list, sorts_asked: list) -> str:
        return url_for(endpoint, **kept, _anchor=anchor,
                       **{filter_argument: filters_asked, sort_argument: sorts_asked})

    return (
        tuple(target(filter_clauses[:index] + filter_clauses[index + 1:], sort_clauses)
              for index in range(len(filter_clauses))),
        tuple(target(filter_clauses, sort_clauses[:index] + sort_clauses[index + 1:])
              for index in range(len(sort_clauses))),
    )


def _removed_chip(argument: str) -> Optional[int]:
    """The chip index a remove press named, or None where this request holds no such press.

    A value that is not a number is None rather than an error, the way every other argument on these
    pages is read: the press arrives in a hand-edited URL as readily as from the button.
    """
    try:
        return int(request.args[argument])
    except (KeyError, ValueError, TypeError):
        return None


def _effective_arguments(table: filters.Table) -> object:
    """This request's arguments with the fields of any chip a remove press named taken out.

    The form submits every chip it holds, so a press of one row's × arrives as the whole surface plus
    `remove_filter=<index>`. Dropping that row's three controls here — before `requested`,
    `exploded_filter_specifications` or the redirect read anything — is what turns the press into "the
    form without that row" without a second grammar for it: every remaining chip goes through exactly
    the validation it went through before.

    A press naming a row this request does not hold takes nothing out, which is the same
    ignore-rather-than-refuse every unreadable argument here gets.
    """
    presses = ((REMOVE_FILTER_ARGUMENT, filters.filter_argument(table)),
               (REMOVE_SORT_ARGUMENT, filters.sort_argument(table)))
    stems = [f'{stem}{filters.CLAUSE_SEPARATOR}{index}{filters.CLAUSE_SEPARATOR}'
             for argument, stem in presses
             for index in (_removed_chip(argument),) if index is not None]
    if not stems:
        return request.args
    kept = MultiDict()
    for name in request.args.keys():
        if any(name.startswith(stem) for stem in stems):
            continue
        for value in request.args.getlist(name):
            kept.add(name, value)
    return kept


def _canonical_filter_arguments(table: filters.Table) -> object:
    """The canonical `f.`/`s.` arguments a request asked with, whether it spoke that grammar directly
    or exploded it into per-chip controls.

    A chip form's controls are always named the exploded way — `requested` never reads the exploded
    spelling, so a request that used it would otherwise look like one that asked for nothing at all.
    """
    arguments = _effective_arguments(table)
    if not filters.has_exploded_arguments(arguments, table):
        return arguments
    canonical = MultiDict()
    for specification in filters.exploded_filter_specifications(arguments, table):
        canonical.add(filters.filter_argument(table), specification)
    for specification in filters.exploded_sort_specifications(arguments, table):
        canonical.add(filters.sort_argument(table), specification)
    return canonical


def _carried_arguments() -> dict:
    """Every query argument this request carries, minus any beginning with `_`.

    `url_for` takes `_anchor`, `_method`, `_scheme` and `_external` as keyword-only arguments of its
    own, so a reader-named argument spelled the same way would bind to one of those instead of
    becoming part of the URL, and — since every value here is a list from `to_dict(flat=False)` —
    raise rather than render for the three of those that take a single value. No page's own grammar
    starts with `_`, so dropping the whole family here cannot lose an argument a reader meant.
    """
    return {name: values for name, values in request.args.to_dict(flat=False).items()
           if not name.startswith('_')}


def _redirect_target(table: filters.Table, endpoint: str) -> Optional[str]:
    """Where a chip-form submission belongs once its exploded columns, operators and values have been
    turned back into the canonical `f.<table>=`/`s.<table>=` spelling this page's own links speak, or
    None where the request already speaks that spelling and needs no redirect.

    Skipped for a `+filter`/`+sort` press: that button asks the page already open for one more blank
    chip, not a new address — the chip it adds has nothing to redirect to yet.

    A remove press is the opposite and DOES redirect. The row it names is already gone from
    `_effective_arguments`, so the canonical spelling built here is the surface without that row, and
    landing on that address is also what drops a blank row a `+filter` press had asked for: the press
    itself is not carried forward, so the page that answers renders only the chips its clauses need.
    """
    removing = (_removed_chip(REMOVE_FILTER_ARGUMENT) is not None
                or _removed_chip(REMOVE_SORT_ARGUMENT) is not None)
    if not removing and (ADD_FILTER_ARGUMENT in request.args
                         or ADD_SORT_ARGUMENT in request.args):
        return None
    arguments = _effective_arguments(table)
    # Asked of the request rather than of `arguments` when removing: taking the last chip out leaves
    # no exploded argument behind, and that request is exactly the one that most needs its redirect —
    # without it the reader is left parked on a URL naming a row that is no longer on the page.
    if not filters.has_exploded_arguments(request.args if removing else arguments, table):
        return None
    filter_stem = f'{filters.filter_argument(table)}{filters.CLAUSE_SEPARATOR}'
    sort_stem = f'{filters.sort_argument(table)}{filters.CLAUSE_SEPARATOR}'
    dropped = (ADD_FILTER_ARGUMENT, ADD_SORT_ARGUMENT, REMOVE_FILTER_ARGUMENT,
               REMOVE_SORT_ARGUMENT)
    kept = {name: values for name, values in _carried_arguments().items()
           if name not in dropped
           and not name.startswith(filter_stem) and not name.startswith(sort_stem)}
    kept[filters.filter_argument(table)] = list(
        filters.exploded_filter_specifications(arguments, table))
    kept[filters.sort_argument(table)] = list(
        filters.exploded_sort_specifications(arguments, table))
    return url_for(endpoint, **kept)


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


@dataclass(frozen=True)
class QueueChoice:
    """What a request asked of the queue picker: the selection it resolved to, and the vocabulary
    values this page could not read, kept so the page can name them instead of narrowing silently."""

    selection: queues.Selection
    ignored: tuple


def _selection(known_builders: tuple) -> QueueChoice:
    """The reader's queue selection, read from `family`, `group`, `version` and `builder`, each
    repeatable and unioned. `version` is `group:version`, group-qualified so a bare version number is
    never taken from the wrong group's vocabulary — `version=iOS:26` must not also select
    `visionOS-26-Simulator-WK2-Tests-EWS`. An unknown family, an unknown group, a `version` that does
    not split into exactly two non-empty parts, and a builder not in this window's own set are all
    dropped rather than refused, matching every other filter here.

    A dropped family, group or version is also named back to the reader, the way an unreadable filter
    clause already is: those three come from a fixed vocabulary, so a value outside it is a URL this
    page cannot honour and silence would read as a filter that matched everything. A builder is not
    named, because a builder absent from this window is a perfectly good queue name asked over the
    wrong days, and it comes back by widening the window.
    """
    families, groups, versions, ignored = [], [], [], []
    for name in request.args.getlist('family'):
        if name in queues.QUEUE_FAMILY_NAMES:
            families.append(name)
        else:
            ignored.append(f'family={name}')
    for name in request.args.getlist('group'):
        if name in queues.QUEUE_GROUP_NAMES:
            groups.append(name)
        else:
            ignored.append(f'group={name}')
    for raw in request.args.getlist('version'):
        parts = raw.split(':', 1)
        if len(parts) == 2 and parts[0] in queues.QUEUE_GROUP_NAMES and parts[1]:
            versions.append((parts[0], parts[1]))
        else:
            ignored.append(f'version={raw}')
    builders = tuple(name for name in request.args.getlist('builder') if name in known_builders)
    return QueueChoice(
        selection=queues.Selection(families=tuple(families), groups=tuple(groups),
                                   versions=tuple(versions), builders=builders),
        ignored=tuple(ignored),
    )


def _queue_summary(selection: queues.Selection, resolved: tuple) -> str:
    """What the collapsed dropdown reads before a reader opens it."""
    if selection.empty:
        return 'All queues'
    count = len(resolved)
    return f'{count} queue' + ('' if count == 1 else 's')


def _build_filter() -> BuildFilter:
    """The builds pane's own filter, read from `state`, `min_shown` and `max_shown`.

    No `state` argument at all is the quiet default: the two noise states rather than every build.
    `formatting.ANY_STATE` asks past that default explicitly, narrowing by state not at all. A list
    that named states but recognized none of them falls back to the same default rather than to
    everything, so a hand-mangled URL cannot silently widen the pane past what a fresh visit shows.
    """
    requested = request.args.getlist('state')
    if not requested:
        states, defaulted = formatting.DEFAULT_STATES, True
    elif formatting.ANY_STATE in requested:
        states, defaulted = (), False
    else:
        recognized = tuple(value for value in requested if value in formatting.STATE_CHOICES)
        states, defaulted = (recognized, False) if recognized else (formatting.DEFAULT_STATES, True)
    return BuildFilter(
        states=states,
        defaulted=defaulted,
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

    @app.route('/tests')
    def tests() -> Union[str, Response]:
        target = _redirect_target(filters.TESTS, 'tests')
        if target is not None:
            return redirect(target)
        return render_template('tests.html', **_tests_context(open_database(), _window()))

    @app.route('/escapes')
    def escaped_regressions() -> Union[str, Response]:
        target = _redirect_target(filters.ESCAPES, 'escaped_regressions')
        if target is not None:
            return redirect(target)
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
    """What every page narrows by, and the queue tree a reader picks from.

    `activity` is computed from `suite` alone, never from the queue selection: it is what the
    dropdown's tree is built from, so narrowing to one queue never makes the others disappear from
    the list a reader could still pick. `builders` is the selection resolved to concrete builder
    names against that same list, which is what every analysis call filters by; an empty `builders`
    means no selection was made, which every analysis function reads as no filter at all.
    """

    suite: Optional[str]
    selection: queues.Selection
    builders: tuple
    activity: list
    tree: tuple
    summary: str
    ignored: tuple


def _scope(open_connection: sqlite3.Connection, window: Window) -> Scope:
    suite = _chosen('suite', SUITE_CHOICES)
    activity = convictions.queue_activity(open_connection, window.since, window.until, suite=suite)
    known = tuple(queue.builder for queue in activity)
    choice = _selection(known)
    selection = choice.selection
    resolved = queues.resolve(selection, known)
    counts = {queue.builder: queue.convictions for queue in activity}
    return Scope(
        suite=suite,
        selection=selection,
        builders=resolved,
        activity=activity,
        tree=queues.tree(known, counts),
        summary=_queue_summary(selection, resolved),
        ignored=choice.ignored,
    )


def _selection_args(selection: queues.Selection) -> dict:
    """`family`/`group`/`version`/`builder` as the tuples a template forwards through a link or a
    hidden field, in the URL grammar `_selection` reads back."""
    return {
        'family': selection.families,
        'group': selection.groups,
        'version': tuple(f'{group}:{version}' for group, version in selection.versions),
        'builder': selection.builders,
    }


def _landing_context(open_connection: sqlite3.Connection, window: Window) -> dict:
    scope = _scope(open_connection, window)
    rolling = _chosen_number('rolling', ROLLING_CHOICES, trend.ROLLING_DAYS)
    classifier = false_positive.cached_classifier(open_connection)
    points = trend.daily(open_connection, classifier, trend.today(), days=window.days,
                         rolling=rolling, suite=scope.suite, builders=scope.builders)
    return dict(
        window=window,
        window_choices=WINDOW_CHOICES,
        suite=scope.suite,
        suite_choices=SUITE_CHOICES,
        **_selection_args(scope.selection),
        queue_tree=scope.tree,
        queue_summary=scope.summary,
        queue_ignored=scope.ignored,
        rolling=rolling,
        rolling_choices=ROLLING_CHOICES,
        counts=false_positive.rate(open_connection, classifier, window.since, window.until,
                                   suite=scope.suite, builders=scope.builders),
        by_rule=convictions.by_rule(open_connection, window.since, window.until,
                                    suite=scope.suite, builders=scope.builders),
        rule_descriptions=config.RULE_DESCRIPTIONS,
        builds_queried=convictions.builds_queried(open_connection, window.since, window.until,
                                                  suite=scope.suite, builders=scope.builders),
        query_failures=convictions.query_failures(open_connection, window.since, window.until,
                                                  suite=scope.suite, builders=scope.builders),
        chart=chart.of_trend(points, trend.deployments_within(points)),
        threshold_pct=config.PRE_EXISTING_THRESHOLD_PCT,
        filter_argument=filters.filter_argument(filters.TESTS),
        links=links,
        **_freshness_context(open_connection),
    )


def _explore_context(open_connection: sqlite3.Connection, window: Window) -> dict:
    scope = _scope(open_connection, window)
    suite, builders = scope.suite, scope.builders
    test_filter = request.args.get('test') or None
    build_filter = _build_filter()
    classifier = false_positive.cached_classifier(open_connection)
    builds, builds_matched = _filtered_builds(open_connection, window, suite, builders,
                                              classifier, build_filter)
    counts = false_positive.rate(
        open_connection, classifier, window.since, window.until, suite=suite, builders=builders,
    )
    return dict(
        window=window,
        window_choices=WINDOW_CHOICES,
        suite=suite,
        suite_choices=SUITE_CHOICES,
        **_selection_args(scope.selection),
        queue_tree=scope.tree,
        queue_summary=scope.summary,
        queue_ignored=scope.ignored,
        builds=builds,
        builds_total=false_positive.failing_build_count(
            open_connection, window.since, window.until, suite=suite, builders=builders,
        ),
        builds_matched=builds_matched,
        build_filter=build_filter,
        state_choices=formatting.STATE_CHOICES,
        state_counts={choice: getattr(counts, choice.lower()) for choice in formatting.STATE_CHOICES},
        detail=_build_detail(open_connection, classifier, test_filter),
        test_filter=test_filter,
        verdict_descriptions=false_positive.VERDICT_DESCRIPTIONS,
        reason_descriptions=false_positive.REASON_DESCRIPTIONS,
        counts=counts,
        bucket_descriptions=false_positive.BUCKET_DESCRIPTIONS,
        verdict_choices=formatting.VERDICT_CHOICES,
        links=links,
        **_freshness_context(open_connection),
    )


def _tests_context(open_connection: sqlite3.Connection, window: Window) -> dict:
    scope = _scope(open_connection, window)
    suite, builders = scope.suite, scope.builders
    test_filter = request.args.get('test') or None
    asked = filters.requested(_canonical_filter_arguments(filters.TESTS), filters.TESTS)
    filter_removals, sort_removals = _clause_removals('tests', filters.TESTS, asked, 'convicted')
    primary = _primary_sort(filters.TESTS, asked.sort_keys, DEFAULT_SORT)
    convicted = convictions.convicted_tests(
        open_connection, window.since, window.until,
        suite=suite, builders=builders,
        conditions=asked.conditions, sort_keys=_test_order(asked.sort_keys),
    )
    drilldown = (
        convictions.test_convictions(open_connection, window.since, window.until, test_filter,
                                     suite=suite, builders=builders)
        if test_filter else None
    )
    return dict(
        window=window,
        window_choices=WINDOW_CHOICES,
        suite=suite,
        suite_choices=SUITE_CHOICES,
        **_selection_args(scope.selection),
        queue_tree=scope.tree,
        queue_summary=scope.summary,
        queue_ignored=scope.ignored,
        convicted=convicted,
        rule_descriptions=config.RULE_DESCRIPTIONS,
        flake_types=config.FLAKINESS_RULES,
        rule_counts=convictions.by_rule(open_connection, window.since, window.until,
                                        suite=suite, builders=builders),
        sort=primary.column.name,
        descending=primary.descending,
        descending_first=filters.TESTS.descending_first,
        tests_table=filters.TESTS,
        asked=asked,
        test_filter=test_filter,
        drilldown=drilldown,
        current_args=_carried_arguments(),
        filter_argument=filters.filter_argument(filters.TESTS),
        sort_argument=filters.sort_argument(filters.TESTS),
        filter_chips=_filter_chips(asked, 1 if ADD_FILTER_ARGUMENT in request.args else 0,
                                  filter_removals),
        sort_chips=_sort_chips(asked, 1 if ADD_SORT_ARGUMENT in request.args else 0,
                               sort_removals),
        add_filter_argument=ADD_FILTER_ARGUMENT,
        add_sort_argument=ADD_SORT_ARGUMENT,
        remove_filter_argument=REMOVE_FILTER_ARGUMENT,
        remove_sort_argument=REMOVE_SORT_ARGUMENT,
        filter_chip_argument=filters.filter_chip_argument,
        sort_chip_argument=filters.sort_chip_argument,
        all_operators=filters.ALL_OPERATORS,
        input_types=CHIP_INPUT_TYPES,
        links=links,
        **_freshness_context(open_connection),
    )


def _filtered_builds(
    open_connection: sqlite3.Connection,
    window: Window,
    suite: Optional[str],
    builders: tuple,
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
            suite=suite, builders=builders, limit=BUILDS_SHOWN,
        )
        return _build_summaries(rows, classifier, build_filter), None
    rows = false_positive.failing_builds(
        open_connection, window.since, window.until, suite=suite, builders=builders,
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


def _page_asked_for() -> int:
    """Which page of the escapes listing a request asked for, as a number at least 1.

    A page that is not a number, or is below the first, is the first rather than a refusal — the
    argument arrives in a hand-edited URL as readily as from a link, and every other filter on these
    pages drops what it cannot read. A page past the last is clamped by `escapes.convictions`, which
    is the only place that knows how many there are.
    """
    try:
        number = int(request.args.get(PAGE_ARGUMENT, 1))
    except (TypeError, ValueError):
        return 1
    return max(1, number)


def _escapes_context(open_connection: sqlite3.Connection, window: Window) -> dict:
    """The escape page: what main did with each convicted test after the change landed.

    Read-only like the others. Deciding an escape needs results.webkit.org and a checkout, so this
    page shows what the refresh has already decided and says how much it could not.

    The listing is filtered, ordered and paged by the `f.escapes=`/`s.escapes=`/`page=` arguments,
    every column name in them validated against `filters.ESCAPES` before any of it reaches SQL. The
    verdict category the pane selects is applied on top of a reader's own clauses rather than by
    them, so a `verdict` clause naming a bucket outside the open category narrows this page to
    nothing and says so — see the ticket in docs/open-work.md about folding the pane's own `verdict=`
    into the grammar. The one pairing that does now intersect is a `verdict` clause naming either
    half of the merged ESCAPED category, which is how a reader asks for that half on its own.

    The category is chosen from `escapes.CATEGORIES`, not from the stored verdict names, since
    FAILS_ON_MAIN is no longer a bucket of its own; a link that still names it opens the merged
    bucket that holds it rather than falling back to the default.
    """
    scope = _scope(open_connection, window)
    verdict_shown = escapes.category_of(_chosen('verdict', escapes.VERDICTS, escapes.ESCAPED))
    asked = filters.requested(_canonical_filter_arguments(filters.ESCAPES), filters.ESCAPES)
    filter_removals, sort_removals = _clause_removals('escaped_regressions', filters.ESCAPES, asked,
                                                      'convictions')
    shows_increase = verdict_shown == escapes.ESCAPED
    primary = _primary_sort(filters.ESCAPES, asked.sort_keys,
                            ESCAPES_DEFAULT_SORT if shows_increase
                            else ESCAPES_WITHOUT_INCREASE_SORT)
    listed = escapes.convictions(open_connection, window.since, window.until,
                                 escapes.category_verdicts(verdict_shown),
                                 suite=scope.suite, builders=scope.builders,
                                 page=_page_asked_for(), conditions=asked.conditions,
                                 sort_keys=_escape_order(asked.sort_keys, shows_increase))
    counted = escapes.tally(open_connection, window.since, window.until,
                            suite=scope.suite, builders=scope.builders)
    subcategories = escapes.escape_subcategories(open_connection, window.since, window.until,
                                                 suite=scope.suite, builders=scope.builders)
    return dict(
        window=window,
        window_choices=WINDOW_CHOICES,
        suite=scope.suite,
        suite_choices=SUITE_CHOICES,
        **_selection_args(scope.selection),
        queue_tree=scope.tree,
        queue_summary=scope.summary,
        queue_ignored=scope.ignored,
        tally=counted,
        category_counts=counted.by_category,
        escaped_verdict=escapes.ESCAPED,
        escape_verdicts=escapes.MERGED_ESCAPE_VERDICTS,
        subcategories=subcategories,
        listed=listed,
        verdict_shown=verdict_shown,
        reason=escapes.reason,
        verdict_descriptions=escapes.VERDICT_DESCRIPTIONS,
        categories=escapes.CATEGORIES,
        window_days=config.ESCAPE_WINDOW_DAYS,
        significance_alpha=config.ESCAPE_SIGNIFICANCE_ALPHA,
        currency_days=config.CURRENCY_DAYS,
        sort=primary.column.name,
        shows_increase=shows_increase,
        descending=primary.descending,
        descending_first=filters.ESCAPES.descending_first,
        sort_label=primary.column.label,
        escapes_table=filters.ESCAPES,
        asked=asked,
        page_argument=PAGE_ARGUMENT,
        filter_argument=filters.filter_argument(filters.ESCAPES),
        sort_argument=filters.sort_argument(filters.ESCAPES),
        filter_chips=_filter_chips(asked, 1 if ADD_FILTER_ARGUMENT in request.args else 0,
                                  filter_removals),
        sort_chips=_sort_chips(asked, 1 if ADD_SORT_ARGUMENT in request.args else 0,
                               sort_removals),
        add_filter_argument=ADD_FILTER_ARGUMENT,
        add_sort_argument=ADD_SORT_ARGUMENT,
        remove_filter_argument=REMOVE_FILTER_ARGUMENT,
        remove_sort_argument=REMOVE_SORT_ARGUMENT,
        filter_chip_argument=filters.filter_chip_argument,
        sort_chip_argument=filters.sort_chip_argument,
        all_operators=filters.ALL_OPERATORS,
        input_types=CHIP_INPUT_TYPES,
        links=links,
        **_freshness_context(open_connection),
    )


FALLBACK_SORT = ((DEFAULT_SORT, True), ('last_seen', True))

# What a page of escape convictions falls back on: the landings that measurably worsened a test most
# first, then the most recent landing among rows that tie. `order_by` drops a fallback key that repeats
# a column the request already ordered on, so asking for `increase:asc` does not get it twice.
ESCAPES_FALLBACK_SORT = ((ESCAPES_DEFAULT_SORT, True), ('landed', True))

# The same, for a category that prints no rate increase: landing time alone, since an increase key there
# would order the rows by a column of em dashes.
ESCAPES_FALLBACK_SORT_WITHOUT_INCREASE = ((ESCAPES_WITHOUT_INCREASE_SORT, True),)


def _primary_sort(table: filters.Table, keys: tuple, default: str) -> filters.SortKey:
    """The key a column heading marks as the one the table is ordered by, which is the first key a
    request asked for or the table's default where it asked for none."""
    if keys:
        return keys[0]
    return filters.sort_keys(table, ((default, True),))[0]


def _test_order(keys: tuple) -> tuple:
    """The sort keys behind a page of convicted tests: what the request asked for, then the two counts
    every view falls back on before the tiebreak `filters` adds.

    A request that asked for no order at all therefore reads by convictions, so the rows a reader
    opening the page unprompted is looking for arrive first; `order_by` drops a fallback that repeats
    a column already ordered on.
    """
    return tuple(keys) + filters.sort_keys(filters.TESTS, FALLBACK_SORT)


def _escape_order(keys: tuple, shows_increase: bool = True) -> tuple:
    """The sort keys behind a page of escape convictions: what the request asked for, then the rate
    increase and landing time, then the tiebreak `filters` adds.

    A category that prints no rate increase falls back on landing time alone, so its rows are never
    ordered by a bound the table renders as an em dash.

    The tiebreak is not decoration here the way it can look on an uncapped table: this listing pages
    with LIMIT/OFFSET, and two queries that break a tie differently would drop a row from one page and
    repeat it on the next.
    """
    fallback = ESCAPES_FALLBACK_SORT if shows_increase else ESCAPES_FALLBACK_SORT_WITHOUT_INCREASE
    return tuple(keys) + filters.sort_keys(filters.ESCAPES, fallback)


def _build_detail(
    open_connection: sqlite3.Connection,
    classifier: false_positive.Classifier,
    test_filter: Optional[str],
) -> Optional[BuildDetail]:
    """The selected build, looked up by id rather than found in the listed page, so a link to a build
    outside the current window or filter still opens it.

    `tests` stays whole and `matching` carries the filter, so a filter that matches nothing reads as
    a filter that matched nothing rather than as a build that surfaced nothing.

    The tests main already fails come first, since those are the ones the build blamed its author for
    and the reason to open this pane at all. `sorted` is stable, so everything else keeps the order
    `explain` returned it in.
    """
    try:
        build_id = int(request.args['build'])
    except (KeyError, ValueError):
        return None
    row = false_positive.failing_build(open_connection, build_id)
    if row is None:
        return None
    surfaced = sorted(false_positive.explain(open_connection, row),
                      key=lambda test: test.verdict != false_positive.PRE_EXISTING)
    return BuildDetail(
        build=row,
        configuration=results.Configuration.of_build(row),
        classification=classifier(row),
        undetermined_reason=false_positive.undetermined_reason(row),
        tests=surfaced,
        matching=[test for test in surfaced if not test_filter or test_filter in test.name],
    )
