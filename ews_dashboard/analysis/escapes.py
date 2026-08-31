"""What main did with a test after the change EWS told an author was not to blame for it.

Every other number in this dashboard is a floor, because main does not contain the change and a test
that is reliable on main and genuinely broken by the change cannot be told apart from one that flaked
during the build. Once the change lands, it can: the test now runs on main with the change in it, so
a conviction that was wrong shows up as main failing the test it excused.

One convicted test in one build, whose pull request landed as a known commit on main, falls in one
bucket:

  ESCAPED          main failed it unexpectedly in ESCAPE_FAILURE_PCT or more of the runs after the
                   landing, having failed it under that share before: the conviction excused a real
                   regression
  FAILS_ON_MAIN    main fails it without the change, either flaking after the landing or already
                   failing it before, so the conviction was corroborated
  CONTAINED        no unexpected failure on main after the landing
  NO_RUNS          nothing ran the test on main in the window after the landing
  NO_BASELINE      it failed after the landing, but nothing ran before it, so a regression cannot be
                   told from a failure main already had
  TREE_DIVERGED    a later build of the same pull request tested a different head, so what landed is
                   not what this conviction was made on

Only an unexpected failure counts. A test main already lists as failing is failing to order, and
counting it would convict every rule of an escape it had nothing to do with.

What this cannot do: it sees only pull requests whose landing `webkit_checkout` could pin down, only
the window either side of the landing, and only tests some bot on main actually runs in the same
configuration. ESCAPED is therefore a floor as well, and the buckets that answer nothing are counted
and shown rather than dropped.
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from ews_dashboard import config, results, webkit_checkout

ESCAPED = 'ESCAPED'
FAILS_ON_MAIN = 'FAILS_ON_MAIN'
CONTAINED = 'CONTAINED'
NO_RUNS = 'NO_RUNS'
NO_BASELINE = 'NO_BASELINE'
TREE_DIVERGED = 'TREE_DIVERGED'

VERDICTS = (ESCAPED, FAILS_ON_MAIN, CONTAINED, NO_RUNS, NO_BASELINE, TREE_DIVERGED)

# What answers nothing about the conviction, so it belongs in no rate.
UNDECIDED_VERDICTS = (NO_RUNS, NO_BASELINE, TREE_DIVERGED)

VERDICT_DESCRIPTIONS = {
    ESCAPED: 'After the landing main started failing this and kept failing it, so the conviction '
             'excused a real regression.',
    FAILS_ON_MAIN: 'Main fails this without the change, either flaking after the landing or '
                   'already failing it before, so the build was told the truth.',
    CONTAINED: 'After the landing main did not fail this.',
    NO_RUNS: 'No bot ran this on main in the window after the change landed.',
    NO_BASELINE: 'It failed on main after the change landed, but nothing ran it before, so a '
                 'regression cannot be told from a failure main already had.',
    TREE_DIVERGED: 'This conviction was made on a version of the pull request that a later build '
                   'superseded, so the code that landed is not the code it was made on and main '
                   'cannot grade it.',
}

# The pull requests a conviction cannot even be looked for on, counted from `landings` rather than
# stored, since a pull request that has not landed yet is the ordinary case and not an answer.
NOT_LANDED = 'not_landed'
AMBIGUOUS = 'ambiguous'
UNRESOLVED = 'unresolved'
UNAVAILABLE = 'unavailable'

ESCAPE_WINDOW_SECONDS = config.ESCAPE_WINDOW_DAYS * 86400

# One escape is worth reading about individually, so the list is long before it is cut.
ESCAPES_LISTED = 200

WINDOW = 'build.started_at >= :since AND build.started_at < :until'


def _filters(suite: Optional[str], builder: Optional[str]) -> tuple:
    """Extra WHERE clauses for a query with build_verdicts aliased as `build`, and their parameters,
    returned together so a caller cannot bind one without the other."""
    conditions, parameters = '', {}
    if suite is not None:
        conditions += ' AND build.suite = :suite'
        parameters['suite'] = suite
    if builder is not None:
        conditions += ' AND build.builder = :builder'
        parameters['builder'] = builder
    return conditions, parameters


@dataclass(frozen=True)
class Conviction:
    """One conviction main answered, and everything a reader needs to go and check the answer.

    `heads` and `builds` count the pull request's builds rather than this one's, which is what says
    how far the code that landed had moved from the code convicted here.
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
    window_ends_at: int
    tested_sha: Optional[str]
    newest_sha: Optional[str]
    heads: int
    builds: int

    @property
    def landed_at(self) -> int:
        return self.window_ends_at - ESCAPE_WINDOW_SECONDS


