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


def _runs(failed: int, total: int, first_at: int) -> list:
    """`failed` unexpected failures and passes for the rest, ten minutes apart from `first_at`."""
    return [fixtures.run('TEXT' if index < failed else 'PASS', commit_at=first_at + index * 600)
            for index in range(total)]


class TestDecide(fixtures.DatabaseTest):
    """The judgement itself, over the runs either side of a landing."""

    def test_a_test_that_keeps_failing_after_the_landing_escaped(self) -> None:
        verdict = escapes.decide(
            [fixtures.run(commit_at=LANDED_AT - DAY)],
            [fixtures.run('TEXT', commit_at=LANDED_AT), fixtures.run('TEXT', commit_at=LANDED_AT + 60)],
        )
        self.assertEqual(verdict.verdict, escapes.ESCAPED)
        self.assertEqual((verdict.runs_after, verdict.failed_after), (2, 2))

    def test_a_test_failing_less_often_than_the_threshold_fails_on_main(self) -> None:
        watched = [fixtures.run('TEXT', commit_at=LANDED_AT)]
        watched += [fixtures.run(commit_at=LANDED_AT + minute * 60) for minute in range(1, 5)]
        verdict = escapes.decide([fixtures.run(commit_at=LANDED_AT - DAY)], watched)
        self.assertEqual(verdict.verdict, escapes.FAILS_ON_MAIN)
        self.assertEqual((verdict.runs_after, verdict.failed_after), (5, 1))

    def test_a_clean_window_after_the_landing_is_contained(self) -> None:
        verdict = escapes.decide([fixtures.run(commit_at=LANDED_AT - DAY)],
                                 [fixtures.run(commit_at=LANDED_AT)])
        self.assertEqual(verdict.verdict, escapes.CONTAINED)

    def test_a_clean_window_needs_no_baseline_to_be_contained(self) -> None:
        """Nothing failed after the landing, so nothing escaped, whatever main did before it."""
        self.assertEqual(escapes.decide([], [fixtures.run(commit_at=LANDED_AT)]).verdict,
                         escapes.CONTAINED)

    def test_a_baseline_as_broken_as_a_regression_is_not_an_escape(self) -> None:
        """Main was failing it in the share a regression needs before the landing, so even a window
        that fails every run after cannot be laid at this change's door."""
        verdict = escapes.decide(_runs(4, 4, LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS),
                                 _runs(4, 4, LANDED_AT))
        self.assertEqual(verdict.verdict, escapes.FAILS_ON_MAIN)
        self.assertEqual((verdict.runs_before, verdict.failed_before), (4, 4))

    def test_a_baseline_exactly_at_the_threshold_is_not_an_escape(self) -> None:
        verdict = escapes.decide(_runs(2, 4, LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS),
                                 _runs(1, 1, LANDED_AT))
        self.assertEqual(verdict.verdict, escapes.FAILS_ON_MAIN)

    def test_a_regression_over_a_baseline_that_only_flaked_is_an_escape(self) -> None:
        """One failure in the baseline is flakiness, not a broken main, so a test that then fails
        every run after the landing is the regression the conviction excused."""
        verdict = escapes.decide(_runs(1, 40, LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS),
                                 _runs(40, 40, LANDED_AT))
        self.assertEqual(verdict.verdict, escapes.ESCAPED)
        self.assertEqual((verdict.failed_before, verdict.failed_after), (1, 40))

    def test_a_baseline_flaking_at_the_rate_it_keeps_after_corroborates_the_build(self) -> None:
        """A test flaking either side of the landing at a similar low rate is exactly the flakiness
        the build was told it was, so it is decided rather than counted nowhere."""
        verdict = escapes.decide(_runs(6, 88, LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS),
                                 _runs(14, 99, LANDED_AT))
        self.assertEqual(verdict.verdict, escapes.FAILS_ON_MAIN)

    def test_an_empty_window_after_the_landing_answers_nothing_whatever_the_baseline(self) -> None:
        """NO_RUNS is decided before the baseline is read, so a broken main cannot mask it."""
        verdict = escapes.decide(_runs(4, 4, LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS), [])
        self.assertEqual(verdict.verdict, escapes.NO_RUNS)

    def test_a_clean_window_is_contained_whatever_the_baseline(self) -> None:
        verdict = escapes.decide(_runs(4, 4, LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS),
                                 [fixtures.run(commit_at=LANDED_AT)])
        self.assertEqual(verdict.verdict, escapes.CONTAINED)

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


