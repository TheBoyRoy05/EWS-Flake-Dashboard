"""The escape check: what main did with a convicted test after the change landed."""

from __future__ import annotations

import time

from ews_dashboard import config, results
from ews_dashboard.analysis import escapes
from tests import fixtures

LANDED_AT = fixtures.DEFAULT_BUILD_TIME + 86400
DAY = 86400
PULL_REQUEST = 12345
TEST = 'fast/a.html'


def _candidate(landed_at: int = LANDED_AT, tested_sha: str = 'a' * 40,
               newest_sha: str = 'a' * 40) -> escapes.Candidate:
    return escapes.Candidate(
        build_id=1, test_name=TEST, rule=config.CLEAN_TREE,
        configuration=results.Configuration(suite='layout-tests', platform='mac', style='release'),
        pr_id=PULL_REQUEST, landed_at=landed_at, tested_sha=tested_sha, newest_sha=newest_sha,
    )


class TestDecide(fixtures.DatabaseTest):
    """The judgement itself, over the runs either side of a landing."""

    def test_a_test_that_keeps_failing_after_the_landing_escaped(self) -> None:
        verdict = escapes.decide(
            [fixtures.run(commit_at=LANDED_AT - DAY)],
            [fixtures.run('TEXT', commit_at=LANDED_AT), fixtures.run('TEXT', commit_at=LANDED_AT + 60)],
        )
        self.assertEqual(verdict.verdict, escapes.ESCAPED)
        self.assertEqual((verdict.runs_after, verdict.failed_after), (2, 2))

    def test_a_test_failing_less_often_than_the_threshold_is_flaky_on_main(self) -> None:
        watched = [fixtures.run('TEXT', commit_at=LANDED_AT)]
        watched += [fixtures.run(commit_at=LANDED_AT + minute * 60) for minute in range(1, 5)]
        verdict = escapes.decide([fixtures.run(commit_at=LANDED_AT - DAY)], watched)
        self.assertEqual(verdict.verdict, escapes.FLAKY_ON_MAIN)
        self.assertEqual((verdict.runs_after, verdict.failed_after), (5, 1))

    def test_a_clean_window_after_the_landing_is_contained(self) -> None:
        verdict = escapes.decide([fixtures.run(commit_at=LANDED_AT - DAY)],
                                 [fixtures.run(commit_at=LANDED_AT)])
        self.assertEqual(verdict.verdict, escapes.CONTAINED)

    def test_a_clean_window_needs_no_baseline_to_be_contained(self) -> None:
        """Nothing failed after the landing, so nothing escaped, whatever main did before it."""
        self.assertEqual(escapes.decide([], [fixtures.run(commit_at=LANDED_AT)]).verdict,
                         escapes.CONTAINED)

    def test_a_test_main_was_already_failing_is_not_an_escape(self) -> None:
        verdict = escapes.decide([fixtures.run('TEXT', commit_at=LANDED_AT - DAY)],
                                 [fixtures.run('TEXT', commit_at=LANDED_AT)])
        self.assertEqual(verdict.verdict, escapes.ALREADY_FAILING)

    def test_a_failure_with_nothing_before_it_is_disclosed_rather_than_called_an_escape(self) -> None:
        verdict = escapes.decide([], [fixtures.run('TEXT', commit_at=LANDED_AT)])
        self.assertEqual(verdict.verdict, escapes.NO_BASELINE)

    def test_an_empty_window_after_the_landing_answers_nothing(self) -> None:
        verdict = escapes.decide([fixtures.run(commit_at=LANDED_AT - DAY)], [])
        self.assertEqual(verdict.verdict, escapes.NO_RUNS)

    def test_a_failure_main_expects_is_not_a_failure(self) -> None:
        """An expected failure is main failing to order, so counting it would convict every rule of
        an escape it had nothing to do with."""
        verdict = escapes.decide(
            [fixtures.run(commit_at=LANDED_AT - DAY)],
            [fixtures.run('TEXT', expected='PASS TEXT', commit_at=LANDED_AT)],
        )
        self.assertEqual(verdict.verdict, escapes.CONTAINED)


class TestAssessOne(fixtures.DatabaseTest):
    def test_the_landing_commit_belongs_to_the_window_after_it_and_not_to_the_baseline(self) -> None:
        """The endpoint's bounds are not trusted to exclude their endpoints, so a run of the landing
        commit itself must not answer as the baseline it is compared against."""
        history = fixtures.StubRunHistory({TEST: [fixtures.run('TEXT', commit_at=LANDED_AT)]})
        verdict = escapes.assess_one(history, _candidate())
        self.assertEqual(verdict.verdict, escapes.NO_BASELINE)
        self.assertEqual((verdict.runs_before, verdict.runs_after), (0, 1))

    def test_both_windows_span_the_configured_number_of_days(self) -> None:
        history = fixtures.StubRunHistory({TEST: []})
        escapes.assess_one(history, _candidate())
        self.assertEqual(
            [(query.after, query.before) for query in history.queries],
            [(LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS, LANDED_AT),
             (LANDED_AT, LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS)],
        )

    def test_a_pull_request_that_moved_after_the_conviction_is_not_asked_about(self) -> None:
        """A later build tested a different head, so main holds code this conviction was never made
        on and neither answer would be about it."""
        history = fixtures.StubRunHistory({TEST: [fixtures.run('TEXT', commit_at=LANDED_AT)]})
        verdict = escapes.assess_one(history, _candidate(tested_sha='a' * 40, newest_sha='b' * 40))
        self.assertEqual(verdict.verdict, escapes.TREE_DIVERGED)
        self.assertEqual(history.queries, [])