@dataclass(frozen=True)
class Tally:
    """What a window's convictions came to: what main answered, and what it could not be asked."""

    by_verdict: dict
    unaskable: dict

    @property
    def asked(self) -> int:
        """Every conviction main was asked about, answered or not, which is what the buckets divide
        up."""
        return sum(self.by_verdict.values())

    @property
    def decided(self) -> int:
        """Convictions main gave an answer about, which is the only honest denominator here."""
        return sum(count for verdict, count in self.by_verdict.items()
                   if verdict not in UNDECIDED_VERDICTS)

    @property
    def escaped(self) -> int:
        return self.by_verdict.get(ESCAPED, 0)

    @property
    def escape_rate_pct(self) -> Optional[float]:
        if not self.decided:
            return None
        return round(100.0 * self.escaped / self.decided, 1)

    @property
    def undecided(self) -> int:
        return sum(self.by_verdict.get(verdict, 0) for verdict in UNDECIDED_VERDICTS)

    @property
    def unasked(self) -> int:
        return sum(self.unaskable.values())


@dataclass(frozen=True)
class Candidate:
    """One convicted test whose pull request landed, and the two things that bound the check.

    `newest_sha` is the head of the newest build of the same pull request, which is what decides
    whether the conviction was made on the code that landed.
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


def decide(runs_before: list, runs_after: list) -> Verdict:
    """Which bucket the runs put the conviction in.

    Pure, and the whole of the judgement: everything else here fetches, stores or counts.
    """
    failed_before = [run for run in runs_before if run.unexpected]
    failed_after = [run for run in runs_after if run.unexpected]
    counts = dict(runs_before=len(runs_before), failed_before=len(failed_before),
                  runs_after=len(runs_after), failed_after=len(failed_after))

    if not runs_after:
        return Verdict(NO_RUNS, **counts)
    if not failed_after:
        # A clean window after the landing needs no baseline: nothing failed, so nothing escaped.
        return Verdict(CONTAINED, **counts)
    if not runs_before:
        return Verdict(NO_BASELINE, **counts)
    after_pct = 100.0 * len(failed_after) / len(runs_after)
    before_pct = 100.0 * len(failed_before) / len(runs_before)
    if after_pct >= config.ESCAPE_FAILURE_PCT and before_pct < config.ESCAPE_FAILURE_PCT:
        return Verdict(ESCAPED, **counts)
    return Verdict(FAILS_ON_MAIN, **counts)


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
    with connection:
        connection.execute(
            '''INSERT OR REPLACE INTO escape_verdicts (
                build_id, test_name, verdict, runs_before, failed_before, runs_after,
                failed_after, window_ends_at, decided_at
            ) VALUES (?,?,?,?,?,?,?,?,?)''',
            (candidate.build_id, candidate.test_name, verdict.verdict, verdict.runs_before,
             verdict.failed_before, verdict.runs_after, verdict.failed_after,
             candidate.window_ends_at, int(time.time())),
        )


def assess(connection: sqlite3.Connection, history: results.History, since: int,
           until: int) -> Counter:
    """Decide every convicted test in the window that main can be asked about, and store the answers.

    Returns a count per verdict, plus `unavailable` for the convictions results.webkit.org could not
    be reached about. Nothing is stored for those, so the next pass asks again.
    """
    outcomes: Counter = Counter()
    for candidate in candidates(connection, since, until):
        stored = _stored(connection, candidate)
        if stored is not None:
            outcomes[stored.verdict] += 1
            continue
        try:
            verdict = assess_one(history, candidate)
        except results.HistoryUnavailable:
            outcomes[UNAVAILABLE] += 1
            continue
        _store(connection, candidate, verdict)
        outcomes[verdict.verdict] += 1
    return outcomes


def unaskable(connection: sqlite3.Connection, since: int, until: int, suite: Optional[str] = None,
              builder: Optional[str] = None) -> dict:
    """Convictions in the window that main cannot be asked about, by why not.

    Read from the database alone and never stored, because every one of these is a pull request that
    may land, or be resolved, before the next pass.
    """
    conditions, parameters = _filters(suite, builder)
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


def by_verdict(connection: sqlite3.Connection, since: int, until: int, suite: Optional[str] = None,
               builder: Optional[str] = None) -> dict:
    """Stored verdicts per bucket, including buckets nothing reached, so a zero reads as a zero."""
    conditions, parameters = _filters(suite, builder)
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
    return {verdict: counted.get(verdict, 0) for verdict in VERDICTS}


def tally(connection: sqlite3.Connection, since: int, until: int, suite: Optional[str] = None,
          builder: Optional[str] = None) -> 'Tally':
    return Tally(
        by_verdict=by_verdict(connection, since, until, suite=suite, builder=builder),
        unaskable=unaskable(connection, since, until, suite=suite, builder=builder),
    )


def convictions(connection: sqlite3.Connection, since: int, until: int, verdict: str,
                suite: Optional[str] = None, builder: Optional[str] = None,
                limit: int = ESCAPES_LISTED) -> 'list[Conviction]':
    """The individual convictions behind one verdict's count, newest landing first."""
    conditions, parameters = _filters(suite, builder)
    parameters.update({'since': since, 'until': until, 'verdict': verdict, 'limit': limit})
    return [
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
            window_ends_at=row['window_ends_at'],
            tested_sha=row['tested_sha'],
            newest_sha=row['newest_sha'],
            heads=row['heads'],
            builds=row['builds'],
        )
        for row in connection.execute(
            f'''SELECT outcome.*, verdict.rule, build.builder, build.builder_id,
                       build.build_number, build.pr_id, build.suite, build.platform,
                       build.style, build.flavor, build.sha AS tested_sha,
                       (SELECT newer.sha FROM build_verdicts AS newer
                         WHERE newer.pr_id = build.pr_id AND newer.sha IS NOT NULL
                         ORDER BY newer.started_at DESC, newer.build_id DESC LIMIT 1) AS newest_sha,
                       (SELECT COUNT(DISTINCT other.sha) FROM build_verdicts AS other
                         WHERE other.pr_id = build.pr_id AND other.sha IS NOT NULL) AS heads,
                       (SELECT COUNT(*) FROM build_verdicts AS other
                         WHERE other.pr_id = build.pr_id) AS builds
                FROM escape_verdicts AS outcome
                JOIN build_verdicts AS build USING (build_id)
                JOIN latest_flakiness_verdicts AS verdict
                  ON verdict.build_id = outcome.build_id AND verdict.test_name = outcome.test_name
                WHERE outcome.verdict = :verdict AND {WINDOW}{conditions}
                ORDER BY outcome.window_ends_at DESC
                LIMIT :limit''',
            parameters,
        )
    ]