class TestTally(fixtures.DatabaseTest):
    """What a window's verdicts come to, and which of them the escape rate is taken over."""

    def test_a_test_main_fails_without_the_change_is_counted_in_the_rate(self) -> None:
        """Main failing a test whether the change is there or not vindicates the conviction, so it
        belongs in the denominator rather than among the convictions main answered nothing about."""
        self.assertNotIn(escapes.FAILS_ON_MAIN, escapes.UNDECIDED_VERDICTS)
        tally = escapes.Tally(
            by_verdict={escapes.ESCAPED: 0, escapes.FAILS_ON_MAIN: 5, escapes.CONTAINED: 39,
                        escapes.NO_RUNS: 2},
            unaskable={},
        )
        self.assertEqual((tally.decided, tally.undecided), (44, 2))
        self.assertEqual(tally.escape_rate_pct, 0.0)


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


def _conviction(verdict: str, **fields: object) -> escapes.Conviction:
    values = dict(
        test_name=TEST, rule=config.CLEAN_TREE, verdict=verdict, build_id=1,
        builder=fixtures.LAYOUT_BUILDER, builder_id=7, build_number=1, pr_id=PULL_REQUEST,
        configuration=results.Configuration(suite='layout-tests', platform='mac', style='release'),
        runs_before=4, failed_before=0, runs_after=6, failed_after=2,
        window_ends_at=LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS,
        tested_sha='a' * 40, newest_sha='b' * 40, heads=2, builds=3,
    )
    values.update(fields)
    return escapes.Conviction(**values)


class TestSentence(fixtures.DatabaseTest):
    """One sentence per verdict, so a count drilled into explains itself without a legend."""

    def test_a_fails_on_main_verdict_reports_both_rates(self) -> None:
        """The rate either side is where a reader now tells a flaky test from a broken one, so both
        have to be in the sentence."""
        self.assertIn('failed it 14 of 99 runs after the landing against 6 of 88 before',
                      escapes.sentence(_conviction(escapes.FAILS_ON_MAIN, runs_before=88,
                                                   failed_before=6, runs_after=99,
                                                   failed_after=14)))

    def test_a_fails_on_main_verdict_reads_the_same_for_a_baseline_main_was_broken_on(self) -> None:
        self.assertIn('failed it 90 of 96 runs after the landing against 90 of 96 before',
                      escapes.sentence(_conviction(escapes.FAILS_ON_MAIN, runs_before=96,
                                                   failed_before=90, runs_after=96,
                                                   failed_after=90)))

    def test_a_contained_verdict_says_main_never_failed_it(self) -> None:
        self.assertIn('ran it 6 times after the landing and never failed it',
                      escapes.sentence(_conviction(escapes.CONTAINED, failed_after=0)))

    def test_a_no_runs_verdict_says_nothing_ran_it(self) -> None:
        self.assertIn(f'No bot ran it on main in the {config.ESCAPE_WINDOW_DAYS} days',
                      escapes.sentence(_conviction(escapes.NO_RUNS, runs_after=0, failed_after=0)))

    def test_a_no_baseline_verdict_says_nothing_ran_before_it(self) -> None:
        self.assertIn(f'nothing ran it in the {config.ESCAPE_WINDOW_DAYS} days before',
                      escapes.sentence(_conviction(escapes.NO_BASELINE, runs_before=0)))

    def test_a_diverged_verdict_names_both_heads_and_how_many_there_were(self) -> None:
        sentence = escapes.sentence(_conviction(escapes.TREE_DIVERGED))
        self.assertIn(f'Convicted on head {"a" * 8}', sentence)
        self.assertIn(f'PR {PULL_REQUEST} was built 3 times across 2 heads', sentence)
        self.assertIn(f'landed as {"b" * 8}', sentence)

    def test_an_escaped_verdict_says_main_started_failing_it(self) -> None:
        self.assertIn('failed it 2 of 6 runs after the landing, having failed it 0 of 4 before, '
                      f'under the {config.ESCAPE_FAILURE_PCT}%',
                      escapes.sentence(_conviction(escapes.ESCAPED)))

    def test_a_diverged_verdict_with_no_head_recorded_omits_it(self) -> None:
        sentence = escapes.sentence(_conviction(escapes.TREE_DIVERGED, tested_sha=None,
                                                newest_sha=None, pr_id=None))
        self.assertNotIn('None', sentence)
        self.assertIn('the pull request was built 3 times', sentence)


