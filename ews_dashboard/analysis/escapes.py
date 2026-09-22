"""What main did with a test after the change EWS told an author was not to blame for it.

Every other number in this dashboard is a floor, because main does not contain the change and a test
that is reliable on main and genuinely broken by the change cannot be told apart from one that flaked
during the build. Once the change lands, it can: the test now runs on main with the change in it, so
a conviction that was wrong shows up as main failing the test it excused.

One convicted test in one build, whose pull request landed as a known commit on main, falls in one
bucket:

  ESCAPED          main failed it unexpectedly after the landing and never failed it before: the
                   conviction excused a real regression
  FAILS_ON_MAIN    main failed it unexpectedly after the landing, and had failed it before too, at
                   any rate at all
  CONTAINED        no unexpected failure on main after the landing
  NO_RUNS          nothing ran the test on main in the window after the landing
  NO_BASELINE      it failed after the landing, but nothing ran before it, so a regression cannot be
                   told from a failure main already had
  TREE_DIVERGED    a later build of the same pull request, started before the landing, tested a
                   different head, so what landed is not what this conviction was made on. A build
                   EWS started after the landing is not divergence: it cannot have superseded the
                   tree that was already on main

ESCAPED and FAILS_ON_MAIN are one bucket on the page. They are stored apart, because the counts
either side of the landing are the raw observation and nothing here rewrites those, but they answer
the same question — main failed the test after the landing — and the baseline alone decided which of
the two names a conviction got. That one-failure knife-edge sorted severity wrongly: a test main had
failed once in a hundred runs before the landing and then failed 88 of 125 runs after it was called
FAILS_ON_MAIN and went unread, while an escape resting on one failure in fifty runs was called
ESCAPED and led the page. So `CATEGORIES` is what the page lists, its ESCAPED category is the union
of the two stored names, and the baseline question is kept as a subcategory under it: of those
convictions, main was not failing the test before the landing, or was already failing it. Either half
is still reachable on its own through a `verdict` filter on the listing, which narrows by the stored
name.

Three further things are read off a bucket's stored counts rather than stored beside them — how many
distinct tests its convictions name, whether the landing measurably worsened the test, and whether
main is still failing the test now. The distinct-test count is shown beside the conviction count
because one landed regression makes a new conviction on every later pull request whose build trips
the same test, so the convictions count more loudly than the regressions behind them do.

Whether main is still failing an escaped test cannot be read from the window either side of the
landing however wide it is, so the assess pass asks a second, fresh question over the last
CURRENCY_DAYS days — of the escapes alone, at most once a day each — and stores the runs and failures
it got back. That answer has four states and never fewer: still failing, recovered, not run lately
when main ran the test no times in the window, and unchecked when nothing has asked yet. Both of the
last two are absences of evidence rather than recoveries, which is why the runs are stored and no
boolean is.

Only an unexpected failure counts. A test main already lists as failing is failing to order, and
counting it would convict every rule of an escape it had nothing to do with.

What this cannot do: it sees only pull requests whose landing `webkit_checkout` could pin down, only
the window either side of the landing, and only tests some bot on main actually runs in the same
configuration. ESCAPED is therefore a floor as well, and the buckets that answer nothing are counted
and shown rather than dropped.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from ews_dashboard import config, queues, results, webkit_checkout
from ews_dashboard.analysis import filters

# Defined in `config` rather than here, and re-exported under these names so every reader of this
# module still says `escapes.ESCAPED`. The registry in `analysis.filters` needs the same six strings
# to validate a request's verdict against, and this module reads `filters` for its own clauses, so
# the vocabulary has to live below both of them.
ESCAPED = config.ESCAPED
FAILS_ON_MAIN = config.FAILS_ON_MAIN
CONTAINED = config.CONTAINED
NO_RUNS = config.NO_RUNS
NO_BASELINE = config.NO_BASELINE
TREE_DIVERGED = config.TREE_DIVERGED

VERDICTS = config.ESCAPE_VERDICTS

# The stored verdicts the page shows as one ESCAPED bucket: main failed the test after the landing,
# whether or not it had failed it before. Both names stay stored, and both stay in the listing's
# filter vocabulary, so the baseline is demoted from a category to a subcategory rather than lost —
# and folding them is a decision about what the page shows, reversible by editing this tuple, not a
# rewrite of anything the assess pass recorded.
MERGED_ESCAPE_VERDICTS = (ESCAPED, FAILS_ON_MAIN)

# The top-level buckets the page lists, in the order it lists them. FAILS_ON_MAIN is not one of them:
# it is half of ESCAPED. Every stored verdict belongs to exactly one category, which is what lets a
# category's count be a sum over `by_verdict` rather than a second query.
CATEGORIES = (ESCAPED, CONTAINED, NO_RUNS, NO_BASELINE, TREE_DIVERGED)

CATEGORY_VERDICTS = {
    category: MERGED_ESCAPE_VERDICTS if category == ESCAPED else (category,)
    for category in CATEGORIES
}


def category_verdicts(category: str) -> tuple:
    """The stored verdicts one listed category shows.

    A name that is not a category stands for itself, so a caller narrowing by a stored verdict this
    page does not list gets that verdict rather than an empty set.
    """
    return CATEGORY_VERDICTS.get(category, (category,))


def category_of(verdict: str) -> str:
    """Which listed category a stored verdict is shown under.

    A `verdict=FAILS_ON_MAIN` link from before the fold therefore opens the bucket that now holds it
    rather than a bucket the pane no longer has.
    """
    for category, verdicts in CATEGORY_VERDICTS.items():
        if verdict in verdicts:
            return category
    return verdict


# Whether the landing measurably worsened the test, and a partition of the merged escape bucket.
# Derived from the four stored counts on every read rather than stored alongside them, so it can never
# contradict the numbers beside it.
SIGNIFICANT = 'significant'
NOT_SIGNIFICANT = 'not_significant'

# What main is doing with an escaped test now, and a partition of the escapes: exactly one of these
# holds for any row. Neither UNCHECKED nor NOT_RUN_LATELY is another way of saying recovered — nobody
# asked, and nobody ran it, are two different absences of evidence, and neither is good news.
STILL_FAILING = 'still_failing'
RECOVERED = 'recovered'
NOT_RUN_LATELY = 'not_run_lately'
UNCHECKED = 'unchecked'

# What the baseline said, and a partition of the merged escape bucket: the question that used to
# split it into two categories, kept as a split under the one. Read from the stored verdict rather
# than from `failed_before`, so the subcategory a conviction is counted in is the same thing the
# listing's `verdict` filter selects it by and the two can never disagree.
BASELINE_CLEAN = 'baseline_clean'
BASELINE_FAILING = 'baseline_failing'

# What answers nothing about the conviction, so it belongs in no rate. An escape on few failures is
# not here: the question was answered, and only the evidence behind the answer is thin.
UNDECIDED_VERDICTS = (NO_RUNS, NO_BASELINE, TREE_DIVERGED)

VERDICT_DESCRIPTIONS = {
    ESCAPED: f'Main failed this in the {config.ESCAPE_WINDOW_DAYS} days after the landing, so the '
            'conviction excused a failure main went on to have. Whether main had also failed it '
            'before the landing is the split under this bucket, not a bucket of its own: one '
            'failure in a long clean baseline was enough to separate the two, and it separated '
            'them by nothing a reader is looking for. Whether the landing measurably worsened the '
            'test is the other split: strong means the bounded rate increase either side of it '
            'clears zero at alpha '
            f'{config.ESCAPE_SIGNIFICANCE_ALPHA:.2f}, and no longer means a share of the runs after '
            f'the landing failing. The {config.ESCAPE_WINDOW_DAYS} days either side are what the '
            'counts were taken over.',
    FAILS_ON_MAIN: f'Main was already failing this in the {config.ESCAPE_WINDOW_DAYS} days before '
                   'the change landed, and failed it after the landing too. Stored apart from '
                   'ESCAPED and shown with it: this is the already-failing half of that bucket.',
    CONTAINED: f'Main did not fail this in the {config.ESCAPE_WINDOW_DAYS} days after the landing.',
    NO_RUNS: f'No bot ran this on main in the {config.ESCAPE_WINDOW_DAYS} days after the change '
            'landed.',
    NO_BASELINE: f'It failed on main in the {config.ESCAPE_WINDOW_DAYS} days after the change '
                f'landed, but nothing ran it in the {config.ESCAPE_WINDOW_DAYS} days before, so a '
                'regression cannot be told from a failure main already had.',
    TREE_DIVERGED: 'This conviction was made on a version of the pull request that a later build '
                   'superseded before the landing, so the code that landed is not the code it was '
                   'made on and main cannot grade it.',
}

# The pull requests a conviction cannot even be looked for on, counted from `landings` rather than
# stored, since a pull request that has not landed yet is the ordinary case and not an answer.
NOT_LANDED = 'not_landed'
AMBIGUOUS = 'ambiguous'
UNRESOLVED = 'unresolved'
UNAVAILABLE = 'unavailable'

ESCAPE_WINDOW_SECONDS = config.ESCAPE_WINDOW_DAYS * 86400
CURRENCY_WINDOW_SECONDS = config.CURRENCY_DAYS * 86400

# One escape is worth reading about individually, so a page of them is long. It is a page size and no
# longer a cap: `convictions` reports the total it was taken from and which page of it this is, so the
# remainder is reachable rather than silently cut off at row 200.
ESCAPES_LISTED = 200

WINDOW = 'build.started_at >= :since AND build.started_at < :until'


# The one-sided normal deviate the bound below is taken at, derived from the single alpha in `config`
# rather than written down beside it: a hard-coded z is a second place the significance level lives,
# and the two of them drifted apart is exactly how a page ends up printing one level and testing
# another. `statistics` is standard library, so this costs the dashboard no dependency.
SIGNIFICANCE_Z = statistics.NormalDist().inv_cdf(1 - config.ESCAPE_SIGNIFICANCE_ALPHA)


def _wilson_interval(runs: int, failed: int) -> 'tuple[float, float]':
    """The Wilson score interval on `failed / runs`, at the page's one significance level.

    Both ends come back together because the increase bound needs one end of each of two intervals and
    must take them at the same z: two levels either side of a subtraction would be a bound at neither.
    Clamped to [0, 1], which the score interval can exceed at the extremes.
    """
    n = float(runs)
    p = failed / n
    z = SIGNIFICANCE_Z
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def rate_increase_for_counts(runs_before: int, failed_before: int, runs_after: int,
                             failed_after: int) -> Optional[float]:
    """A lower bound on how much more often main fails the test after the landing, as a fraction.

    Newcombe's square-and-add (MOVER-R) bound on `p_after - p_before`: the distance from each rate to
    the end of its own Wilson interval that faces the other, combined in quadrature.

        lower = (p_after - p_before)
                - sqrt((p_after - wilson_lower(after))^2 + (wilson_upper(before) - p_before)^2)

    A lower bound and not the difference itself, which is the whole point: a difference says how much
    the rates differ on the runs that happened, and the bound says how much of that difference the
    number of runs will support. So a test that went from 0 of 4 to 4 of 4 does not outrank one that
    went from 0 of 94 to 56 of 127, and the share of failing runs after the landing — which is what
    this replaced — ranked it above.

    None when either side has no runs. Nothing ran after the landing is no change to measure, and
    nothing ran before it is no baseline to measure against: in both cases the answer is absent rather
    than zero, and a 0 here would read as a landing shown to have changed nothing.

    Negative is kept signed rather than clamped, because below zero is the ordinary case — most rows
    in the merged bucket are there on a baseline that was already failing — and it is what
    `significance_for_counts` reads.
    """
    if not runs_after or not runs_before:
        return None
    before = failed_before / runs_before
    after = failed_after / runs_after
    lower_after, _ = _wilson_interval(runs_after, failed_after)
    _, upper_before = _wilson_interval(runs_before, failed_before)
    return (after - before) - math.sqrt((after - lower_after) ** 2
                                        + (upper_before - before) ** 2)


def significance_for_counts(runs_before: int, failed_before: int, runs_after: int,
                            failed_after: int) -> Optional[str]:
    """Whether the landing measurably worsened the test: the bound above zero, or not.

    None exactly where the bound is None, so a row this cannot answer for is counted in no half of the
    split rather than in the safe-looking one. No row in the merged bucket reaches that:
    `verdict_for_counts` sends a conviction with no run after the landing to NO_RUNS and one with no
    run before it to NO_BASELINE, so both halves together are the whole of the bucket.
    """
    increase = rate_increase_for_counts(runs_before, failed_before, runs_after, failed_after)
    if increase is None:
        return None
    return SIGNIFICANT if increase > 0 else NOT_SIGNIFICANT


def currency_for_counts(recent_runs: Optional[int], recent_failed: Optional[int],
                        recent_checked_at: Optional[int]) -> str:
    """What main is doing with the test now, from the last currency check.

    Four states, and one of them holds for every escape. Both absences of evidence are named as
    themselves: an escape nobody has asked about is UNCHECKED, and one main ran nothing about in the
    window is NOT_RUN_LATELY, because a recovery main was never in a position to demonstrate would
    read as reassurance a reader has no grounds for.
    """
    if recent_checked_at is None or recent_runs is None:
        return UNCHECKED
    if not recent_runs:
        return NOT_RUN_LATELY
    return STILL_FAILING if recent_failed else RECOVERED


def damage_for_counts(recent_runs: Optional[int], recent_failed: Optional[int]) -> Optional[float]:
    """`recent_failed / recent_runs` from the last currency check, as a fraction.

    None when `recent_runs` is None or zero, or when `recent_failed` is None: a row nobody has asked
    about, or asked about and got no runs back, has no rate to report, and 0% would claim an answer
    the data does not carry.
    """
    if not recent_runs or recent_failed is None:
        return None
    return recent_failed / recent_runs


# The two derived figures, as sqlite functions, so a column can be ordered and narrowed by exactly
# the number the page prints. `db.connect` registers these; nothing re-implements either formula in
# SQL, which is the whole point — a stored or re-spelled rate increase is a second definition that can
# disagree with the counts shown beside it.
#
# Both return None where their inputs carry no evidence, which arrives in SQL as NULL, so every
# column built on them orders with `nulls_last` set: a row that answers nothing must not outrank one
# that does, in either direction.
SQL_FUNCTIONS = (
    (config.ESCAPE_INCREASE_FUNCTION, 4, rate_increase_for_counts),
    (config.ESCAPE_DAMAGE_FUNCTION, 2, damage_for_counts),
)


def _filters(suite: Optional[str], builders: tuple = ()) -> tuple:
    """Extra WHERE clauses for a query with build_verdicts aliased as `build`, and their parameters,
    returned together so a caller cannot bind one without the other."""
    conditions, parameters = '', {}
    if suite is not None:
        conditions += ' AND build.suite = :suite'
        parameters['suite'] = suite
    fragment, builder_parameters = queues.builder_filter(builders)
    if fragment:
        conditions += f' AND {fragment}'
        parameters.update(builder_parameters)
    return conditions, parameters


@dataclass(frozen=True)
class Conviction:
    """One conviction main answered, and everything a reader needs to go and check the answer.

    `heads` and `builds` count the pull request's builds rather than this one's, which is what says
    how far the code that landed had moved from the code convicted here.

    `landed_at` and `window_ends_at` both come off the row rather than one being derived from the
    other: the gap between them is ESCAPE_WINDOW_DAYS, and a stored row's landing time must not move
    when that setting does. `landed_at` is None for a row whose landing the database no longer holds,
    and a page says so rather than printing a date.

    The `recent_` fields are the last currency check, and are None together when none has run.
    """

    test_name: str
    rule: str
    verdict: str
    build_id: int
    builder: str
    builder_id: int
    build_number: int
    pr_id: Optional[int]
    configuration: results.Configuration
    runs_before: int
    failed_before: int
    runs_after: int
    failed_after: int
    landed_at: Optional[int]
    window_ends_at: int
    tested_sha: Optional[str]
    newest_sha: Optional[str]
    heads: int
    builds: int
    recent_runs: Optional[int] = None
    recent_failed: Optional[int] = None
    recent_checked_at: Optional[int] = None

    @property
    def significance(self) -> Optional[str]:
        return significance_for_counts(self.runs_before, self.failed_before, self.runs_after,
                                       self.failed_after)

    @property
    def significant(self) -> bool:
        """Whether the landing measurably worsened the test. False where the question cannot be
        answered at all, since a row with no bound has not been shown to have worsened anything."""
        return self.significance == SIGNIFICANT

    @property
    def currency(self) -> str:
        return currency_for_counts(self.recent_runs, self.recent_failed, self.recent_checked_at)

    @property
    def rate_increase(self) -> Optional[float]:
        return rate_increase_for_counts(self.runs_before, self.failed_before, self.runs_after,
                                        self.failed_after)

    @property
    def damage(self) -> Optional[float]:
        return damage_for_counts(self.recent_runs, self.recent_failed)


@dataclass(frozen=True)
class Subcategories:
    """How a window's escapes split, three times over, and how many tests they name.

    Three partitions of the one number, not eight buckets: what main is doing with the test now,
    whether the landing measurably worsened it, and what main had done with it before. Counted here
    rather than in a template so every total is the escape count and a page cannot print a split that
    does not add up.

    `distinct_tests` is not a partition and is not comparable to the others: it is how many test names
    the same convictions name. One landed regression makes a fresh conviction on every later pull
    request whose build trips the same test, so the conviction count runs well ahead of the number of
    underlying regressions and a page that prints only the former invites reading it as the latter.
    `significant_tests` is the same count over the significant half alone, which the headline needs:
    the figure it leads with is a count of blame events, and the number of tests behind them is the
    thing a reader would otherwise infer wrongly from it.
    """

    still_failing: int = 0
    recovered: int = 0
    not_run_lately: int = 0
    unchecked: int = 0
    significant: int = 0
    not_significant: int = 0
    baseline_clean: int = 0
    baseline_failing: int = 0
    distinct_tests: int = 0
    significant_tests: int = 0

    @property
    def total(self) -> int:
        return self.still_failing + self.recovered + self.not_run_lately + self.unchecked

    @property
    def significance_total(self) -> int:
        return self.significant + self.not_significant

    @property
    def baseline_total(self) -> int:
        return self.baseline_clean + self.baseline_failing

    def significant_rate_pct(self, decided: int) -> Optional[float]:
        """The significant half as a share of every conviction main answered, which is the headline.

        Taken over `decided` rather than over this split's own total, because the question the page
        leads with is what share of the answers were a landing making a test worse — not what share of
        the escapes were. None over an empty denominator rather than 0%, which would claim an answer.
        """
        if not decided:
            return None
        return round(100.0 * self.significant / decided, 1)


@dataclass(frozen=True)
class Tally:
    """What a window's convictions came to: what main answered, and what it could not be asked.

    `asked` is `decided` plus `undecided` plus `unrecognised_total`. That last term is normally zero
    and exists so it cannot quietly stop being: a verdict name this code has retired still has rows
    stored under it, and counting only the names in `VERDICTS` once took those convictions out of
    every total on the page without saying so.
    """

    by_verdict: dict
    unaskable: dict
    unrecognised: dict = field(default_factory=dict)

    @property
    def asked(self) -> int:
        """Every conviction main was asked about, answered or not, which is what the buckets divide
        up."""
        return sum(self.by_verdict.values()) + self.unrecognised_total

    @property
    def decided(self) -> int:
        """Convictions main gave an answer about, which is the only honest denominator here."""
        return sum(count for verdict, count in self.by_verdict.items()
                   if verdict not in UNDECIDED_VERDICTS)

    @property
    def by_category(self) -> dict:
        """One count per listed category, so the pane's own buckets are what it tallies.

        Summed from `by_verdict` rather than queried again, since every stored verdict belongs to
        exactly one category: the ESCAPED category is ESCAPED plus FAILS_ON_MAIN, and the rest stand
        alone. A category nothing reached is a zero here rather than a missing key, the way
        `by_verdict` already keeps its own.
        """
        return {category: sum(self.by_verdict.get(verdict, 0)
                              for verdict in category_verdicts(category))
                for category in CATEGORIES}

    @property
    def escaped(self) -> int:
        """The merged escape bucket: every conviction main failed the test after, either baseline."""
        return sum(self.by_verdict.get(verdict, 0) for verdict in MERGED_ESCAPE_VERDICTS)

    @property
    def escape_rate_pct(self) -> Optional[float]:
        if not self.decided:
            return None
        return round(100.0 * self.escaped / self.decided, 1)

    @property
    def undecided(self) -> int:
        return sum(self.by_verdict.get(verdict, 0) for verdict in UNDECIDED_VERDICTS)

    @property
    def unrecognised_total(self) -> int:
        """Convictions stored under a verdict name this code has no bucket for.

        Not folded into `undecided`, which names three specific reasons main answered nothing: these
        were answered, by a version of the dashboard that has since renamed the answer, and the fix
        is `scripts/migrate_verdict_names.py` rather than another run.
        """
        return sum(self.unrecognised.values())

    @property
    def unasked(self) -> int:
        return sum(self.unaskable.values())


@dataclass(frozen=True)
class Candidate:
    """One convicted test whose pull request landed, and the two things that bound the check.

    `newest_sha` is the head of the newest build of the same pull request that EWS started at or
    before the landing, which is what decides whether the conviction was made on the code that
    landed. A build started after the landing tested something main already had, so it cannot have
    superseded the tree this conviction was made on and is left out.
    """

    build_id: int
    test_name: str
    rule: str
    configuration: results.Configuration
    pr_id: int
    landed_at: int
    tested_sha: Optional[str]
    newest_sha: Optional[str]

    @property
    def window_ends_at(self) -> int:
        return self.landed_at + ESCAPE_WINDOW_SECONDS

    @property
    def diverged(self) -> bool:
        return (self.tested_sha is not None and self.newest_sha is not None
                and self.tested_sha != self.newest_sha)


@dataclass(frozen=True)
class Verdict:
    verdict: str
    runs_before: int = 0
    failed_before: int = 0
    runs_after: int = 0
    failed_after: int = 0


CANDIDATE_SQL = f'''
    SELECT verdict.test_name, verdict.rule, build.build_id, build.pr_id, build.sha AS tested_sha,
           build.suite, build.platform, build.style, build.flavor, landing.landed_at,
           (SELECT newer.sha FROM build_verdicts AS newer
             WHERE newer.pr_id = build.pr_id AND newer.sha IS NOT NULL
               AND newer.started_at <= landing.landed_at
             ORDER BY newer.started_at DESC, newer.build_id DESC LIMIT 1) AS newest_sha
    FROM latest_flakiness_verdicts AS verdict
    JOIN build_verdicts AS build USING (build_id)
    JOIN landings AS landing ON landing.pr_id = build.pr_id
    WHERE verdict.rule IS NOT NULL
      AND landing.status = '{webkit_checkout.LANDED}' AND landing.landed_at IS NOT NULL
      AND build.started_at >= :since AND build.started_at < :until
    ORDER BY landing.landed_at, build.build_id, verdict.test_name
'''


def candidates(connection: sqlite3.Connection, since: int, until: int) -> 'list[Candidate]':
    """Every convicted test in the window that main can be asked about, oldest landing first."""
    return [
        Candidate(
            build_id=row['build_id'],
            test_name=row['test_name'],
            rule=row['rule'],
            configuration=results.Configuration.of_build(row),
            pr_id=row['pr_id'],
            landed_at=row['landed_at'],
            tested_sha=row['tested_sha'],
            newest_sha=row['newest_sha'],
        )
        for row in connection.execute(CANDIDATE_SQL, {'since': since, 'until': until})
    ]


def verdict_for_counts(runs_before: int, failed_before: int, runs_after: int,
                       failed_after: int) -> str:
    """Which bucket the runs either side of the landing put the conviction in.

    The baseline decides whose failure it is and nothing else does: a test main was already failing,
    at any rate at all, is main's, and a test main had never failed before the landing escaped
    whether it then failed most of the runs or one of them. Whether the landing measurably worsened
    the test is `significance_for_counts`, read off these same counts wherever an escape is shown
    rather than stored as a verdict of its own.
    """
    if not runs_after:
        return NO_RUNS
    if not failed_after:
        # A clean window after the landing needs no baseline: nothing failed, so nothing escaped.
        return CONTAINED
    if not runs_before:
        return NO_BASELINE
    if failed_before:
        return FAILS_ON_MAIN
    return ESCAPED


def redecided(verdict: str, runs_before: int, failed_before: int, runs_after: int,
              failed_after: int) -> str:
    """What a verdict already stored with these counts would be decided as now.

    TREE_DIVERGED is reached before any run is asked for and stores no counts, so it is left as it
    is: putting its zeroes through the rule would rewrite it to NO_RUNS.
    """
    if verdict == TREE_DIVERGED:
        return verdict
    return verdict_for_counts(runs_before, failed_before, runs_after, failed_after)


def decide(runs_before: list, runs_after: list) -> Verdict:
    """Which bucket the runs put the conviction in.

    Pure, and the whole of the judgement: everything else here fetches, stores or counts.
    """
    counts = dict(runs_before=len(runs_before),
                  failed_before=len([run for run in runs_before if run.unexpected]),
                  runs_after=len(runs_after),
                  failed_after=len([run for run in runs_after if run.unexpected]))
    return Verdict(verdict_for_counts(**counts), **counts)


def _runs_in(history: results.History, candidate: Candidate, after: int, before: int) -> list:
    return history.runs(results.RunQuery(candidate.test_name, candidate.configuration,
                                         after=after, before=before))


def _baseline_runs(history: results.History, candidate: Candidate) -> list:
    """What main did with the test before the landing.

    Filtered on the commit rather than left to the query's bounds, because whether the endpoint's
    `after_timestamp` and `before_timestamp` include their endpoints is not documented, and a run of
    the landing commit itself would otherwise count as the baseline it is compared against.
    """
    runs = _runs_in(history, candidate, candidate.landed_at - ESCAPE_WINDOW_SECONDS,
                    candidate.landed_at)
    return [run for run in runs if run.commit_at < candidate.landed_at]


def _watched_runs(history: results.History, candidate: Candidate) -> list:
    runs = _runs_in(history, candidate, candidate.landed_at, candidate.window_ends_at)
    return [run for run in runs if run.commit_at >= candidate.landed_at]


def assess_one(history: results.History, candidate: Candidate) -> Verdict:
    """One conviction's verdict, asking main about the test either side of the landing."""
    if candidate.diverged:
        return Verdict(TREE_DIVERGED)
    return decide(_baseline_runs(history, candidate), _watched_runs(history, candidate))