class TestAssess(fixtures.DatabaseTest):
    """The stored pass over a window of convictions."""

    def _convict(self, number: int = 1, started_at: int = fixtures.DEFAULT_BUILD_TIME,
                 sha: str = 'a' * 40) -> int:
        return self.store_build(number, flaky={TEST: config.CLEAN_TREE}, pr_id=PULL_REQUEST,
                                pr_title='A change that landed', sha=sha, started_at=started_at)

    def _assess(self, history: fixtures.StubRunHistory) -> dict:
        return dict(escapes.assess(self.connection, history, fixtures.DEFAULT_BUILD_TIME - DAY,
                                   fixtures.DEFAULT_BUILD_TIME + DAY))

    def test_a_conviction_whose_pull_request_landed_is_decided_and_stored(self) -> None:
        self._convict()
        self.store_landing(PULL_REQUEST, landed_at=LANDED_AT)
        history = fixtures.StubRunHistory({TEST: [
            fixtures.run(commit_at=LANDED_AT - DAY),
            fixtures.run('TEXT', commit_at=LANDED_AT),
        ]})
        self.assertEqual(self._assess(history), {escapes.ESCAPED: 1})
        stored = self.connection.execute('SELECT * FROM escape_verdicts').fetchall()
        self.assertEqual([(row['test_name'], row['verdict']) for row in stored],
                         [(TEST, escapes.ESCAPED)])

    def test_a_settled_verdict_is_not_asked_about_again(self) -> None:
        self._convict()
        self.store_landing(PULL_REQUEST, landed_at=LANDED_AT)
        history = fixtures.StubRunHistory({TEST: [fixtures.run(commit_at=LANDED_AT)]})
        self._assess(history)
        asked = len(history.queries)
        self.assertEqual(self._assess(history), {escapes.CONTAINED: 1})
        self.assertEqual(len(history.queries), asked)

    def test_a_verdict_reached_before_its_window_closed_is_asked_again(self) -> None:
        """The runs that turn CONTAINED into ESCAPED arrive after the window's last commit, so a
        verdict decided while it was still filling cannot be kept."""
        self._convict()
        self.store_landing(PULL_REQUEST, landed_at=int(time.time()) - 60)
        history = fixtures.StubRunHistory({TEST: []})
        self._assess(history)
        asked = len(history.queries)
        self._assess(history)
        self.assertGreater(len(history.queries), asked)

    def test_a_conviction_on_a_pull_request_with_no_landing_reaches_no_network(self) -> None:
        self._convict()
        history = fixtures.StubRunHistory({TEST: [fixtures.run('TEXT', commit_at=LANDED_AT)]})
        self.assertEqual(self._assess(history), {})
        self.assertEqual(history.queries, [])

    def test_an_ambiguous_title_is_not_asked_about(self) -> None:
        self._convict()
        self.store_landing(PULL_REQUEST, status='ambiguous', matches=14)
        history = fixtures.StubRunHistory({TEST: [fixtures.run('TEXT', commit_at=LANDED_AT)]})
        self.assertEqual(self._assess(history), {})
        self.assertEqual(history.queries, [])

    def test_an_unreachable_results_service_is_counted_and_not_stored(self) -> None:
        self._convict()
        self.store_landing(PULL_REQUEST, landed_at=LANDED_AT)
        history = fixtures.StubRunHistory({}, unavailable={TEST})
        self.assertEqual(self._assess(history), {escapes.UNAVAILABLE: 1})
        self.assertEqual(self.connection.execute(
            'SELECT COUNT(*) FROM escape_verdicts').fetchone()[0], 0)

    def test_a_conviction_made_on_a_head_a_later_build_replaced_is_not_asked_about(self) -> None:
        self._convict(number=1, sha='a' * 40, started_at=fixtures.DEFAULT_BUILD_TIME)
        self._convict(number=2, sha='b' * 40, started_at=fixtures.DEFAULT_BUILD_TIME + 600)
        self.store_landing(PULL_REQUEST, landed_at=LANDED_AT)
        history = fixtures.StubRunHistory({TEST: [
            fixtures.run(commit_at=LANDED_AT - DAY),
            fixtures.run('TEXT', commit_at=LANDED_AT),
        ]})
        self.assertEqual(self._assess(history),
                         {escapes.TREE_DIVERGED: 1, escapes.ESCAPED: 1})


class TestUnaskable(fixtures.DatabaseTest):
    def test_convictions_main_cannot_be_asked_about_are_counted_by_why_not(self) -> None:
        self.store_build(1, flaky={TEST: config.CLEAN_TREE}, pr_id=1, pr_title='One')
        self.store_build(2, flaky={TEST: config.CLEAN_TREE}, pr_id=2, pr_title='Two')
        self.store_build(3, flaky={TEST: config.CLEAN_TREE}, pr_id=3, pr_title='Three')
        self.store_landing(2, status='not_landed', matches=0)
        self.store_landing(3, status='ambiguous', matches=9)
        self.assertEqual(
            escapes.unaskable(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                              fixtures.DEFAULT_BUILD_TIME + DAY),
            {escapes.NOT_LANDED: 1, escapes.AMBIGUOUS: 1, escapes.UNRESOLVED: 1},
        )

    def test_a_conviction_that_was_asked_about_is_not_counted_as_unaskable(self) -> None:
        self.store_build(1, flaky={TEST: config.CLEAN_TREE}, pr_id=1, pr_title='One')
        self.store_landing(1, landed_at=LANDED_AT)
        self.assertEqual(
            escapes.unaskable(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                              fixtures.DEFAULT_BUILD_TIME + DAY),
            {escapes.NOT_LANDED: 0, escapes.AMBIGUOUS: 0, escapes.UNRESOLVED: 0},
        )
