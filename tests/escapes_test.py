"""The escape check: what main did with a convicted test after the change landed."""

from __future__ import annotations

import sqlite3
import statistics
import time
import unittest
from typing import Optional
from unittest import mock

from ews_dashboard import config, results
from ews_dashboard.analysis import escapes, filters
from tests import fixtures

LANDED_AT = fixtures.DEFAULT_BUILD_TIME + 86400
DAY = 86400
PULL_REQUEST = 12345
TEST = 'fast/a.html'


def _candidate(landed_at: int = LANDED_AT, tested_sha: str = 'a' * 40,
               newest_sha: str = 'a' * 40, build_id: int = 1) -> escapes.Candidate:
    return escapes.Candidate(
        build_id=build_id, test_name=TEST, rule=config.CLEAN_TREE,
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

    def test_a_lone_failure_over_a_clean_baseline_escaped_on_few_failures(self) -> None:
        """The population this check exists to find: main never failed it in 152 runs before the
        landing and failed it once in 96 after, which is an escape on thin evidence and not main's
        own failure."""
        verdict = escapes.decide(_runs(0, 152, LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS),
                                 _runs(1, 96, LANDED_AT))
        self.assertEqual(verdict.verdict, escapes.ESCAPED)
        self.assertEqual(escapes.significance_for_counts(152, 0, verdict.runs_after,
                                                         verdict.failed_after),
                         escapes.NOT_SIGNIFICANT)
        self.assertEqual((verdict.runs_before, verdict.failed_before), (152, 0))
        self.assertEqual((verdict.runs_after, verdict.failed_after), (96, 1))

    def test_a_baseline_that_failed_once_is_main_s_however_bad_the_window_after_is(self) -> None:
        """The baseline decides whose failure it is: main was failing this before the change existed,
        so no rate after the landing can lay it at the change's door."""
        verdict = escapes.decide(_runs(1, 96, LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS),
                                 _runs(96, 96, LANDED_AT))
        self.assertEqual(verdict.verdict, escapes.FAILS_ON_MAIN)
        self.assertEqual((verdict.failed_before, verdict.failed_after), (1, 96))

    def test_a_clean_baseline_over_the_threshold_is_still_a_plain_escape(self) -> None:
        verdict = escapes.decide(_runs(0, 96, LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS),
                                 _runs(60, 96, LANDED_AT))
        self.assertEqual(verdict.verdict, escapes.ESCAPED)

    def test_an_escape_on_few_failures_is_an_answer_and_not_an_absence_of_one(self) -> None:
        """The evidence is thinner, not missing, so it belongs in the rate rather than beside it."""
        self.assertNotIn(escapes.ESCAPED, escapes.UNDECIDED_VERDICTS)
        self.assertIn(escapes.ESCAPED, escapes.VERDICTS)

    def test_how_hard_a_test_failed_is_no_verdict_of_its_own(self) -> None:
        """One stored answer, with the bound read off the counts beside it: a second verdict name is
        what let a stored grade disagree with the runs it was taken from."""
        self.assertEqual(len([verdict for verdict in escapes.VERDICTS if 'ESCAPE' in verdict]), 1)
        self.assertEqual(sorted(escapes.VERDICT_DESCRIPTIONS), sorted(escapes.VERDICTS))

    def test_a_bound_of_exactly_zero_is_not_significant(self) -> None:
        """The boundary belongs to the not-significant side: the page's claim is that the bound clears
        zero, and a bound sitting on zero has not cleared it."""
        self.assertEqual(escapes.significance_for_counts(4, 0, 4, 4), escapes.SIGNIFICANT)
        self.assertEqual(escapes.significance_for_counts(96, 48, 96, 47),
                         escapes.NOT_SIGNIFICANT)
        self.assertLessEqual(escapes.rate_increase_for_counts(96, 48, 96, 47), 0.0)

    def test_a_test_failing_less_often_than_the_threshold_over_a_clean_baseline_escaped(self) -> None:
        watched = [fixtures.run('TEXT', commit_at=LANDED_AT)]
        watched += [fixtures.run(commit_at=LANDED_AT + minute * 60) for minute in range(1, 5)]
        verdict = escapes.decide([fixtures.run(commit_at=LANDED_AT - DAY)], watched)
        self.assertEqual(verdict.verdict, escapes.ESCAPED)
        self.assertEqual(escapes.significance_for_counts(1, 0, verdict.runs_after,
                                                         verdict.failed_after),
                         escapes.NOT_SIGNIFICANT)
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

    def test_a_regression_over_a_baseline_that_only_flaked_is_main_s_failure(self) -> None:
        """One failure in the baseline is still main failing the test without the change, so the
        conviction is corroborated however hard the test fails afterwards."""
        verdict = escapes.decide(_runs(1, 40, LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS),
                                 _runs(40, 40, LANDED_AT))
        self.assertEqual(verdict.verdict, escapes.FAILS_ON_MAIN)
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


class TestRateIncrease(unittest.TestCase):
    """The Newcombe square-and-add bound `rate_increase_for_counts` ranks escapes by, which reads the
    baseline as well as the window after the landing."""

    def test_the_z_is_derived_from_the_one_configured_alpha(self) -> None:
        """Not a written-down constant: a second copy of the significance level is how a page ends up
        printing one alpha and testing another."""
        self.assertAlmostEqual(
            escapes.SIGNIFICANCE_Z,
            statistics.NormalDist().inv_cdf(1 - config.ESCAPE_SIGNIFICANCE_ALPHA), places=12)
        self.assertAlmostEqual(escapes.SIGNIFICANCE_Z, 1.2816, places=4)

    def test_no_runs_after_the_landing_answers_nothing(self) -> None:
        self.assertIsNone(escapes.rate_increase_for_counts(94, 0, 0, 0))
        self.assertIsNone(escapes.significance_for_counts(94, 0, 0, 0))

    def test_no_runs_before_the_landing_answers_nothing_either(self) -> None:
        """No baseline is no change to measure. A 0 here would read as a landing shown to have
        changed nothing, which is the opposite of what an absent baseline says."""
        self.assertIsNone(escapes.rate_increase_for_counts(0, 0, 127, 56))
        self.assertIsNone(escapes.significance_for_counts(0, 0, 127, 56))

    def test_the_bound_is_below_the_difference_in_the_raw_rates(self) -> None:
        """A lower bound and not the difference itself: 0 of 94 to 56 of 127 is a 44.1-point rise in
        the raw rates, and the bound keeps only what 221 runs will support."""
        bound = escapes.rate_increase_for_counts(94, 0, 127, 56)
        self.assertAlmostEqual(bound, 0.38299, places=4)
        self.assertLess(bound, 56 / 127 - 0)

    def test_the_landing_the_fifty_percent_rule_dismissed_is_significant(self) -> None:
        """The population this replaced the rule for: never failed in 94 runs, then 56 of 127, called
        RARE because 44.1 is under 50 while the worsening is the clearest on the page."""
        self.assertEqual(escapes.significance_for_counts(94, 0, 127, 56), escapes.SIGNIFICANT)

    def test_a_baseline_already_failing_at_the_same_rate_is_not_significant(self) -> None:
        """What the share of post-landing runs could not see at all: 88 of 100 after the landing is a
        severe test and not a severe landing when main was failing it 88 of 100 before."""
        self.assertEqual(escapes.significance_for_counts(100, 88, 100, 90),
                         escapes.NOT_SIGNIFICANT)
        self.assertLess(escapes.rate_increase_for_counts(100, 88, 100, 90), 0)

    def test_the_bound_orders_thin_and_thick_evidence_the_raw_rise_gets_backwards(self) -> None:
        """Both landings raise the rate to the same place from the same clean baseline; only the number
        of runs behind them differs, and the bound is what puts the thick evidence first."""
        thin = escapes.rate_increase_for_counts(4, 0, 4, 2)
        thick = escapes.rate_increase_for_counts(400, 0, 400, 200)
        self.assertLess(thin, thick)


class TestRedecided(fixtures.DatabaseTest):
    """Deciding a stored row again from the counts it kept, which is all the migration has."""

    def test_a_stale_verdict_is_named_by_the_counts_rather_than_by_what_was_stored(self) -> None:
        self.assertEqual(escapes.redecided(escapes.FAILS_ON_MAIN, 152, 0, 96, 1), escapes.ESCAPED)

    def test_a_diverged_verdict_is_left_alone_because_it_stored_no_counts(self) -> None:
        """It is reached before any run is asked for, so its zeroes would read as NO_RUNS."""
        self.assertEqual(escapes.redecided(escapes.TREE_DIVERGED, 0, 0, 0, 0),
                         escapes.TREE_DIVERGED)

    def test_a_verdict_the_counts_still_support_is_unchanged(self) -> None:
        self.assertEqual(escapes.redecided(escapes.FAILS_ON_MAIN, 88, 6, 99, 14),
                         escapes.FAILS_ON_MAIN)


class TestTally(fixtures.DatabaseTest):
    """What a window's verdicts come to, and which of them the escape rate is taken over."""

    def test_a_test_main_failed_before_the_change_too_is_counted_in_the_rate(self) -> None:
        """A conviction main answered by failing the test is in the numerator as well as the
        denominator now: FAILS_ON_MAIN is the already-failing half of the escape bucket rather than a
        bucket that vindicates the conviction, so it can be in neither of the two groups the rate
        leaves out — the undecided ones, and the ones outside the numerator."""
        self.assertNotIn(escapes.FAILS_ON_MAIN, escapes.UNDECIDED_VERDICTS)
        self.assertIn(escapes.FAILS_ON_MAIN, escapes.MERGED_ESCAPE_VERDICTS)
        tally = escapes.Tally(
            by_verdict={escapes.ESCAPED: 0, escapes.FAILS_ON_MAIN: 5, escapes.CONTAINED: 39,
                        escapes.NO_RUNS: 2},
            unaskable={},
        )
        self.assertEqual((tally.decided, tally.undecided), (44, 2))
        self.assertEqual(tally.escaped, 5)
        self.assertEqual(tally.escape_rate_pct, round(100.0 * 5 / 44, 1))

    def test_what_was_asked_counts_the_convictions_no_answer_came_back_about(self) -> None:
        """The buckets divide up every conviction main was asked about, so their total has to hold
        the undecided ones the rate leaves out."""
        tally = escapes.Tally(
            by_verdict={escapes.ESCAPED: 0, escapes.FAILS_ON_MAIN: 5, escapes.CONTAINED: 39,
                        escapes.NO_RUNS: 2},
            unaskable={escapes.NOT_LANDED: 7},
        )
        self.assertEqual(tally.asked, 46)

    def test_a_verdict_name_this_code_retired_is_counted_rather_than_dropped(self) -> None:
        """Rebuilding the buckets from VERDICTS alone once took the rows stored under a since-retired
        name out of every total on the page silently, so what is asked has to hold them."""
        tally = escapes.Tally(
            by_verdict={escapes.ESCAPED: 1, escapes.FAILS_ON_MAIN: 5, escapes.CONTAINED: 39,
                        escapes.NO_RUNS: 2},
            unaskable={},
            unrecognised={'FLAKY_ON_MAIN': 10, 'ALREADY_FAILING': 5},
        )
        self.assertEqual(tally.unrecognised_total, 15)
        self.assertEqual(tally.asked, 62)
        self.assertEqual(tally.asked, tally.decided + tally.undecided + tally.unrecognised_total)

    def test_a_retired_name_is_kept_out_of_the_rate_it_cannot_be_graded_by(self) -> None:
        """It answers nothing this code can read, so it belongs in no numerator and no denominator;
        the migration is what folds it into the bucket that replaced it."""
        tally = escapes.Tally(
            by_verdict={escapes.ESCAPED: 1, escapes.CONTAINED: 3},
            unaskable={},
            unrecognised={'FLAKY_ON_MAIN': 96},
        )
        self.assertEqual((tally.decided, tally.undecided), (4, 0))
        self.assertEqual(tally.escape_rate_pct, 25.0)

    def test_the_headline_counts_every_way_a_conviction_can_escape(self) -> None:
        """A conviction that excused something main had not been failing escaped whether the
        failures after were many or few, so the one bucket holds both and the rate is over it."""
        tally = escapes.Tally(
            by_verdict={escapes.ESCAPED: 5, escapes.CONTAINED: 15, escapes.NO_RUNS: 4},
            unaskable={},
        )
        self.assertEqual((tally.escaped, tally.decided), (5, 20))
        self.assertEqual(tally.escape_rate_pct, 25.0)

    def test_the_buckets_still_divide_up_what_was_asked(self) -> None:
        tally = escapes.Tally(
            by_verdict={escapes.ESCAPED: 5, escapes.CONTAINED: 15, escapes.NO_RUNS: 4},
            unaskable={escapes.NOT_LANDED: 7},
            unrecognised={'FLAKY_ON_MAIN': 6},
        )
        self.assertEqual(tally.asked, tally.decided + tally.undecided + tally.unrecognised_total)
        self.assertEqual(tally.asked, 30)

    def test_a_tally_with_nothing_unrecognised_counts_exactly_what_it_used_to(self) -> None:
        """The ordinary case, and the one every other page number is read under."""
        tally = escapes.Tally(by_verdict={escapes.ESCAPED: 1, escapes.NO_RUNS: 2}, unaskable={})
        self.assertEqual((tally.unrecognised_total, tally.asked), (0, 3))


class TestByVerdict(fixtures.DatabaseTest):
    """What the stored rows come to, including any stored under a name since retired."""

    def _store(self, number: int, verdict: str) -> None:
        build_id = self.store_build(number, flaky={TEST: config.CLEAN_TREE}, pr_id=number,
                                    pr_title='A change that landed', sha='a' * 40)
        with self.connection:
            self.connection.execute(
                '''INSERT INTO escape_verdicts (
                    build_id, test_name, verdict, runs_before, failed_before, runs_after,
                    failed_after, window_ends_at, decided_at
                ) VALUES (?,?,?,?,?,?,?,?,?)''',
                (build_id, TEST, verdict, 4, 0, 6, 2,
                 LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS, LANDED_AT),
            )

    def _tally(self) -> escapes.Tally:
        return escapes.tally(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                             fixtures.DEFAULT_BUILD_TIME + DAY)

    def test_every_bucket_is_reported_even_when_nothing_reached_it(self) -> None:
        self._store(1, escapes.CONTAINED)
        by_verdict = escapes.by_verdict(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                        fixtures.DEFAULT_BUILD_TIME + DAY)
        self.assertEqual(sorted(by_verdict), sorted(escapes.VERDICTS))
        self.assertEqual(by_verdict[escapes.ESCAPED], 0)

    def test_a_verdict_no_bucket_matches_is_reported_separately(self) -> None:
        """A row can only hold one on a table whose CHECK constraint predates the rename, so the
        database-level case lives with the migration that fixes it; this pins the empty answer."""
        self._store(1, escapes.CONTAINED)
        self.assertEqual(self._tally().unrecognised, {})


class TestSubcategories(fixtures.DatabaseTest):
    """How the escapes split under the one headline number, twice over."""

    def _escape(self, number: int, runs_after: int, failed_after: int,
                recent_runs: Optional[int] = None, recent_failed: Optional[int] = None,
                recent_checked_at: Optional[int] = None,
                verdict: str = escapes.ESCAPED) -> None:
        test_name = f'fast/{number}.html'
        build_id = self.store_build(number, flaky={test_name: config.CLEAN_TREE}, pr_id=number,
                                    pr_title='A change that landed', sha='a' * 40)
        with self.connection:
            self.connection.execute(
                '''INSERT INTO escape_verdicts (
                    build_id, test_name, verdict, runs_before, failed_before, runs_after,
                    failed_after, recent_runs, recent_failed, recent_checked_at,
                    window_ends_at, decided_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (build_id, test_name, verdict, 152, 0, runs_after, failed_after, recent_runs,
                 recent_failed, recent_checked_at, LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS,
                 LANDED_AT),
            )

    def _subcategories(self, **scope: object) -> escapes.Subcategories:
        return escapes.escape_subcategories(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                            fixtures.DEFAULT_BUILD_TIME + DAY, **scope)

    def test_each_split_adds_up_to_the_escapes_it_divides(self) -> None:
        """One of each currency state, so the four are shown to partition the bucket rather than to
        happen to agree with it on a fixture missing one."""
        checked_at = fixtures.DEFAULT_BUILD_TIME + 2 * DAY
        self._escape(1, runs_after=96, failed_after=90, recent_runs=18, recent_failed=11,
                     recent_checked_at=checked_at)
        self._escape(2, runs_after=96, failed_after=1, recent_runs=22, recent_failed=0,
                     recent_checked_at=checked_at)
        self._escape(3, runs_after=40, failed_after=2, recent_runs=0, recent_failed=0,
                     recent_checked_at=checked_at)
        self._escape(4, runs_after=40, failed_after=2)

        split = self._subcategories()

        self.assertEqual(
            (split.still_failing, split.recovered, split.not_run_lately, split.unchecked),
            (1, 1, 1, 1),
        )
        self.assertEqual((split.significant, split.not_significant), (3, 1))
        self.assertEqual(split.total, 4)
        self.assertEqual(split.significance_total, split.total)

    def test_the_significance_split_reads_the_baseline_and_not_the_share_of_runs(self) -> None:
        """2 of 40 failing is 5%, which the deleted 50% rule called a thin escape and this calls a
        measurable rise, because the baseline it is measured against is 0 of 152."""
        self._escape(1, runs_after=40, failed_after=2)
        self._escape(2, runs_after=96, failed_after=1)
        split = self._subcategories()
        self.assertEqual((split.significant, split.not_significant), (1, 1))
        self.assertEqual(split.significant_tests, 1)

    def test_an_escape_nothing_asked_about_is_neither_still_failing_nor_recovered(self) -> None:
        self._escape(1, runs_after=96, failed_after=1)
        split = self._subcategories()
        self.assertEqual(
            (split.still_failing, split.recovered, split.not_run_lately, split.unchecked),
            (0, 0, 0, 1),
        )

    def test_an_escape_main_has_not_run_lately_is_not_counted_as_a_recovery(self) -> None:
        """Main demonstrating nothing is not main demonstrating a fix, and the recovered count is the
        one a reader takes as reassurance."""
        self._escape(1, runs_after=96, failed_after=1, recent_runs=0, recent_failed=0,
                     recent_checked_at=fixtures.DEFAULT_BUILD_TIME + 2 * DAY)
        split = self._subcategories()
        self.assertEqual(
            (split.still_failing, split.recovered, split.not_run_lately, split.unchecked),
            (0, 0, 1, 0),
        )

    def test_no_escape_can_fall_outside_the_significance_split(self) -> None:
        """A row with no run on one side of the landing would be counted by the currency split and by
        neither significance bucket; `verdict_for_counts` cannot produce one in this bucket, which is
        what makes the two halves a partition rather than a pair of filters."""
        self.assertEqual(escapes.verdict_for_counts(152, 0, 0, 0), escapes.NO_RUNS)
        self.assertEqual(escapes.verdict_for_counts(0, 0, 96, 1), escapes.NO_BASELINE)
        self.assertIsNotNone(escapes.significance_for_counts(1, 0, 1, 1))

    def test_no_verdict_outside_the_merged_bucket_is_counted_in_the_split(self) -> None:
        """CONTAINED is not in the bucket the splits divide; FAILS_ON_MAIN is, since it is the
        already-failing half of it."""
        self._escape(1, runs_after=96, failed_after=0, verdict=escapes.CONTAINED)
        self._escape(2, runs_after=96, failed_after=48, verdict=escapes.FAILS_ON_MAIN)
        self.assertEqual(self._subcategories().total, 1)

    def test_a_queue_the_page_is_narrowed_to_narrows_the_split_too(self) -> None:
        self._escape(1, runs_after=96, failed_after=90)
        self.assertEqual(self._subcategories(builders=(fixtures.GTK_BUILDER,)).total, 0)
        self.assertEqual(self._subcategories(builders=(fixtures.LAYOUT_BUILDER,)).total, 1)

    def test_the_head_counts_the_same_convictions_the_listing_pages_through(self) -> None:
        """The defect this closes: the head counted from a source of its own and the listing from one
        that joins the flakiness verdict, so a stored verdict whose conviction had gone was a distinct
        test in the head and no row in the tally. A conviction is a flakiness verdict that HAS a rule, so
        neither counts one without it now, and the two numbers a reader sees side by side agree."""
        self._escape(1, runs_after=96, failed_after=90)
        self._escape(2, runs_after=96, failed_after=90)
        with self.connection:
            self.connection.execute('DELETE FROM flakiness_verdicts WHERE test_name = ?',
                                   ('fast/2.html',))
        listed = escapes.answered_tests(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                        fixtures.DEFAULT_BUILD_TIME + DAY,
                                        escapes.MERGED_ESCAPE_VERDICTS)
        self.assertEqual(self._subcategories().distinct_tests, listed.total)
        self.assertEqual([one.test_name for one in listed.tests], ['fast/1.html'])

    def test_a_conviction_whose_rule_was_withdrawn_is_in_neither_count(self) -> None:
        """`flakiness_verdicts.rule` is nullable, and a row without one is a test the bot asked about and
        did not convict. The tally, the head's splits and the listing all read `CONVICTED`, so none of
        them counts a verdict stored against a conviction that no longer exists."""
        self._escape(1, runs_after=96, failed_after=90)
        self._escape(2, runs_after=96, failed_after=90)
        with self.connection:
            self.connection.execute('UPDATE flakiness_verdicts SET rule = NULL WHERE test_name = ?',
                                   ('fast/2.html',))
        counted = escapes.tally(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                fixtures.DEFAULT_BUILD_TIME + DAY)
        listed = escapes.answered_tests(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                        fixtures.DEFAULT_BUILD_TIME + DAY,
                                        escapes.MERGED_ESCAPE_VERDICTS)
        split = self._subcategories()
        self.assertEqual((counted.escaped, split.total, split.distinct_tests, listed.total),
                         (1, 1, 1, 1))


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


class TestSupersedingHead(fixtures.DatabaseTest):
    """Which build of a pull request is allowed to say the conviction was made on superseded code.

    EWS keeps building a pull request after it has landed, so the newest build of one is routinely a
    build of code main already had. Such a build cannot have superseded the tree that landed, and
    counting it as divergence retires a conviction main could have graded.
    """

    def _convict(self, number: int, sha: str, started_at: int) -> int:
        return self.store_build(number, flaky={TEST: config.CLEAN_TREE}, pr_id=PULL_REQUEST,
                                pr_title='A change that landed', sha=sha, started_at=started_at)

    def _candidate(self, build_id: int) -> escapes.Candidate:
        listed = [one for one in escapes.candidates(self.connection,
                                                    fixtures.DEFAULT_BUILD_TIME - DAY,
                                                    LANDED_AT + DAY)
                  if one.build_id == build_id]
        self.assertEqual(len(listed), 1)
        return listed[0]

    def test_a_build_started_after_the_landing_is_not_the_head_that_landed(self) -> None:
        convicted = self._convict(1, 'a' * 40, fixtures.DEFAULT_BUILD_TIME)
        self._convict(2, 'b' * 40, LANDED_AT + 600)
        self.store_landing(PULL_REQUEST, landed_at=LANDED_AT)
        candidate = self._candidate(convicted)
        self.assertEqual(candidate.newest_sha, 'a' * 40)
        self.assertFalse(candidate.diverged)

    def test_a_build_started_before_the_landing_is(self) -> None:
        convicted = self._convict(1, 'a' * 40, fixtures.DEFAULT_BUILD_TIME)
        self._convict(2, 'b' * 40, fixtures.DEFAULT_BUILD_TIME + 600)
        self.store_landing(PULL_REQUEST, landed_at=LANDED_AT)
        candidate = self._candidate(convicted)
        self.assertEqual(candidate.newest_sha, 'b' * 40)
        self.assertTrue(candidate.diverged)

    def test_a_conviction_a_post_landing_build_followed_is_graded_rather_than_retired(self) -> None:
        self._convict(1, 'a' * 40, fixtures.DEFAULT_BUILD_TIME)
        self._convict(2, 'b' * 40, LANDED_AT + 600)
        self.store_landing(PULL_REQUEST, landed_at=LANDED_AT)
        history = fixtures.StubRunHistory({TEST: [
            fixtures.run(commit_at=LANDED_AT - DAY),
            fixtures.run('TEXT', commit_at=LANDED_AT),
        ]})
        self.assertEqual(dict(escapes.assess(self.connection, history,
                                             fixtures.DEFAULT_BUILD_TIME - DAY,
                                             fixtures.DEFAULT_BUILD_TIME + DAY)),
                         {escapes.ESCAPED: 1})


class TestCurrency(fixtures.DatabaseTest):
    """Whether main is still failing an escaped test, asked over a fresh window ending now."""

    def setUp(self) -> None:
        super().setUp()
        self.build_id = self.store_build(1, flaky={TEST: config.CLEAN_TREE}, pr_id=PULL_REQUEST,
                                         pr_title='A change that landed', sha='a' * 40)
        self.store_landing(PULL_REQUEST, landed_at=LANDED_AT)
        self.now = int(time.time())

    def _store_escape(self, verdict: str = escapes.ESCAPED, recent_runs: Optional[int] = None,
                      recent_failed: Optional[int] = None,
                      recent_checked_at: Optional[int] = None) -> None:
        with self.connection:
            self.connection.execute(
                '''INSERT OR REPLACE INTO escape_verdicts (
                    build_id, test_name, verdict, runs_before, failed_before, runs_after,
                    failed_after, recent_runs, recent_failed, recent_checked_at,
                    window_ends_at, decided_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (self.build_id, TEST, verdict, 152, 0, 96, 1, recent_runs, recent_failed,
                 recent_checked_at, LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS, LANDED_AT),
            )

    def _stored(self) -> sqlite3.Row:
        return self.connection.execute(
            'SELECT recent_runs, recent_failed, recent_checked_at FROM escape_verdicts '
            'WHERE build_id = ? AND test_name = ?', (self.build_id, TEST),
        ).fetchone()

    def _candidate(self) -> escapes.Candidate:
        return _candidate(build_id=self.build_id)

    def test_an_escape_checked_within_the_day_is_not_asked_again(self) -> None:
        """Roughly sixty escapes are re-read on every pass, so the answer has to stand for a while or
        the check becomes a second full sweep of results.webkit.org."""
        self._store_escape(recent_runs=40, recent_failed=3,
                           recent_checked_at=self.now - config.CURRENCY_TTL_SECONDS + 600)
        history = fixtures.StubRunHistory({TEST: [fixtures.run('TEXT', commit_at=self.now - 3600)]})

        self.assertFalse(escapes.check_currency(self.connection, history, self._candidate(),
                                                self.now))
        self.assertEqual(history.queries, [])
        self.assertEqual(self._stored()['recent_failed'], 3)

    def test_an_escape_checked_longer_ago_than_the_ttl_is_asked_again(self) -> None:
        self._store_escape(recent_runs=40, recent_failed=3,
                           recent_checked_at=self.now - config.CURRENCY_TTL_SECONDS - 600)
        history = fixtures.StubRunHistory({TEST: [
            fixtures.run('TEXT', commit_at=self.now - 3600),
            fixtures.run(commit_at=self.now - 7200),
        ]})

        self.assertTrue(escapes.check_currency(self.connection, history, self._candidate(),
                                               self.now))
        self.assertEqual([(query.after, query.before) for query in history.queries],
                         [(self.now - escapes.CURRENCY_WINDOW_SECONDS, self.now)])
        row = self._stored()
        self.assertEqual((row['recent_runs'], row['recent_failed'], row['recent_checked_at']),
                         (2, 1, self.now))

    def test_the_window_asked_about_ends_now_rather_than_at_the_landing(self) -> None:
        """A window fixed to the landing cannot say whether the regression is still there, however
        wide it is."""
        self._store_escape()
        history = fixtures.StubRunHistory({TEST: []})

        escapes.check_currency(self.connection, history, self._candidate(), self.now)

        asked = history.queries[0]
        self.assertEqual(asked.before, self.now)
        self.assertEqual(self.now - asked.after, config.CURRENCY_DAYS * 86400)
        self.assertGreater(asked.after, LANDED_AT)

    def test_an_escape_nothing_has_asked_about_is_asked(self) -> None:
        self._store_escape()
        history = fixtures.StubRunHistory({TEST: [fixtures.run(commit_at=self.now - 600)]})

        self.assertTrue(escapes.check_currency(self.connection, history, self._candidate(),
                                               self.now))
        self.assertEqual(self._stored()['recent_checked_at'], self.now)

    def test_an_outage_leaves_the_escape_unchecked_rather_than_recovered(self) -> None:
        self._store_escape()
        history = fixtures.StubRunHistory({}, unavailable={TEST})

        self.assertFalse(escapes.check_currency(self.connection, history, self._candidate(),
                                                self.now))
        row = self._stored()
        self.assertIsNone(row['recent_checked_at'])
        self.assertEqual(escapes.currency_for_counts(row['recent_runs'], row['recent_failed'],
                                                     row['recent_checked_at']), escapes.UNCHECKED)

    def test_no_failure_in_the_recent_window_reads_as_main_having_stopped(self) -> None:
        self._store_escape()
        history = fixtures.StubRunHistory({TEST: [fixtures.run(commit_at=self.now - 600),
                                                  fixtures.run(commit_at=self.now - 1200)]})

        escapes.check_currency(self.connection, history, self._candidate(), self.now)

        row = self._stored()
        self.assertEqual((row['recent_runs'], row['recent_failed']), (2, 0))
        self.assertEqual(escapes.currency_for_counts(row['recent_runs'], row['recent_failed'],
                                                     row['recent_checked_at']), escapes.RECOVERED)

    def test_a_window_main_ran_nothing_in_reads_as_unmeasured_and_not_as_a_recovery(self) -> None:
        """Zero runs and zero failures out of them are the same two numbers a recovery stores, so
        without the run count this state would have been reported as main having fixed the test."""
        self._store_escape()
        history = fixtures.StubRunHistory({TEST: []})

        escapes.check_currency(self.connection, history, self._candidate(), self.now)

        row = self._stored()
        self.assertEqual((row['recent_runs'], row['recent_failed'], row['recent_checked_at']),
                         (0, 0, self.now))
        self.assertEqual(escapes.currency_for_counts(row['recent_runs'], row['recent_failed'],
                                                     row['recent_checked_at']),
                         escapes.NOT_RUN_LATELY)

    def test_no_two_currency_states_are_ever_the_same_answer(self) -> None:
        """No boolean is stored for exactly this reason: zero failures out of some runs, zero runs to
        fail, and no measurement at all are three different answers, and two of them are not answers.
        """
        states = (escapes.currency_for_counts(40, 3, self.now),
                  escapes.currency_for_counts(44, 0, self.now),
                  escapes.currency_for_counts(0, 0, self.now),
                  escapes.currency_for_counts(None, None, None))
        self.assertEqual(states, (escapes.STILL_FAILING, escapes.RECOVERED,
                                  escapes.NOT_RUN_LATELY, escapes.UNCHECKED))
        self.assertEqual(len(set(states)), 4)

    def test_only_the_escapes_are_asked_whether_main_is_still_failing_them(self) -> None:
        """Tens of escapes against thousands of convictions: a currency query per conviction is the
        hour-long pass this dashboard already learned to avoid."""
        history = fixtures.StubRunHistory({TEST: [
            fixtures.run(commit_at=LANDED_AT - DAY),
            fixtures.run(commit_at=LANDED_AT),
            fixtures.run('TEXT', commit_at=self.now - 3600),
        ]})

        outcomes = dict(escapes.assess(self.connection, history, fixtures.DEFAULT_BUILD_TIME - DAY,
                                       fixtures.DEFAULT_BUILD_TIME + DAY))

        self.assertEqual(outcomes, {escapes.CONTAINED: 1})
        self.assertEqual([(query.after, query.before) for query in history.queries],
                         [(LANDED_AT - escapes.ESCAPE_WINDOW_SECONDS, LANDED_AT),
                          (LANDED_AT, LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS)])
        self.assertIsNone(self._stored()['recent_checked_at'])

    def test_an_escape_the_assess_pass_decides_is_checked_in_the_same_pass(self) -> None:
        history = fixtures.StubRunHistory({TEST: [
            fixtures.run(commit_at=LANDED_AT - DAY),
            fixtures.run('TEXT', commit_at=LANDED_AT),
            fixtures.run('TEXT', commit_at=self.now - 3600),
        ]})

        outcomes = dict(escapes.assess(self.connection, history, fixtures.DEFAULT_BUILD_TIME - DAY,
                                       fixtures.DEFAULT_BUILD_TIME + DAY))

        self.assertEqual(outcomes, {escapes.ESCAPED: 1})
        row = self._stored()
        self.assertEqual((row['recent_runs'], row['recent_failed']), (1, 1))
        self.assertIsNotNone(row['recent_checked_at'])


class TestDamage(unittest.TestCase):
    """The raw rate `damage_for_counts` reads off the last currency check."""

    def test_none_runs_answers_nothing(self) -> None:
        self.assertIsNone(escapes.damage_for_counts(None, None))

    def test_zero_runs_answers_nothing(self) -> None:
        self.assertIsNone(escapes.damage_for_counts(0, 0))

    def test_none_failures_answers_nothing_rather_than_zero(self) -> None:
        """A row nobody has run the currency query against stores no failure count either, and 0%
        would claim an answer this row does not carry."""
        self.assertIsNone(escapes.damage_for_counts(40, None))

    def test_one_of_three_hundred_and_eleven_is_the_raw_rate(self) -> None:
        self.assertAlmostEqual(escapes.damage_for_counts(311, 1), 0.0032, places=4)


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
        landed_at=LANDED_AT, window_ends_at=LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS,
        tested_sha='a' * 40, newest_sha='b' * 40, heads=2, builds=3,
    )
    values.update(fields)
    return escapes.Conviction(**values)


class TestReason(fixtures.DatabaseTest):
    """The short phrase a row gets where main answered nothing, and the silence where a row answers in
    figures instead."""

    def test_the_merged_escape_bucket_gets_no_phrase_because_its_figures_say_it(self) -> None:
        """Both halves of the bucket print three count pairs — the baseline, the counts after the
        landing, and the counts main has run lately — so a phrase beside them would only restate a
        number already in the row."""
        for verdict in escapes.MERGED_ESCAPE_VERDICTS:
            self.assertEqual(escapes.reason(_conviction(verdict)), '', verdict)

    def test_a_contained_verdict_says_main_never_failed_it_and_how_often_it_ran(self) -> None:
        """Both numeric cells are em dashes on this row, so the run count the prose carried has
        nowhere else to live."""
        self.assertEqual(escapes.reason(_conviction(escapes.CONTAINED, failed_after=0)),
                         'Never failed it in 6 runs')

    def test_a_no_runs_verdict_says_nothing_ran_after_the_landing(self) -> None:
        self.assertEqual(escapes.reason(_conviction(escapes.NO_RUNS, runs_after=0, failed_after=0)),
                         'No runs after the landing')

    def test_a_no_baseline_verdict_keeps_the_after_counts_and_says_nothing_ran_before(self) -> None:
        self.assertEqual(escapes.reason(_conviction(escapes.NO_BASELINE, runs_before=0)),
                         'Failed 2 of 6, nothing before')

    def test_a_diverged_verdict_says_how_far_the_pull_request_moved(self) -> None:
        """The two shas the prose named are dropped rather than abbreviated: the row's Build and PR
        links reach both, and these counts are what say how far apart they are."""
        self.assertEqual(escapes.reason(_conviction(escapes.TREE_DIVERGED)),
                         'Built 3 times across 2 heads')

    def test_a_diverged_verdict_with_no_head_recorded_reads_the_same(self) -> None:
        """No sha is named, so the branch the prose needed for a build ingested before
        `github.head.sha` was recorded is gone rather than special-cased."""
        self.assertEqual(escapes.reason(_conviction(escapes.TREE_DIVERGED, tested_sha=None,
                                                   newest_sha=None, pr_id=None)),
                         'Built 3 times across 2 heads')

    def test_every_reason_fits_the_cap_the_column_is_budgeted_at(self) -> None:
        """The cap is the point of the column: the prose it replaced ran to 21 words a row and 4,359
        down the column, and a phrase free to grow grows back into prose.
        `tests/prose_budget_test.py` holds the page itself to the same ceiling."""
        for verdict in escapes.VERDICTS:
            self.assertLessEqual(len(escapes.reason(_conviction(verdict)).split()),
                                 escapes.REASON_WORDS, verdict)

    def test_a_reason_carries_no_markup_of_its_own(self) -> None:
        """It interpolates stored counts, so the page has to keep autoescaping it."""
        for verdict in escapes.VERDICTS:
            phrase = escapes.reason(_conviction(verdict))
            self.assertIs(type(phrase), str)
            self.assertNotIn('<', phrase)

    def test_no_reason_mentions_a_figure_the_row_prints_beside_it(self) -> None:
        """The whole restructure: the after pair and the recent pair have their own cells, so a phrase
        repeating either is the duplication this column was replaced to stop."""
        escape = _conviction(escapes.ESCAPED, runs_before=98, failed_before=0, runs_after=108,
                             failed_after=7, recent_runs=259, recent_failed=0,
                             recent_checked_at=fixtures.DEFAULT_BUILD_TIME)
        self.assertEqual(escapes.reason(escape), '')


class TestDrilldownConvictions(fixtures.DatabaseTest):
    """The individual convictions behind one test's row, which is what the drilldown lists."""

    def _convict(self, number: int, test_name: str, verdict: str, pr_id: int,
                 builder: str = fixtures.LAYOUT_BUILDER, builder_id: int = 7,
                 landed_at: Optional[int] = LANDED_AT) -> int:
        build_id = self.store_build(number, flaky={test_name: config.CLEAN_TREE}, pr_id=pr_id,
                                    pr_title='A change that landed', builder=builder,
                                    builder_id=builder_id, sha='a' * 40)
        with self.connection:
            self.connection.execute(
                '''INSERT INTO escape_verdicts (
                    build_id, test_name, verdict, runs_before, failed_before, runs_after,
                    failed_after, landed_at, window_ends_at, decided_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (build_id, test_name, verdict, 4, 0, 6, 2, landed_at,
                 LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS, LANDED_AT),
            )
        return build_id

    def _convictions(self, verdict: str, test_name: str = TEST, **scope: object) -> tuple:
        """One test's convictions alone, since every test in this class is about one row's contents."""
        return escapes.test_convictions(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                        fixtures.DEFAULT_BUILD_TIME + DAY, verdict, test_name,
                                        **scope).convictions

    def test_only_the_convictions_of_the_opened_test_in_the_open_category_are_listed(self) -> None:
        self._convict(1, TEST, escapes.CONTAINED, pr_id=1)
        self._convict(2, 'fast/b.html', escapes.CONTAINED, pr_id=2)
        listed = self._convictions(escapes.CONTAINED)
        self.assertEqual([(one.test_name, one.verdict) for one in listed],
                         [(TEST, escapes.CONTAINED)])
        self.assertEqual(listed[0].rule, config.CLEAN_TREE)
        self.assertEqual(listed[0].landed_at, LANDED_AT)

    def test_a_test_with_no_conviction_in_the_open_category_lists_nothing(self) -> None:
        """The pane is scoped to one category, so opening a row cannot reach convictions main
        answered another way — those are a row of that category's own listing."""
        self._convict(1, TEST, escapes.CONTAINED, pr_id=1)
        self.assertEqual(self._convictions(escapes.NO_RUNS), ())

    def test_a_queue_the_page_is_narrowed_to_narrows_the_list_too(self) -> None:
        self._convict(1, TEST, escapes.CONTAINED, pr_id=1)
        self._convict(2, TEST, escapes.CONTAINED, pr_id=2, builder=fixtures.GTK_BUILDER,
                      builder_id=9)
        self.assertEqual(
            [one.builder for one in self._convictions(escapes.CONTAINED,
                                                      builders=(fixtures.GTK_BUILDER,))],
            [fixtures.GTK_BUILDER],
        )

    def test_a_suite_the_page_is_narrowed_to_narrows_the_list_too(self) -> None:
        self._convict(1, TEST, escapes.CONTAINED, pr_id=1)
        self._convict(2, 'TestWebKitAPI.A.b', escapes.CONTAINED, pr_id=2,
                      builder=fixtures.API_BUILDER, builder_id=8)
        self.assertEqual([one.test_name for one in
                          self._convictions(escapes.CONTAINED, test_name='TestWebKitAPI.A.b',
                                            suite='api-tests')],
                         ['TestWebKitAPI.A.b'])
        self.assertEqual(self._convictions(escapes.CONTAINED, suite='api-tests'), ())

    def test_the_newest_conviction_is_listed_first(self) -> None:
        """A conviction list reads by when it landed, and the build id behind that makes the order
        total: the pane is capped, so two renders must not disagree about what the cap kept."""
        self._convict(1, TEST, escapes.CONTAINED, pr_id=1)
        self._convict(2, TEST, escapes.CONTAINED, pr_id=2, landed_at=LANDED_AT + 600)
        self.assertEqual([one.pr_id for one in self._convictions(escapes.CONTAINED)], [2, 1])

    def test_the_cap_is_reported_rather_than_silently_cutting_the_list(self) -> None:
        for number in range(1, 4):
            self._convict(number, TEST, escapes.CONTAINED, pr_id=number)
        capped = escapes.test_convictions(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                          fixtures.DEFAULT_BUILD_TIME + DAY, escapes.CONTAINED,
                                          TEST, limit=2)
        self.assertEqual((len(capped.convictions), capped.total), (2, 3))
        self.assertTrue(capped.truncated)

    def test_a_reader_s_own_clause_narrows_the_list_the_way_it_narrowed_the_row(self) -> None:
        """The defect this closes: the listing row honoured a `queue` clause and the pane beside it did
        not, so the row's own Convictions cell read one number and the pane it opened read another over
        the same test. Both read through one predicate now, so the pane's total IS the row's count."""
        self._convict(1, TEST, escapes.CONTAINED, pr_id=1)
        self._convict(2, TEST, escapes.CONTAINED, pr_id=2, builder=fixtures.GTK_BUILDER,
                      builder_id=9)
        self._convict(3, TEST, escapes.CONTAINED, pr_id=3, builder=fixtures.GTK_BUILDER,
                      builder_id=9)
        narrowing = filters.parse(filters.ESCAPES,
                                 (('queue', 'eq', fixtures.LAYOUT_BUILDER),))
        pane = escapes.test_convictions(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                       fixtures.DEFAULT_BUILD_TIME + DAY, escapes.CONTAINED, TEST,
                                       conditions=narrowing)
        row = escapes.answered_tests(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                     fixtures.DEFAULT_BUILD_TIME + DAY, escapes.CONTAINED,
                                     conditions=narrowing).tests[0]
        self.assertEqual((pane.total, len(pane.convictions)), (row.convictions, row.convictions))
        self.assertEqual([one.builder for one in pane.convictions], [fixtures.LAYOUT_BUILDER])

    def test_a_clause_about_the_group_does_not_narrow_the_convictions_inside_one(self) -> None:
        """An aggregate clause is the HAVING half: it chose which tests the listing holds, and inside
        one of those tests there is no group left for it to narrow. So the pane still lists every
        conviction the row counted rather than the none a `COUNT(*) >= 2` would leave of a single row."""
        self._convict(1, TEST, escapes.CONTAINED, pr_id=1)
        self._convict(2, TEST, escapes.CONTAINED, pr_id=2)
        grouped = filters.parse(filters.ESCAPES, (('convictions', 'ge', '2'),))
        pane = escapes.test_convictions(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                       fixtures.DEFAULT_BUILD_TIME + DAY, escapes.CONTAINED, TEST,
                                       conditions=grouped)
        self.assertEqual((pane.total, len(pane.convictions)), (2, 2))

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

    def test_the_head_named_as_the_one_that_landed_predates_the_landing(self) -> None:
        """The listing has to name the head the verdict was reached against, or a reader is told a
        conviction diverged from a build EWS only started once the change was already on main."""
        self._convict(1, TEST, escapes.TREE_DIVERGED, pr_id=PULL_REQUEST)
        self.store_build(2, flaky={TEST: config.CLEAN_TREE}, pr_id=PULL_REQUEST,
                         pr_title='A change that landed', sha='b' * 40,
                         started_at=LANDED_AT + 600)
        listed = self._convictions(escapes.TREE_DIVERGED)
        self.assertEqual((listed[0].tested_sha, listed[0].newest_sha), ('a' * 40, 'a' * 40))

    def test_a_row_with_no_landing_time_is_still_listed_and_reports_none(self) -> None:
        """The backfill leaves a row whose landing the database no longer holds at null, and the
        counts either side of that landing are still the evidence the page exists to show, so the row
        stays and the missing time is reported as missing."""
        self._convict(1, TEST, escapes.ESCAPED, pr_id=1, landed_at=None)
        listed = self._convictions(escapes.ESCAPED)
        self.assertEqual([one.test_name for one in listed], [TEST])
        self.assertIsNone(listed[0].landed_at)
        self.assertEqual((listed[0].runs_after, listed[0].failed_after), (6, 2))

    def test_the_rate_increase_and_damage_read_through_the_stored_counts(self) -> None:
        build_id = self._convict(1, TEST, escapes.ESCAPED, pr_id=1)
        with self.connection:
            self.connection.execute(
                'UPDATE escape_verdicts SET recent_runs = ?, recent_failed = ?, '
                'recent_checked_at = ? WHERE build_id = ? AND test_name = ?',
                (40, 3, LANDED_AT, build_id, TEST),
            )
        listed = self._convictions(escapes.ESCAPED)
        self.assertAlmostEqual(listed[0].rate_increase,
                               escapes.rate_increase_for_counts(4, 0, 6, 2), places=12)
        self.assertLess(listed[0].rate_increase, 0.0)
        self.assertFalse(listed[0].significant)
        self.assertEqual(listed[0].damage, 0.075)


class TestStoredLandingTime(fixtures.DatabaseTest):
    """The landing time a verdict was decided about, which the row carries rather than derives.

    `escapes.ESCAPE_WINDOW_SECONDS` is computed at import time from `config.ESCAPE_WINDOW_DAYS`, so
    patching the config value would leave the module's constant alone and prove nothing; the constant
    itself is what a running dashboard would have been restarted with, and what these patch.
    """

    def _convict(self) -> int:
        return self.store_build(1, flaky={TEST: config.CLEAN_TREE}, pr_id=PULL_REQUEST,
                                pr_title='A change that landed', sha='a' * 40)

    def _listed(self, verdict: str) -> escapes.Conviction:
        listed = escapes.test_convictions(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                          fixtures.DEFAULT_BUILD_TIME + DAY, verdict, TEST)
        self.assertEqual(listed.total, 1)
        return listed.convictions[0]

    def test_a_stored_landing_time_does_not_move_when_the_window_widens(self) -> None:
        """The whole point of storing it: the window's width is configuration, and back-deriving the
        landing from `window_ends_at` printed a date that never happened the moment it changed."""
        self._convict()
        self.store_landing(PULL_REQUEST, landed_at=LANDED_AT)
        history = fixtures.StubRunHistory({TEST: [fixtures.run(commit_at=LANDED_AT)]})
        escapes.assess(self.connection, history, fixtures.DEFAULT_BUILD_TIME - DAY,
                       fixtures.DEFAULT_BUILD_TIME + DAY)

        with mock.patch.object(escapes, 'ESCAPE_WINDOW_SECONDS', 30 * 86400):
            self.assertEqual(self._listed(escapes.CONTAINED).landed_at, LANDED_AT)

    def test_the_counts_stay_attached_to_the_window_they_were_counted_over(self) -> None:
        """The other half of storing both: a widened window must not claim the old counts came from
        it, so the row's window end is the one it was decided under."""
        self._convict()
        self.store_landing(PULL_REQUEST, landed_at=LANDED_AT)
        history = fixtures.StubRunHistory({TEST: [fixtures.run(commit_at=LANDED_AT)]})
        escapes.assess(self.connection, history, fixtures.DEFAULT_BUILD_TIME - DAY,
                       fixtures.DEFAULT_BUILD_TIME + DAY)
        counted_over = LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS

        with mock.patch.object(escapes, 'ESCAPE_WINDOW_SECONDS', 30 * 86400):
            listed = self._listed(escapes.CONTAINED)
            self.assertEqual(listed.window_ends_at, counted_over)
            self.assertNotEqual(listed.window_ends_at, listed.landed_at + 30 * 86400)

    def _store_row(self, window_ends_at: int, decided_at: int, landed_at: int) -> None:
        with self.connection:
            self.connection.execute(
                '''INSERT INTO escape_verdicts (
                    build_id, test_name, verdict, runs_before, failed_before, runs_after,
                    failed_after, landed_at, window_ends_at, decided_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (self._convict(), TEST, escapes.CONTAINED, 4, 0, 6, 0, landed_at, window_ends_at,
                 decided_at),
            )

    def _asked(self, history: fixtures.StubRunHistory) -> int:
        escapes.assess(self.connection, history, fixtures.DEFAULT_BUILD_TIME - DAY,
                       fixtures.DEFAULT_BUILD_TIME + DAY)
        return len(history.queries)

    def test_a_window_that_has_not_settled_is_asked_again_however_old_the_landing(self) -> None:
        """The settling check is about when the runs stop arriving, which is the end of the window and
        never the landing: keyed on the landing, this row would be kept a whole window early."""
        now = int(time.time())
        self.store_landing(PULL_REQUEST, landed_at=LANDED_AT)
        self._store_row(window_ends_at=now, decided_at=now, landed_at=LANDED_AT)

        self.assertGreater(self._asked(fixtures.StubRunHistory({TEST: []})), 0)

    def test_a_window_that_has_settled_is_kept_however_recent_the_landing(self) -> None:
        settled_end = fixtures.DEFAULT_BUILD_TIME
        self.store_landing(PULL_REQUEST, landed_at=int(time.time()) - 60)
        self._store_row(window_ends_at=settled_end,
                        decided_at=settled_end + results.RUNS_SETTLING_SECONDS,
                        landed_at=int(time.time()) - 60)

        self.assertEqual(self._asked(fixtures.StubRunHistory({TEST: []})), 0)


class TestListingOrder(fixtures.DatabaseTest):
    """What `answered_tests` groups, orders and narrows by.

    A row is one test, so every figure is a reduction of that test's convictions, and the rate increase
    a page sorts by is the one it prints: both go through `rate_increase_for_counts` — the sqlite
    function `db.connect` registers — rather than through a stored column or the formula re-spelled in
    SQL.
    """

    def _convict(self, number: int, test_name: str, verdict: str, runs_after: int,
                 failed_after: int, recent_runs: Optional[int] = None,
                 recent_failed: Optional[int] = None,
                 landed_at: Optional[int] = LANDED_AT,
                 builder: str = fixtures.LAYOUT_BUILDER, builder_id: int = 7,
                 checked_at: Optional[int] = None,
                 rule: str = config.CLEAN_TREE) -> int:
        build_id = self.store_build(number, flaky={test_name: rule}, pr_id=number,
                                    pr_title='A change that landed', sha='a' * 40,
                                    builder=builder, builder_id=builder_id)
        with self.connection:
            self.connection.execute(
                '''INSERT INTO escape_verdicts (
                    build_id, test_name, verdict, runs_before, failed_before, runs_after,
                    failed_after, landed_at, window_ends_at, decided_at, recent_runs,
                    recent_failed, recent_checked_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (build_id, test_name, verdict, 4, 0, runs_after, failed_after, landed_at,
                 LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS, LANDED_AT, recent_runs, recent_failed,
                 (checked_at or LANDED_AT) if recent_runs is not None else None),
            )
        return build_id

    def _listed(self, verdict: str, **keywords: object) -> escapes.TestPage:
        return escapes.answered_tests(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                      fixtures.DEFAULT_BUILD_TIME + DAY, verdict, **keywords)

    def _names(self, verdict: str, **keywords: object) -> list:
        return [one.test_name for one in self._listed(verdict, **keywords).tests]

    def _configuration_of(self, build_id: int) -> results.Configuration:
        """One build's whole configuration, so a row's link is compared on every field it is built
        from — the fixtures differ only in flavor, which is the field a partial comparison drops."""
        return results.Configuration.of_build(self.connection.execute(
            'SELECT suite, platform, style, flavor FROM build_verdicts WHERE build_id = ?',
            (build_id,)).fetchone())

    def test_the_registered_function_answers_in_sql_what_the_dataclass_answers_in_python(self) -> None:
        """One definition, called from two places: if these ever disagree the page is printing a
        figure it did not sort by. The listed figure is a percentage, since that is the number a
        reader filters against."""
        self._convict(1, TEST, escapes.ESCAPED, runs_after=8, failed_after=1)
        row = self.connection.execute(
            f'SELECT {config.ESCAPE_INCREASE_FUNCTION}(runs_before, failed_before, runs_after, '
            'failed_after) AS increase FROM escape_verdicts').fetchone()
        listed = self._listed(escapes.ESCAPED).tests[0]
        self.assertAlmostEqual(row['increase'] * 100, listed.rate_increase_pct, places=12)
        self.assertIsNone(listed.damage_pct)

    def test_one_row_per_test_however_many_convictions_it_has(self) -> None:
        """The whole of the grouping: a cascade of convictions on one test is one row that says how
        many, not a page of rows a reader has to add up themselves."""
        self._convict(1, TEST, escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(2, TEST, escapes.ESCAPED, runs_after=10, failed_after=7)
        self._convict(3, 'fast/other.html', escapes.ESCAPED, runs_after=10, failed_after=1)
        listed = self._listed(escapes.ESCAPED)
        self.assertEqual(listed.total, 2)
        grouped = {one.test_name: one.convictions for one in listed.tests}
        self.assertEqual(grouped, {TEST: 2, 'fast/other.html': 1})

    def test_a_row_collapses_every_queue_that_convicted_the_test_into_one_row(self) -> None:
        self._convict(1, TEST, escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(2, TEST, escapes.ESCAPED, runs_after=10, failed_after=5,
                      builder=fixtures.GTK_BUILDER, builder_id=9)
        listed = self._listed(escapes.ESCAPED).tests
        self.assertEqual([(one.test_name, one.convictions) for one in listed], [(TEST, 2)])

    def test_a_row_carries_the_distinct_set_of_rules_that_convicted_the_test(self) -> None:
        self._convict(1, TEST, escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(2, TEST, escapes.ESCAPED, runs_after=10, failed_after=5,
                      rule=config.DIRTY_TREE)
        self._convict(3, TEST, escapes.ESCAPED, runs_after=10, failed_after=5)
        self.assertEqual(self._listed(escapes.ESCAPED).tests[0].rules,
                         tuple(sorted((config.CLEAN_TREE, config.DIRTY_TREE))))

    def test_a_row_reports_the_earliest_landing_and_the_worst_rate_increase(self) -> None:
        """Neither is a statistic over the group: the earliest landing is when the test started
        failing, and the worst conviction is the one worth reading. Averaging two bounds would report a
        landing that never happened and a rise nothing measured."""
        self._convict(1, TEST, escapes.ESCAPED, runs_after=10, failed_after=1,
                      landed_at=LANDED_AT + 600)
        self._convict(2, TEST, escapes.ESCAPED, runs_after=10, failed_after=9, landed_at=LANDED_AT)
        listed = self._listed(escapes.ESCAPED).tests[0]
        self.assertEqual(listed.landed_at, LANDED_AT)
        self.assertAlmostEqual(listed.rate_increase_pct,
                               100 * escapes.rate_increase_for_counts(4, 0, 10, 9), places=12)

    def test_an_unknown_landing_time_does_not_hide_a_known_one_in_the_same_group(self) -> None:
        """MIN passes over the nulls, so a group holding one backfilled row still reports the earliest
        landing it does know; a group of nothing but nulls reports none."""
        self._convict(1, TEST, escapes.ESCAPED, runs_after=10, failed_after=5, landed_at=None)
        self._convict(2, TEST, escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(3, 'fast/unknown.html', escapes.ESCAPED, runs_after=10, failed_after=5,
                      landed_at=None)
        landings = {one.test_name: one.landed_at for one in self._listed(escapes.ESCAPED).tests}
        self.assertEqual(landings, {TEST: LANDED_AT, 'fast/unknown.html': None})

    def test_a_row_reports_the_damage_at_the_latest_currency_check_it_holds(self) -> None:
        """The currency answers were asked on different days, and only the newest describes what main
        is doing now. A group where nobody has been asked reports none, not zero."""
        self._convict(1, TEST, escapes.ESCAPED, runs_after=10, failed_after=5, recent_runs=40,
                      recent_failed=20, checked_at=LANDED_AT)
        self._convict(2, TEST, escapes.ESCAPED, runs_after=10, failed_after=5, recent_runs=50,
                      recent_failed=5, checked_at=LANDED_AT + 600)
        self._convict(3, 'fast/unasked.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        damage = {one.test_name: one.damage_pct for one in self._listed(escapes.ESCAPED).tests}
        self.assertEqual(damage, {TEST: 10.0, 'fast/unasked.html': None})

    def test_two_checks_at_the_same_moment_report_the_worse_rate(self) -> None:
        """Measured on live data: the assess pass asks about many convictions of one test in one run,
        so they share a `recent_checked_at` to the second and their counts differ — 30 convictions
        answering 5 of 100 beside 31 answering 4 of 176. Breaking that tie on arrival order would make
        the figure depend on the query plan, so the worse rate wins."""
        self._convict(1, TEST, escapes.ESCAPED, runs_after=10, failed_after=5, recent_runs=176,
                      recent_failed=4, checked_at=LANDED_AT)
        self._convict(2, TEST, escapes.ESCAPED, runs_after=10, failed_after=5, recent_runs=100,
                      recent_failed=5, checked_at=LANDED_AT)
        self.assertEqual(self._listed(escapes.ESCAPED).tests[0].damage_pct, 5.0)

    def test_a_check_that_got_no_runs_does_not_outrank_a_real_rate_at_the_same_moment(self) -> None:
        """No runs back is an absence of evidence, not a lower rate, so it must not blank a cell
        another conviction answered in the same second."""
        self._convict(1, TEST, escapes.ESCAPED, runs_after=10, failed_after=5, recent_runs=0,
                      recent_failed=0, checked_at=LANDED_AT)
        self._convict(2, TEST, escapes.ESCAPED, runs_after=10, failed_after=5, recent_runs=40,
                      recent_failed=2, checked_at=LANDED_AT)
        self.assertEqual(self._listed(escapes.ESCAPED).tests[0].damage_pct, 5.0)

    def test_a_row_carries_the_configuration_of_the_conviction_it_took_its_increase_from(self) -> None:
        """A test convicted on several queues has no single configuration, and the row's own figure
        came from one conviction: the test-history link it offers is that conviction's.

        Compared whole rather than field by field. The two fixture builds report the same platform and
        the same style — both carry `CONFIGURATION_PROPERTIES` — and differ only in the flavor their
        builder name implies, so a comparison of platform and style alone passed whichever build won.
        """
        self._convict(1, TEST, escapes.ESCAPED, runs_after=10, failed_after=1)
        worst_build = self._convict(2, TEST, escapes.ESCAPED, runs_after=10, failed_after=9,
                                    builder=fixtures.GTK_BUILDER, builder_id=9)
        expected = self._configuration_of(worst_build)
        self.assertNotEqual(expected, self._configuration_of(
            self.connection.execute('SELECT MIN(build_id) FROM escape_verdicts').fetchone()[0]))
        self.assertEqual(self._listed(escapes.ESCAPED).tests[0].configuration, expected)

    def test_a_group_with_no_bound_at_all_still_names_one_configuration(self) -> None:
        """Every conviction of a category main answered nothing about has a null bound, so there is no
        worst one; the newest is named rather than an arbitrary row, so two renders agree."""
        older = self._convict(1, TEST, escapes.NO_RUNS, runs_after=0, failed_after=0)
        newest = self._convict(2, TEST, escapes.NO_RUNS, runs_after=0, failed_after=0,
                               builder=fixtures.GTK_BUILDER, builder_id=9)
        expected = self._configuration_of(newest)
        self.assertNotEqual(expected, self._configuration_of(older))
        self.assertEqual(self._listed(escapes.NO_RUNS).tests[0].configuration, expected)

    def test_ordering_by_the_rate_increase_puts_the_worst_landing_first(self) -> None:
        """Which is what the page asks for by default — the default itself lives in the route, the
        way the convicted-tests table's does, so here it is the keys that are passed in."""
        self._convict(1, 'fast/thin.html', escapes.ESCAPED, runs_after=100, failed_after=1)
        self._convict(2, 'fast/hard.html', escapes.ESCAPED, runs_after=10, failed_after=10)
        self._convict(3, 'fast/half.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        keys = filters.sort_keys(filters.ESCAPES, (('increase', True),))
        self.assertEqual(self._names(escapes.ESCAPED, sort_keys=keys),
                         ['fast/hard.html', 'fast/half.html', 'fast/thin.html'])

    def test_ordering_by_a_count_reads_the_group_s_own_count(self) -> None:
        self._convict(1, 'fast/once.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(2, 'fast/twice.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(3, 'fast/twice.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        keys = filters.sort_keys(filters.ESCAPES, (('convictions', True),))
        self.assertEqual(self._names(escapes.ESCAPED, sort_keys=keys),
                         ['fast/twice.html', 'fast/once.html'])

    def test_no_sort_key_at_all_still_leaves_a_total_order(self) -> None:
        """The tiebreak is the group key and is appended unconditionally, so a caller that passes no
        key still gets an order two queries cannot disagree about."""
        self._convict(1, 'fast/a.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(2, 'fast/b.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        self.assertEqual(self._names(escapes.ESCAPED), ['fast/a.html', 'fast/b.html'])

    def test_a_sort_key_the_request_asked_for_leads_the_fallback(self) -> None:
        self._convict(1, 'fast/zzz.html', escapes.ESCAPED, runs_after=10, failed_after=10)
        self._convict(2, 'fast/aaa.html', escapes.ESCAPED, runs_after=10, failed_after=1)
        ascending = filters.sort_keys(filters.ESCAPES, (('test', False),))
        self.assertEqual(self._names(escapes.ESCAPED, sort_keys=ascending),
                         ['fast/aaa.html', 'fast/zzz.html'])

    def test_a_row_with_no_evidence_sorts_last_whichever_way_the_increase_is_read(self) -> None:
        """NO_RUNS stores no run after the landing, so its bound is None: sqlite would lead an
        ascending page with it, and a row that answers nothing must not outrank one that does."""
        self._convict(1, 'fast/none.html', escapes.NO_RUNS, runs_after=0, failed_after=0)
        self._convict(2, 'fast/some.html', escapes.NO_RUNS, runs_after=10, failed_after=2)
        for descending in (True, False):
            keys = filters.sort_keys(filters.ESCAPES, (('increase', descending),))
            self.assertEqual(self._names(escapes.NO_RUNS, sort_keys=keys),
                             ['fast/some.html', 'fast/none.html'], descending)

    def test_a_row_with_no_landing_time_sorts_last_too(self) -> None:
        self._convict(1, 'fast/unknown.html', escapes.ESCAPED, runs_after=10, failed_after=5,
                      landed_at=None)
        self._convict(2, 'fast/known.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        for descending in (True, False):
            keys = filters.sort_keys(filters.ESCAPES, (('landed', descending),))
            self.assertEqual(self._names(escapes.ESCAPED, sort_keys=keys),
                             ['fast/known.html', 'fast/unknown.html'], descending)

    def test_a_condition_narrows_the_listing_and_the_total_with_it(self) -> None:
        self._convict(1, 'fast/webgl/a.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(2, 'fast/forms/b.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        conditions = filters.parse(filters.ESCAPES, (('test', 'has', 'webgl'),))
        listed = self._listed(escapes.ESCAPED, conditions=conditions)
        self.assertEqual([one.test_name for one in listed.tests], ['fast/webgl/a.html'])
        self.assertEqual(listed.total, 1)

    def test_a_condition_on_a_derived_column_narrows_through_the_registered_function(self) -> None:
        """The column is the function scaled to a percentage, so `at least 50` means the 50% a reader
        sees in the cell."""
        self._convict(1, 'fast/hard.html', escapes.ESCAPED, runs_after=100, failed_after=95)
        self._convict(2, 'fast/thin.html', escapes.ESCAPED, runs_after=100, failed_after=1)
        conditions = filters.parse(filters.ESCAPES, (('increase', 'ge', '50'),))
        self.assertEqual([one.test_name for one in
                          self._listed(escapes.ESCAPED, conditions=conditions).tests],
                         ['fast/hard.html'])

    def test_an_aggregate_clause_narrows_by_the_group_and_not_by_one_conviction(self) -> None:
        """`at least 2 convictions` is a question about the row, which only a HAVING can answer: a
        WHERE runs before the grouping exists and would have nothing to compare."""
        self._convict(1, 'fast/once.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(2, 'fast/twice.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(3, 'fast/twice.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        conditions = filters.parse(filters.ESCAPES, (('convictions', 'ge', '2'),))
        self.assertEqual([one.test_name for one in
                          self._listed(escapes.ESCAPED, conditions=conditions).tests],
                         ['fast/twice.html'])

    def test_a_per_conviction_clause_keeps_the_test_and_narrows_what_its_row_counts(self) -> None:
        """A queue clause is a WHERE, so it asks which convictions the group is built from: the test
        is listed because one of its convictions is on that queue, and the row then counts that one
        alone."""
        self._convict(1, TEST, escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(2, TEST, escapes.ESCAPED, runs_after=10, failed_after=5,
                      builder=fixtures.GTK_BUILDER, builder_id=9)
        conditions = filters.parse(filters.ESCAPES, (('queue', 'eq', fixtures.GTK_BUILDER),))
        listed = self._listed(escapes.ESCAPED, conditions=conditions).tests
        self.assertEqual([(one.test_name, one.convictions) for one in listed],
                         [(TEST, 1)])

    def test_a_rule_set_clause_asks_about_every_rule_the_row_carries(self) -> None:
        """The same set operators `/tests` already offers on its own rule column, which `clause` sends
        to HAVING: `any of` is a question about the group, not about one of its convictions."""
        self._convict(1, 'fast/clean.html', escapes.ESCAPED, runs_after=10, failed_after=5)
        self._convict(2, 'fast/dirty.html', escapes.ESCAPED, runs_after=10, failed_after=5,
                      rule=config.DIRTY_TREE)
        conditions = filters.parse(filters.ESCAPES, (('rule', 'anyof', config.DIRTY_TREE),))
        self.assertEqual([one.test_name for one in
                          self._listed(escapes.ESCAPED, conditions=conditions).tests],
                         ['fast/dirty.html'])


class TestListingPages(fixtures.DatabaseTest):
    """The 200-row page size, now counted in TESTS: the remainder is reachable and counted."""

    def _convict_many(self, count: int, convictions_each: int = 1) -> None:
        number = 0
        for test in range(1, count + 1):
            for _ in range(convictions_each):
                number += 1
                build_id = self.store_build(
                    number, flaky={f'fast/f{test:03d}.html': config.CLEAN_TREE}, pr_id=number,
                    pr_title='A change that landed', sha='a' * 40)
                with self.connection:
                    self.connection.execute(
                        '''INSERT INTO escape_verdicts (
                            build_id, test_name, verdict, runs_before, failed_before, runs_after,
                            failed_after, landed_at, window_ends_at, decided_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                        (build_id, f'fast/f{test:03d}.html', escapes.ESCAPED, 4, 0, 10, test,
                         LANDED_AT, LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS, LANDED_AT),
                    )

    def _page(self, page: int, limit: int = 2) -> escapes.TestPage:
        return escapes.answered_tests(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                      fixtures.DEFAULT_BUILD_TIME + DAY, escapes.ESCAPED,
                                      limit=limit, page=page)

    # Three tests ranked differently by every sortable column: six columns, and the six permutations of
    # three rows, one each. Every value inside a column is distinct, so each order is total and its
    # descending form is the exact reverse — a sort that reached another column's alias, or an alias the
    # SELECT never carried, cannot come back with the right sequence by luck.
    RANKED_CONVICTIONS = {'fast/a.html': 4, 'fast/b.html': 3, 'fast/c.html': 2}
    RANKED_QUEUES = {'fast/a.html': 1, 'fast/b.html': 3, 'fast/c.html': 2}
    RANKED_LANDED = {'fast/a.html': 200, 'fast/b.html': 300, 'fast/c.html': 100}
    RANKED_FAILED_AFTER = {'fast/a.html': 5, 'fast/b.html': 2, 'fast/c.html': 9}
    RANKED_RECENT_FAILED = {'fast/a.html': 9, 'fast/b.html': 1, 'fast/c.html': 5}
    RANKED_BUILDERS = ((fixtures.LAYOUT_BUILDER, 7), (fixtures.GTK_BUILDER, 9),
                       (fixtures.WPE_BUILDER, 11))

    def _distinctly_ranked(self) -> dict:
        """Store those three tests, and return each sortable column's ascending order of them."""
        number = 0
        for test_name, convictions in self.RANKED_CONVICTIONS.items():
            for index in range(convictions):
                number += 1
                builder, builder_id = self.RANKED_BUILDERS[
                    min(index, self.RANKED_QUEUES[test_name] - 1)]
                build_id = self.store_build(number, flaky={test_name: config.CLEAN_TREE},
                                            pr_id=number, pr_title='A change that landed',
                                            sha='a' * 40, builder=builder, builder_id=builder_id)
                worst = index == 0
                with self.connection:
                    self.connection.execute(
                        '''INSERT INTO escape_verdicts (
                            build_id, test_name, verdict, runs_before, failed_before, runs_after,
                            failed_after, landed_at, window_ends_at, decided_at, recent_runs,
                            recent_failed, recent_checked_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (build_id, test_name, escapes.ESCAPED, 4, 0, 10,
                         self.RANKED_FAILED_AFTER[test_name] if worst else 1,
                         LANDED_AT + self.RANKED_LANDED[test_name],
                         LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS, LANDED_AT,
                         100 if worst else None,
                         self.RANKED_RECENT_FAILED[test_name] if worst else None,
                         LANDED_AT if worst else None),
                    )
        return {
            'test': sorted(self.RANKED_CONVICTIONS),
            'convictions': self._ascending(self.RANKED_CONVICTIONS),
            'queues': self._ascending(self.RANKED_QUEUES),
            'landed': self._ascending(self.RANKED_LANDED),
            'increase': self._ascending(self.RANKED_FAILED_AFTER),
            'damage': self._ascending(self.RANKED_RECENT_FAILED),
        }

    @staticmethod
    def _ascending(ranking: dict) -> list:
        return [test_name for test_name, _ in sorted(ranking.items(), key=lambda pair: pair[1])]

    def test_a_page_reports_the_whole_set_it_was_taken_from(self) -> None:
        self._convict_many(5)
        first = self._page(1)
        self.assertEqual((first.shown, first.total, first.pages, first.number), (2, 5, 3, 1))
        self.assertEqual((first.first, first.last, first.remaining), (1, 2, 3))
        self.assertTrue(first.truncated)

    def test_the_total_counts_groups_and_not_the_convictions_inside_them(self) -> None:
        """Counting rows would page over a set three times the size of the one the table shows, so the
        last pages would be empty and the remainder would be a number nothing reaches."""
        self._convict_many(5, convictions_each=3)
        self.assertEqual(self._page(1).total, 5)

    def test_every_row_is_reachable_across_the_pages_and_none_is_reached_twice(self) -> None:
        """The point of a total order behind LIMIT/OFFSET: a partial one drops a row from one page
        and repeats it on the next."""
        self._convict_many(5)
        seen = []
        for number in range(1, 4):
            seen.extend(one.test_name for one in self._page(number).tests)
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)

    def test_the_last_page_has_no_next_and_the_first_has_no_previous(self) -> None:
        self._convict_many(5)
        self.assertIsNone(self._page(1).previous_page)
        self.assertEqual(self._page(1).next_page, 2)
        last = self._page(3)
        self.assertEqual((last.shown, last.remaining), (1, 0))
        self.assertIsNone(last.next_page)
        self.assertEqual(last.previous_page, 2)

    def test_a_page_past_the_end_is_answered_with_the_last_page(self) -> None:
        """A number in a URL, or a reader who narrowed the set while standing on page 3: an empty
        table reads as "nothing matched", which is not what happened."""
        self._convict_many(5)
        clamped = self._page(9)
        self.assertEqual(clamped.number, 3)
        self.assertEqual(clamped.shown, 1)

    def test_a_page_below_the_first_is_the_first(self) -> None:
        self._convict_many(5)
        self.assertEqual((self._page(0).number, self._page(-3).number), (1, 1))

    def test_an_empty_set_is_one_page_of_nothing_rather_than_page_one_of_zero(self) -> None:
        empty = self._page(1)
        self.assertEqual((empty.total, empty.shown, empty.pages, empty.number), (0, 0, 1, 1))
        self.assertEqual((empty.first, empty.last, empty.remaining), (0, 0, 0))
        self.assertFalse(empty.truncated)
        self.assertIsNone(empty.next_page)

    def test_the_page_size_is_bounded_below_by_one_row(self) -> None:
        """A limit of zero would make every page empty and the remainder unreachable, which is the
        defect paging exists to close."""
        self._convict_many(3)
        self.assertEqual(self._page(1, limit=0).shown, 1)

    def test_every_sortable_aggregate_is_selected_under_the_name_it_is_ordered_by(self) -> None:
        """An aggregate orders by its output alias, so a registered column left out of the row query's
        SELECT would make a reader's sort an sqlite error. The SELECT is built from the registry for
        exactly this reason; this is the check that says so.

        Asserted against the RENDERED rows and their order. It used to assert the page's `total`, which
        comes off a count subquery no ORDER BY reaches — so it proved only that no exception was raised,
        and every column's order went unread. `_distinctly_ranked` gives each column a different
        permutation of the three tests, so a sort that reached the wrong column changes the answer.
        """
        expected = self._distinctly_ranked()
        for name in filters.ESCAPES.sortable_names:
            for descending in (True, False):
                keys = filters.sort_keys(filters.ESCAPES, ((name, descending),))
                page = escapes.answered_tests(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                              fixtures.DEFAULT_BUILD_TIME + DAY, escapes.ESCAPED,
                                              sort_keys=keys)
                ascending = expected[name]
                wanted = list(reversed(ascending)) if descending else ascending
                self.assertEqual([one.test_name for one in page.tests], wanted,
                                 f'{name}:{descending}')


class TestMergedEscapeCategory(fixtures.DatabaseTest):
    """ESCAPED and FAILS_ON_MAIN as one listed bucket, with the baseline demoted to a split.

    The stored verdicts are untouched by the fold — nothing here writes a verdict the assess pass did
    not — so every test in this class stores both names and asks what the page makes of them.
    """

    def _convict(self, number: int, test_name: str, verdict: str, runs_after: int = 96,
                 failed_after: int = 48, failed_before: int = 0,
                 recent_runs: Optional[int] = None,
                 recent_failed: Optional[int] = None) -> int:
        build_id = self.store_build(number, flaky={test_name: config.CLEAN_TREE}, pr_id=number,
                                    pr_title='A change that landed', sha='a' * 40)
        with self.connection:
            self.connection.execute(
                '''INSERT INTO escape_verdicts (
                    build_id, test_name, verdict, runs_before, failed_before, runs_after,
                    failed_after, landed_at, window_ends_at, decided_at, recent_runs,
                    recent_failed, recent_checked_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (build_id, test_name, verdict, 100, failed_before, runs_after, failed_after,
                 LANDED_AT, LANDED_AT + escapes.ESCAPE_WINDOW_SECONDS, LANDED_AT, recent_runs,
                 recent_failed, LANDED_AT if recent_runs is not None else None),
            )
        return build_id

    def _tally(self) -> escapes.Tally:
        return escapes.tally(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                             fixtures.DEFAULT_BUILD_TIME + DAY)

    def _subcategories(self) -> escapes.Subcategories:
        return escapes.escape_subcategories(self.connection, fixtures.DEFAULT_BUILD_TIME - DAY,
                                            fixtures.DEFAULT_BUILD_TIME + DAY)

    def _names(self, category: str, conditions: tuple = ()) -> list:
        return [one.test_name for one in escapes.answered_tests(
            self.connection, fixtures.DEFAULT_BUILD_TIME - DAY, fixtures.DEFAULT_BUILD_TIME + DAY,
            escapes.category_verdicts(category), conditions=conditions).tests]

    def test_the_listed_categories_partition_every_stored_verdict_exactly_once(self) -> None:
        """A verdict in no category would vanish from the pane, and one in two would be counted
        twice; the pane's tally is a sum over `by_verdict`, so both failures would be silent."""
        listed = [verdict for category in escapes.CATEGORIES
                  for verdict in escapes.category_verdicts(category)]
        self.assertEqual(sorted(listed), sorted(escapes.VERDICTS))
        self.assertEqual(len(listed), len(set(listed)))

    def test_fails_on_main_is_not_a_category_of_its_own_but_is_shown_under_escaped(self) -> None:
        self.assertNotIn(escapes.FAILS_ON_MAIN, escapes.CATEGORIES)
        self.assertIn(escapes.FAILS_ON_MAIN, escapes.category_verdicts(escapes.ESCAPED))
        self.assertEqual(escapes.category_of(escapes.FAILS_ON_MAIN), escapes.ESCAPED)

    def test_a_stored_verdict_this_page_does_not_list_still_stands_for_itself(self) -> None:
        """So a caller narrowing by a stored name gets that name's rows rather than no rows."""
        self.assertEqual(escapes.category_verdicts(escapes.CONTAINED), (escapes.CONTAINED,))
        self.assertEqual(escapes.category_of(escapes.CONTAINED), escapes.CONTAINED)

    def test_the_escaped_category_counts_both_halves_as_one_bucket(self) -> None:
        self._convict(1, 'fast/clean.html', escapes.ESCAPED)
        self._convict(2, 'fast/already.html', escapes.FAILS_ON_MAIN, failed_before=1)
        self._convict(3, 'fast/other.html', escapes.FAILS_ON_MAIN, failed_before=7)
        self._convict(4, 'fast/contained.html', escapes.CONTAINED, failed_after=0)

        counted = self._tally()

        self.assertEqual(counted.by_category[escapes.ESCAPED], 3)
        self.assertEqual(counted.escaped, 3)
        self.assertEqual(counted.by_category[escapes.CONTAINED], 1)
        self.assertNotIn(escapes.FAILS_ON_MAIN, counted.by_category)

    def test_the_stored_counts_are_left_exactly_as_the_assess_pass_wrote_them(self) -> None:
        """The fold is what the page shows, not a rewrite: that is what makes it reversible."""
        self._convict(1, 'fast/already.html', escapes.FAILS_ON_MAIN, failed_before=1)
        self._tally()
        self._subcategories()
        self._names(escapes.ESCAPED)
        stored = self.connection.execute(
            'SELECT verdict, failed_before FROM escape_verdicts').fetchall()
        self.assertEqual([(row['verdict'], row['failed_before']) for row in stored],
                         [(escapes.FAILS_ON_MAIN, 1)])

    def test_a_category_with_no_conviction_in_either_half_reads_as_a_zero(self) -> None:
        self._convict(1, 'fast/contained.html', escapes.CONTAINED, failed_after=0)
        self.assertEqual(self._tally().by_category[escapes.ESCAPED], 0)

    def test_the_listing_shows_both_halves_under_the_one_category(self) -> None:
        self._convict(1, 'fast/clean.html', escapes.ESCAPED)
        self._convict(2, 'fast/already.html', escapes.FAILS_ON_MAIN, failed_before=1)
        self._convict(3, 'fast/contained.html', escapes.CONTAINED, failed_after=0)
        self.assertEqual(sorted(self._names(escapes.ESCAPED)),
                         ['fast/already.html', 'fast/clean.html'])

    def test_a_verdict_filter_still_reaches_either_half_on_its_own(self) -> None:
        """The reader's own clause is applied on top of the category, so the merged bucket is not a
        one-way door: asking for one stored name returns that name's rows and only those."""
        self._convict(1, 'fast/clean.html', escapes.ESCAPED)
        self._convict(2, 'fast/already.html', escapes.FAILS_ON_MAIN, failed_before=1)
        for verdict, expected in ((escapes.FAILS_ON_MAIN, ['fast/already.html']),
                                  (escapes.ESCAPED, ['fast/clean.html'])):
            conditions = filters.parse(filters.ESCAPES, (('verdict', 'eq', verdict),))
            self.assertEqual(len(conditions), 1, f'{verdict} did not parse as a verdict filter')
            self.assertEqual(self._names(escapes.ESCAPED, conditions=conditions), expected)

    def test_the_baseline_split_partitions_the_merged_bucket(self) -> None:
        self._convict(1, 'fast/clean.html', escapes.ESCAPED)
        self._convict(2, 'fast/already.html', escapes.FAILS_ON_MAIN, failed_before=1)
        self._convict(3, 'fast/other.html', escapes.FAILS_ON_MAIN, failed_before=7)
        self._convict(4, 'fast/contained.html', escapes.CONTAINED, failed_after=0)

        split = self._subcategories()

        self.assertEqual((split.baseline_clean, split.baseline_failing), (1, 2))
        self.assertEqual(split.baseline_total, 3)
        self.assertEqual(split.baseline_total, split.total)
        self.assertEqual(split.baseline_total, split.significance_total)

    def test_every_split_is_counted_over_the_whole_merged_bucket(self) -> None:
        """A split counted over less than the bucket above it would print partitions of a number that
        is not the one on the entry."""
        self._convict(1, 'fast/clean.html', escapes.ESCAPED, recent_runs=18, recent_failed=11)
        self._convict(2, 'fast/already.html', escapes.FAILS_ON_MAIN, failed_before=1)

        split = self._subcategories()

        self.assertEqual((split.still_failing, split.unchecked), (1, 1))
        self.assertEqual(split.total, 2)
        self.assertEqual(split.significance_total, 2)

    def test_the_distinct_test_count_is_the_tests_and_not_the_convictions(self) -> None:
        """One landed regression makes a fresh conviction on every later pull request whose build
        trips the same test, so the two numbers are far apart and printing only the convictions
        invites reading them as separate regressions."""
        self._convict(1, 'fast/poisoned.html', escapes.ESCAPED)
        self._convict(2, 'fast/poisoned.html', escapes.FAILS_ON_MAIN, failed_before=1)
        self._convict(3, 'fast/poisoned.html', escapes.FAILS_ON_MAIN, failed_before=2)
        self._convict(4, 'fast/other.html', escapes.FAILS_ON_MAIN, failed_before=1)

        split = self._subcategories()

        self.assertEqual(split.total, 4)
        self.assertEqual(split.distinct_tests, 2)

    def test_the_distinct_test_count_is_narrowed_with_the_bucket_it_describes(self) -> None:
        self._convict(1, 'fast/clean.html', escapes.ESCAPED)
        self.assertEqual(escapes.escape_subcategories(
            self.connection, fixtures.DEFAULT_BUILD_TIME - DAY, fixtures.DEFAULT_BUILD_TIME + DAY,
            builders=(fixtures.GTK_BUILDER,)).distinct_tests, 0)
        self.assertEqual(escapes.escape_subcategories(
            self.connection, fixtures.DEFAULT_BUILD_TIME - DAY, fixtures.DEFAULT_BUILD_TIME + DAY,
            builders=(fixtures.LAYOUT_BUILDER,)).distinct_tests, 1)

    def test_an_empty_bucket_names_no_tests(self) -> None:
        self._convict(1, 'fast/contained.html', escapes.CONTAINED, failed_after=0)
        self.assertEqual(self._subcategories().distinct_tests, 0)