def _stored(connection: sqlite3.Connection, candidate: Candidate) -> Optional[Verdict]:
    """The verdict already reached for this conviction, or None when it has to be reached again.

    A verdict decided while the window it watched was still filling is not kept: the runs that would
    turn CONTAINED into ESCAPED arrive after the last commit in the window, not with it.
    """
    row = connection.execute(
        '''SELECT verdict, runs_before, failed_before, runs_after, failed_after,
                  window_ends_at, decided_at
           FROM escape_verdicts WHERE build_id = ? AND test_name = ?''',
        (candidate.build_id, candidate.test_name),
    ).fetchone()
    if row is None:
        return None
    if row['decided_at'] < row['window_ends_at'] + results.RUNS_SETTLING_SECONDS:
        return None
    return Verdict(
        verdict=row['verdict'],
        runs_before=row['runs_before'],
        failed_before=row['failed_before'],
        runs_after=row['runs_after'],
        failed_after=row['failed_after'],
    )


def _store(connection: sqlite3.Connection, candidate: Candidate, verdict: Verdict) -> None:
    """Store the verdict, dropping any currency answer with it.

    REPLACE deletes the row before inserting, so the `recent_` columns go back to null here. That is
    what a re-decided verdict deserves — the answer was about the old verdict — and the currency check
    that follows in the same pass fills them again when the verdict is still an escape.
    """
    with connection:
        connection.execute(
            '''INSERT OR REPLACE INTO escape_verdicts (
                build_id, test_name, verdict, runs_before, failed_before, runs_after,
                failed_after, landed_at, window_ends_at, decided_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (candidate.build_id, candidate.test_name, verdict.verdict, verdict.runs_before,
             verdict.failed_before, verdict.runs_after, verdict.failed_after,
             candidate.landed_at, candidate.window_ends_at, int(time.time())),
        )


def _currency_due(connection: sqlite3.Connection, candidate: Candidate, now: int) -> bool:
    """Whether this escape's currency answer is missing or older than the TTL."""
    row = connection.execute(
        'SELECT recent_checked_at FROM escape_verdicts WHERE build_id = ? AND test_name = ?',
        (candidate.build_id, candidate.test_name),
    ).fetchone()
    if row is None or row['recent_checked_at'] is None:
        return True
    return row['recent_checked_at'] <= now - config.CURRENCY_TTL_SECONDS


def _store_currency(connection: sqlite3.Connection, candidate: Candidate, runs: int, failed: int,
                    checked_at: int) -> None:
    with connection:
        connection.execute(
            '''UPDATE escape_verdicts
               SET recent_runs = ?, recent_failed = ?, recent_checked_at = ?
               WHERE build_id = ? AND test_name = ?''',
            (runs, failed, checked_at, candidate.build_id, candidate.test_name),
        )


def check_currency(connection: sqlite3.Connection, history: results.History, candidate: Candidate,
                   now: int) -> bool:
    """Ask main whether it is still failing an escaped test, over a fresh window ending now.

    Returns whether an answer was stored. An outage leaves the columns as they were: the page names
    such a verdict unchecked, which is the truth, and the next pass asks again.
    """
    if not _currency_due(connection, candidate, now):
        return False
    try:
        runs = _runs_in(history, candidate, now - CURRENCY_WINDOW_SECONDS, now)
    except results.HistoryUnavailable:
        return False
    _store_currency(connection, candidate, len(runs),
                    len([run for run in runs if run.unexpected]), now)
    return True


def assess(connection: sqlite3.Connection, history: results.History, since: int,
           until: int) -> Counter:
    """Decide every convicted test in the window that main can be asked about, and store the answers.

    Returns a count per verdict, plus `unavailable` for the convictions results.webkit.org could not
    be reached about. Nothing is stored for those, so the next pass asks again.

    Only the escapes are asked whether main is still failing them, and each of those at most once a
    day: there are tens of escapes against thousands of convictions, and a currency query per
    conviction would be a second full pass over results.webkit.org.
    """
    outcomes: Counter = Counter()
    now = int(time.time())
    for candidate in candidates(connection, since, until):
        verdict = _stored(connection, candidate)
        if verdict is None:
            try:
                verdict = assess_one(history, candidate)
            except results.HistoryUnavailable:
                outcomes[UNAVAILABLE] += 1
                continue
            _store(connection, candidate, verdict)
        outcomes[verdict.verdict] += 1
        if verdict.verdict == ESCAPED:
            check_currency(connection, history, candidate, now)
    return outcomes


def unaskable(connection: sqlite3.Connection, since: int, until: int, suite: Optional[str] = None,
              builders: tuple = ()) -> dict:
    """Convictions in the window that main cannot be asked about, by why not.

    Read from the database alone and never stored, because every one of these is a pull request that
    may land, or be resolved, before the next pass.
    """
    conditions, parameters = _filters(suite, builders)
    parameters.update({'since': since, 'until': until, 'unresolved': UNRESOLVED,
                       'landed': webkit_checkout.LANDED})
    counted = {
        row['status']: row['convictions']
        for row in connection.execute(
            f'''SELECT COALESCE(landing.status, :unresolved) AS status, COUNT(*) AS convictions
                FROM latest_flakiness_verdicts AS verdict
                JOIN build_verdicts AS build USING (build_id)
                LEFT JOIN landings AS landing ON landing.pr_id = build.pr_id
                WHERE verdict.rule IS NOT NULL AND {WINDOW}{conditions}
                  AND (landing.status IS NULL OR landing.status != :landed)
                GROUP BY status''',
            parameters,
        )
    }
    return {reason: counted.get(reason, 0) for reason in (NOT_LANDED, AMBIGUOUS, UNRESOLVED)}


def _counted_verdicts(connection: sqlite3.Connection, since: int, until: int,
                      suite: Optional[str], builders: tuple = ()) -> tuple:
    """Stored verdicts split into the buckets this code knows and the names it does not.

    Both halves come off one query, because a name no longer in `VERDICTS` still has rows and
    counting only the known ones is how a retired verdict once dropped out of every total silently.
    """
    conditions, parameters = _filters(suite, builders)
    parameters.update({'since': since, 'until': until})
    counted = {
        row['verdict']: row['convictions']
        for row in connection.execute(
            f'''SELECT outcome.verdict, COUNT(*) AS convictions
                FROM escape_verdicts AS outcome
                JOIN build_verdicts AS build USING (build_id)
                WHERE {WINDOW}{conditions}
                GROUP BY outcome.verdict''',
            parameters,
        )
    }
    known = {verdict: counted.get(verdict, 0) for verdict in VERDICTS}
    unrecognised = {verdict: count for verdict, count in counted.items() if verdict not in VERDICTS}
    return known, unrecognised


def by_verdict(connection: sqlite3.Connection, since: int, until: int, suite: Optional[str] = None,
               builders: tuple = ()) -> dict:
    """Stored verdicts per bucket, including buckets nothing reached, so a zero reads as a zero.

    Only the buckets this code knows: `tally` is what reports the rest.
    """
    return _counted_verdicts(connection, since, until, suite, builders)[0]


def tally(connection: sqlite3.Connection, since: int, until: int, suite: Optional[str] = None,
          builders: tuple = ()) -> 'Tally':
    known, unrecognised = _counted_verdicts(connection, since, until, suite, builders)
    return Tally(
        by_verdict=known,
        unaskable=unaskable(connection, since, until, suite=suite, builders=builders),
        unrecognised=unrecognised,
    )


def _verdict_scope(verdicts: object) -> tuple:
    """`(fragment, parameters)` narrowing a query to one category's stored verdicts.

    Takes a tuple of names or a single name, so a caller asking for one stored verdict does not have
    to wrap it. The bind names are generated here and cannot collide with the ones `filters` binds for
    a reader's own clauses, which are all prefixed `filter`.
    """
    names = (verdicts,) if isinstance(verdicts, str) else tuple(verdicts)
    parameters = {f'verdict{index}': name for index, name in enumerate(names)}
    placeholders = ', '.join(f':{bind}' for bind in parameters)
    return f'outcome.verdict IN ({placeholders})', parameters


def escape_subcategories(connection: sqlite3.Connection, since: int, until: int,
                         suite: Optional[str] = None,
                         builders: tuple = ()) -> Subcategories:
    """How the window's escapes split by what main did before, whether the landing measurably worsened
    the test, and what main is doing now — and how many distinct tests they name.

    Over the whole merged bucket, both stored verdicts, because a split counted over less than the
    category it sits under would print three partitions of a number that is not the one above them.

    Counted in Python off the stored counts, through the same two functions the sentences use, so a
    bucket on the page cannot disagree with the sentence a reader opens under it. The baseline split
    reads the stored verdict instead, which is the observation itself rather than a second reading of
    it.
    """
    conditions, parameters = _filters(suite, builders)
    scope, bound = _verdict_scope(MERGED_ESCAPE_VERDICTS)
    parameters.update(bound)
    parameters.update({'since': since, 'until': until})
    counted: Counter = Counter()
    tests: set = set()
    significant_tests: set = set()
    for row in connection.execute(
            f'''SELECT outcome.test_name, outcome.verdict, outcome.runs_before,
                       outcome.failed_before, outcome.runs_after,
                       outcome.failed_after, outcome.recent_runs, outcome.recent_failed,
                       outcome.recent_checked_at
                FROM escape_verdicts AS outcome
                JOIN build_verdicts AS build USING (build_id)
                WHERE {scope} AND {WINDOW}{conditions}''',
            parameters,
    ):
        tests.add(row['test_name'])
        counted[currency_for_counts(row['recent_runs'], row['recent_failed'],
                                    row['recent_checked_at'])] += 1
        counted[BASELINE_FAILING if row['verdict'] == FAILS_ON_MAIN else BASELINE_CLEAN] += 1
        significance = significance_for_counts(row['runs_before'], row['failed_before'],
                                               row['runs_after'], row['failed_after'])
        if significance is not None:
            counted[significance] += 1
        if significance == SIGNIFICANT:
            significant_tests.add(row['test_name'])
    return Subcategories(still_failing=counted[STILL_FAILING], recovered=counted[RECOVERED],
                         not_run_lately=counted[NOT_RUN_LATELY], unchecked=counted[UNCHECKED],
                         significant=counted[SIGNIFICANT], not_significant=counted[NOT_SIGNIFICANT],
                         baseline_clean=counted[BASELINE_CLEAN],
                         baseline_failing=counted[BASELINE_FAILING],
                         distinct_tests=len(tests), significant_tests=len(significant_tests))


@dataclass(frozen=True)
class ConvictionPage:
    """One page of the convictions behind a verdict's count, and where in the whole set it sits.

    `total` is every conviction the query matched, not the page's own length, so a page can say how
    many remain rather than stopping at its last row and leaving a reader to guess. `offset` is the
    page's own start after clamping, which is what a link back to this page has to carry: a `page`
    argument past the end is answered with the last page rather than an empty table, and a reader
    whose URL said page 9 of 3 must not be handed links built on the 9.

    Indices are 1-based because they are read as prose ("201 to 349 of 349"), and `first`/`last` are
    both 0 on an empty page so neither reads as a row that is not there.
    """

    convictions: list
    total: int
    limit: int
    offset: int

    @property
    def shown(self) -> int:
        return len(self.convictions)

    @property
    def truncated(self) -> bool:
        return self.total > self.shown

    @property
    def first(self) -> int:
        return self.offset + 1 if self.convictions else 0

    @property
    def last(self) -> int:
        return self.offset + self.shown

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.last)

    @property
    def pages(self) -> int:
        """At least 1, so an empty set is page 1 of 1 rather than page 1 of 0."""
        return max(1, -(-self.total // self.limit))

    @property
    def number(self) -> int:
        return self.offset // self.limit + 1

    @property
    def next_page(self) -> Optional[int]:
        return self.number + 1 if self.remaining else None

    @property
    def previous_page(self) -> Optional[int]:
        return self.number - 1 if self.number > 1 else None


def _page_offset(total: int, limit: int, page: int) -> int:
    """Where a page starts, clamped into the set that exists.

    A page below the first is the first and a page past the last is the last, because the page number
    arrives in a URL as readily as from a link: a reader who narrowed the set while standing on page 3
    is shown the end of the narrowed set, not a blank table they cannot tell from "nothing matched".
    """
    if page < 1 or total <= 0:
        return 0
    return min(page - 1, (total - 1) // limit) * limit


def convictions(connection: sqlite3.Connection, since: int, until: int, verdicts: object,
                suite: Optional[str] = None, builders: tuple = (),
                limit: int = ESCAPES_LISTED, page: int = 1, conditions: tuple = (),
                sort_keys: tuple = ()) -> 'ConvictionPage':
    """One page of the individual convictions behind one category's count, in the order asked for.

    `verdicts` is a tuple of stored verdict names, or one name: the ESCAPED category lists two stored
    verdicts and every other category lists one, and `category_verdicts` is what turns a category into
    the tuple. A reader's own `verdict` clause still applies on top of it, which is how either half of
    the merged bucket stays reachable on its own.

    `conditions` and `sort_keys` come from `filters`, which owns every column name, operator and
    expression a request can reach: nothing a reader typed is spelled into this SQL, only bound to it.
    The rate-increase and damage columns are ordered and narrowed through the sqlite functions
    `db.connect` registers from `SQL_FUNCTIONS`, so the figure a page sorts by is the one it prints.

    `page` is a number rather than an offset so the arithmetic lives here, where `limit` is known: a
    caller that computed its own offset against a different limit would page over a set of a size the
    query never used.

    Every order ends in `filters.ESCAPES.tiebreak`, which is this table's primary key. LIMIT/OFFSET
    over a partial order drops and duplicates rows across page boundaries, because nothing obliges
    sqlite to break a tie the same way in two queries.
    """
    scoping, parameters = _filters(suite, builders)
    where, having, bound = filters.clause(conditions)
    if having:
        # No ESCAPES column is an aggregate and none takes a `grouped` operator, so `clause` has
        # nothing to put here. This query does not group, and a HAVING attached to it would be read
        # over the whole result as one group; loud rather than silent, for whoever registers the
        # first aggregate column on this table.
        raise ValueError('the escapes listing does not group, so it cannot answer a HAVING clause')
    parameters.update(bound)
    scope, scope_parameters = _verdict_scope(verdicts)
    parameters.update(scope_parameters)
    parameters.update({'since': since, 'until': until})
    narrowing = f' AND {where}' if where else ''
    source = f'''FROM escape_verdicts AS outcome
                JOIN build_verdicts AS build USING (build_id)
                JOIN latest_flakiness_verdicts AS verdict
                  ON verdict.build_id = outcome.build_id AND verdict.test_name = outcome.test_name
                WHERE {scope} AND {WINDOW}{scoping}{narrowing}'''
    # Counted through the same WHERE as the rows, and without the row query's correlated subqueries,
    # so the two cannot disagree about the set this page was taken from.
    total = connection.execute(f'SELECT COUNT(*) {source}', parameters).fetchone()[0]
    size = max(1, limit)
    offset = _page_offset(total, size, page)
    parameters.update({'limit': size, 'offset': offset})
    listed = [
        Conviction(
            test_name=row['test_name'],
            rule=row['rule'],
            verdict=row['verdict'],
            build_id=row['build_id'],
            builder=row['builder'],
            builder_id=row['builder_id'],
            build_number=row['build_number'],
            pr_id=row['pr_id'],
            configuration=results.Configuration.of_build(row),
            runs_before=row['runs_before'],
            failed_before=row['failed_before'],
            runs_after=row['runs_after'],
            failed_after=row['failed_after'],
            landed_at=row['landed_at'],
            window_ends_at=row['window_ends_at'],
            tested_sha=row['tested_sha'],
            newest_sha=row['newest_sha'],
            heads=row['heads'],
            builds=row['builds'],
            recent_runs=row['recent_runs'],
            recent_failed=row['recent_failed'],
            recent_checked_at=row['recent_checked_at'],
        )
        for row in connection.execute(
            f'''SELECT outcome.*, verdict.rule, build.builder, build.builder_id,
                       build.build_number, build.pr_id, build.suite, build.platform,
                       build.style, build.flavor, build.sha AS tested_sha,
                       (SELECT newer.sha FROM build_verdicts AS newer
                         WHERE newer.pr_id = build.pr_id AND newer.sha IS NOT NULL
                           AND outcome.landed_at IS NOT NULL AND newer.started_at <= outcome.landed_at
                         ORDER BY newer.started_at DESC, newer.build_id DESC LIMIT 1) AS newest_sha,
                       (SELECT COUNT(DISTINCT other.sha) FROM build_verdicts AS other
                         WHERE other.pr_id = build.pr_id AND other.sha IS NOT NULL) AS heads,
                       (SELECT COUNT(*) FROM build_verdicts AS other
                         WHERE other.pr_id = build.pr_id) AS builds
                {source}
                ORDER BY {filters.order_by(filters.ESCAPES, sort_keys)}
                LIMIT :limit OFFSET :offset''',
            parameters,
        )
    ]
    return ConvictionPage(convictions=listed, total=total, limit=size, offset=offset)


@dataclass(frozen=True)
class Part:
    """One run of a verdict's sentence, and whether a page should emphasise it.

    The counts are what a reader is looking for in the prose, and they carry a test name beside
    them, so the emphasis travels as data and the template is what turns it into markup.
    """

    text: str
    emphasis: bool = False


def _emphasised(text: str) -> Part:
    return Part(text, emphasis=True)


def _diverged_sentence(conviction: Conviction) -> 'tuple[Part, ...]':
    """What the heads say, with each piece dropped rather than rendered when it was never stored: a
    build ingested before `github.head.sha` was recorded has no head to name, and a row with no
    `landed_at` has no sha to name it landed as."""
    convicted = (f'Convicted on head {conviction.tested_sha[:8]}' if conviction.tested_sha
                 else 'Convicted on a head this build did not record')
    subject = f'PR {conviction.pr_id}' if conviction.pr_id is not None else 'the pull request'
    landed = f' and landed as {conviction.newest_sha[:8]}' if conviction.newest_sha else ''
    return (
        Part(f'{convicted}, but {subject} was built '),
        _emphasised(f'{conviction.builds} times'),
        Part(' across '),
        _emphasised(f'{conviction.heads} heads'),
        Part(f'{landed}.'),
    )


def _currency_clause(conviction: Conviction) -> 'tuple[Part, ...]':
    """What main is doing with the test now, or nothing at all when nobody has asked.

    An unchecked escape gets no clause rather than a hedged one: a sentence that mentions the last
    week at all implies main was asked about it.
    """
    state = conviction.currency
    if state == STILL_FAILING:
        return (
            Part(' Main is still failing it, '),
            _emphasised(f'{conviction.recent_failed} of {conviction.recent_runs}'),
            Part(f' runs in the last {config.CURRENCY_DAYS} days.'),
        )
    if state == RECOVERED:
        return (
            Part(' Main has stopped failing it: none of its '),
            _emphasised(f'{conviction.recent_runs} runs'),
            Part(f' in the last {config.CURRENCY_DAYS} days did.'),
        )
    if state == NOT_RUN_LATELY:
        return (
            Part(f' Main has not run it in the last {config.CURRENCY_DAYS} days, so whether the '
                 'failure is still there is unmeasured.'),
        )
    return ()


def _significance_clause(conviction: Conviction) -> Part:
    """Whether the landing measurably worsened the test, which is the split this bucket is read by.

    One clause for both halves of the merged bucket, because it is the same question of both and the
    counts either side are what answers it. A row the bound cannot be taken on says the question is
    unanswerable rather than saying no: no row in this bucket reaches that, and silence there would
    read as a landing shown to have changed nothing.
    """
    significance = conviction.significance
    if significance == SIGNIFICANT:
        return Part(' The landing measurably worsened it.')
    if significance == NOT_SIGNIFICANT:
        return Part(' The landing did not measurably worsen it.')
    return Part(' Whether the landing worsened it cannot be measured from these runs.')


def _escaped_sentence(conviction: Conviction) -> 'tuple[Part, ...]':
    """The counts behind the escape, whether the landing worsened the test, and what main is doing with
    it now."""
    return (
        Part('Main failed it '),
        _emphasised(f'{conviction.failed_after} of {conviction.runs_after}'),
        Part(' runs after the landing, having never failed it in the '),
        _emphasised(str(conviction.runs_before)),
        Part(' runs before.'),
        _significance_clause(conviction),
    ) + _currency_clause(conviction)


def sentence(conviction: Conviction) -> 'tuple[Part, ...]':
    """Why this conviction reached the verdict it did, in the counts main was asked for."""
    if conviction.verdict == FAILS_ON_MAIN:
        # The counts on both sides, and the one conclusion the counts do support. This row sits in the
        # same bucket as an ESCAPED one now, and the old tail ("main's failure, not this change's")
        # was the knife-edge reading the fold exists to stop making: one failure in a long clean
        # baseline is not grounds for telling a reader whose failure it is. What the two count pairs
        # do answer is whether the landing made the test measurably worse, so that is what is said.
        return (
            Part('Main failed it '),
            _emphasised(f'{conviction.failed_after} of {conviction.runs_after}'),
            Part(' runs '),
            _emphasised('after'),
            Part(' the landing, and '),
            _emphasised(f'{conviction.failed_before} of {conviction.runs_before}'),
            Part(' before it.'),
            _significance_clause(conviction),
        ) + _currency_clause(conviction)
    if conviction.verdict == CONTAINED:
        return (
            Part('Main ran it '),
            _emphasised(f'{conviction.runs_after} times'),
            Part(' after the landing and never failed it.'),
        )
    if conviction.verdict == NO_RUNS:
        return (Part(f'No bot ran it on main in the {config.ESCAPE_WINDOW_DAYS} days after the '
                     'landing, so there is nothing to compare against.'),)
    if conviction.verdict == NO_BASELINE:
        return (
            Part('Main failed it '),
            _emphasised(f'{conviction.failed_after} of {conviction.runs_after}'),
            Part(' runs after the landing, but nothing ran it in the '
                 f'{config.ESCAPE_WINDOW_DAYS} days before, so a regression cannot be told from a '
                 'failure main already had.'),
        )
    if conviction.verdict == TREE_DIVERGED:
        return _diverged_sentence(conviction)
    return _escaped_sentence(conviction)