def _diverged_sentence(conviction: Conviction) -> str:
    """What the heads say, with each piece dropped rather than rendered when it was never stored: a
    build ingested before `github.head.sha` was recorded has no head to name."""
    convicted = (f'Convicted on head {conviction.tested_sha[:8]}' if conviction.tested_sha
                 else 'Convicted on a head this build did not record')
    subject = f'PR {conviction.pr_id}' if conviction.pr_id is not None else 'the pull request'
    landed = f' and landed as {conviction.newest_sha[:8]}' if conviction.newest_sha else ''
    return (f'{convicted}, but {subject} was built {conviction.builds} times across '
            f'{conviction.heads} heads{landed}.')


def sentence(conviction: Conviction) -> str:
    """Why this conviction reached the verdict it did, in the counts main was asked for."""
    if conviction.verdict == FAILS_ON_MAIN:
        return (f'Main failed it {conviction.failed_after} of {conviction.runs_after} runs after '
                f'the landing against {conviction.failed_before} of {conviction.runs_before} '
                'before, so the failure is main\'s and not this change\'s.')
    if conviction.verdict == CONTAINED:
        return f'Main ran it {conviction.runs_after} times after the landing and never failed it.'
    if conviction.verdict == NO_RUNS:
        return (f'No bot ran it on main in the {config.ESCAPE_WINDOW_DAYS} days after the landing, '
                'so there is nothing to compare against.')
    if conviction.verdict == NO_BASELINE:
        return (f'Main failed it {conviction.failed_after} of {conviction.runs_after} runs after '
                f'the landing, but nothing ran it in the {config.ESCAPE_WINDOW_DAYS} days before, '
                'so a regression cannot be told from a failure main already had.')
    if conviction.verdict == TREE_DIVERGED:
        return _diverged_sentence(conviction)
    return (f'Main failed it {conviction.failed_after} of {conviction.runs_after} runs after the '
            f'landing, having failed it {conviction.failed_before} of {conviction.runs_before} '
            f'before, under the {config.ESCAPE_FAILURE_PCT}% a regression needs.')