class TestConvictions(fixtures.DatabaseTest):
    """The individual convictions behind one verdict's count."""

    def _convict(self, number: int, test_name: str, verdict: str, pr_id: int,
                 builder: str = fixtures.LAYOUT_BUILDER, builder_id: int = 7) -> int:
        build_id = self.store_build(number, flaky={test_name: config.CLEAN_TREE}, pr_id=pr_id,
                                    pr_title='A change that landed', builder=builder,
                                    builder_id=builder_id, sha='a' * 40)
        with self.connection:
            self.connection.execute(
                '''INSERT INTO escape_verdicts (
                    build_id, test_name, verdict, runs_before, failed_before, runs_after,
                    failed_after, window_ends_at, decided_at
                ) VALUES (?,?,?,?,?,?,?,?,?)''',
                (build_id, test_name, verdict, 4, 0, 6, 2,
                 LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS, LANDED_AT),
            )
        return build_id

    def _convictions(self, verdict: str, **scope: object) -> list:
        return escapes.convictions(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                   fixtures.DEFAULT_BUILD_TIME + DAY, verdict, **scope)

    def test_only_the_convictions_with_the_asked_for_verdict_are_listed(self) -> None:
        self._convict(1, TEST, escapes.CONTAINED, pr_id=1)
        self._convict(2, 'fast/b.html', escapes.NO_RUNS, pr_id=2)
        listed = self._convictions(escapes.CONTAINED)
        self.assertEqual([(one.test_name, one.verdict) for one in listed],
                         [(TEST, escapes.CONTAINED)])
        self.assertEqual(listed[0].rule, config.CLEAN_TREE)
        self.assertEqual(listed[0].landed_at, LANDED_AT)

    def test_a_queue_the_page_is_narrowed_to_narrows_the_list_too(self) -> None:
        self._convict(1, TEST, escapes.CONTAINED, pr_id=1)
        self._convict(2, 'fast/b.html', escapes.CONTAINED, pr_id=2,
                      builder=fixtures.GTK_BUILDER, builder_id=9)
        self.assertEqual(
            [one.test_name for one in self._convictions(escapes.CONTAINED,
                                                        builder=fixtures.GTK_BUILDER)],
            ['fast/b.html'],
        )

    def test_a_suite_the_page_is_narrowed_to_narrows_the_list_too(self) -> None:
        self._convict(1, TEST, escapes.CONTAINED, pr_id=1)
        self._convict(2, 'TestWebKitAPI.A.b', escapes.CONTAINED, pr_id=2,
                      builder=fixtures.API_BUILDER, builder_id=8)
        self.assertEqual([one.test_name for one in self._convictions(escapes.CONTAINED,
                                                                     suite='api-tests')],
                         ['TestWebKitAPI.A.b'])

    def test_the_heads_of_the_whole_pull_request_are_carried_not_this_build_s(self) -> None:
        """TREE_DIVERGED's sentence is about how far the pull request moved, which one build cannot
        say."""
        self._convict(1, TEST, escapes.TREE_DIVERGED, pr_id=PULL_REQUEST)
        self.store_build(2, flaky={TEST: config.CLEAN_TREE}, pr_id=PULL_REQUEST,
                         pr_title='A change that landed', sha='b' * 40,
                         started_at=fixtures.DEFAULT_BUILD_TIME + 600)
        listed = self._convictions(escapes.TREE_DIVERGED)
        self.assertEqual((listed[0].tested_sha, listed[0].newest_sha), ('a' * 40, 'b' * 40))
        self.assertEqual((listed[0].heads, listed[0].builds), (2, 2))
