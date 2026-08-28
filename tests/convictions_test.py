"""Convictions are counted per build and test, from the answer that stood for each test."""

from __future__ import annotations

from ews_dashboard import config, suites
from ews_dashboard.analysis import convictions
from tests import fixtures

WINDOW = (fixtures.DEFAULT_BUILD_TIME - 86400, fixtures.DEFAULT_BUILD_TIME + 86400)


class TestByRule(fixtures.DatabaseTest):
    def test_a_rule_that_never_fired_reports_zero_rather_than_being_absent(self) -> None:
        self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE})
        counted = convictions.by_rule(self.connection, *WINDOW)
        self.assertEqual(set(counted), set(config.FLAKINESS_RULES))
        self.assertEqual(counted[config.CLEAN_TREE], 1)
        self.assertEqual(counted[config.BETWEEN_BUILDS], 0)

    def test_the_same_test_convicted_in_two_builds_counts_twice(self) -> None:
        self.store_build(1, flaky={'fast/a.html': config.DIRTY_TREE})
        self.store_build(2, flaky={'fast/a.html': config.DIRTY_TREE})
        self.assertEqual(convictions.by_rule(self.connection, *WINDOW)[config.DIRTY_TREE], 2)

    def test_a_query_with_no_answer_is_not_a_conviction(self) -> None:
        self.store_build(1, query_failed=['fast/a.html'])
        self.assertEqual(sum(convictions.by_rule(self.connection, *WINDOW).values()), 0)
        self.assertEqual(convictions.query_failures(self.connection, *WINDOW), 1)

    def test_builds_outside_the_window_are_not_counted(self) -> None:
        self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE},
                         started_at=WINDOW[0] - 86400)
        self.assertEqual(sum(convictions.by_rule(self.connection, *WINDOW).values()), 0)

    def test_the_rerun_answer_stands_where_it_disagrees_with_the_first_run(self) -> None:
        self.store_build(
            1,
            flaky_first_run={'fast/a.html': config.CLEAN_TREE, 'fast/only-first.html': config.DIRTY_TREE},
            flaky={'fast/a.html': config.DIRTY_TREE},
        )
        counted = convictions.by_rule(self.connection, *WINDOW)
        self.assertEqual(counted[config.CLEAN_TREE], 0)
        self.assertEqual(counted[config.DIRTY_TREE], 2)


class TestBuildsQueried(fixtures.DatabaseTest):
    def test_a_build_that_asked_and_convicted_nothing_still_counts_as_having_asked(self) -> None:
        self.store_build(1, flaky={})
        self.store_build(2)
        self.assertEqual(convictions.builds_queried(self.connection, *WINDOW), 1)


class TestScopedCounts(fixtures.DatabaseTest):
    """The landing page's cards narrow the same way its rule table and the explore page do, so a
    reader who picks one queue is not shown a rate computed over every queue."""

    def setUp(self) -> None:
        super().setUp()
        self.store_build(1, flaky={'fast/layout.html': config.CLEAN_TREE})
        self.store_build(2, flaky={'fast/api.html': config.CLEAN_TREE},
                         query_failed=['fast/unanswered.html'],
                         builder=fixtures.API_BUILDER, builder_id=9)

    def test_convictions_narrow_to_one_queue(self) -> None:
        counted = convictions.by_rule(self.connection, *WINDOW, builder=fixtures.API_BUILDER)
        self.assertEqual(counted[config.CLEAN_TREE], 1)
        self.assertEqual(sum(convictions.by_rule(self.connection, *WINDOW).values()), 2)

    def test_convictions_narrow_to_one_suite(self) -> None:
        suite = suites.suite_for_builder(fixtures.API_BUILDER).name
        counted = convictions.by_rule(self.connection, *WINDOW, suite=suite)
        self.assertEqual(counted[config.CLEAN_TREE], 1)

    def test_builds_that_asked_narrow_to_one_queue(self) -> None:
        self.assertEqual(convictions.builds_queried(self.connection, *WINDOW), 2)
        self.assertEqual(
            convictions.builds_queried(self.connection, *WINDOW, builder=fixtures.API_BUILDER), 1)

    def test_unanswered_queries_narrow_to_one_queue(self) -> None:
        self.assertEqual(
            convictions.query_failures(self.connection, *WINDOW, builder=fixtures.API_BUILDER), 1)
        self.assertEqual(
            convictions.query_failures(self.connection, *WINDOW,
                                       builder=fixtures.LAYOUT_BUILDER), 0)


class TestConvictedTests(fixtures.DatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.store_build(1, flaky={'fast/often.html': config.CLEAN_TREE},
                         started_at=fixtures.DEFAULT_BUILD_TIME)
        self.store_build(2, flaky={'fast/often.html': config.CLEAN_TREE},
                         started_at=fixtures.DEFAULT_BUILD_TIME + 600)
        self.store_build(3, flaky={'fast/rare.html': config.CLEAN_TREE},
                         builder=fixtures.API_BUILDER, builder_id=9)

    def _convicted(self, **filters: object) -> list:
        return convictions.convicted_tests(self.connection, config.CLEAN_TREE, *WINDOW,
                                           **filters).tests

    def test_most_convicted_first(self) -> None:
        self.assertEqual([test.test_name for test in self._convicted()],
                         ['fast/often.html', 'fast/rare.html'])
        self.assertEqual(self._convicted()[0].convictions, 2)

    def test_the_linked_build_is_the_most_recent_conviction(self) -> None:
        self.assertEqual(self._convicted()[0].build_number, 2)
        self.assertEqual(self._convicted()[0].last_seen, fixtures.DEFAULT_BUILD_TIME + 600)

    def test_the_configuration_travels_with_the_test_so_it_can_be_linked(self) -> None:
        configuration = self._convicted()[0].configuration
        self.assertEqual((configuration.suite, configuration.platform, configuration.style),
                         ('layout-tests', 'mac', 'release'))
        self.assertEqual(configuration.flavor, 'wk2')

    def test_a_builder_filter_narrows_the_list(self) -> None:
        self.assertEqual([test.test_name for test in self._convicted(builder=fixtures.API_BUILDER)],
                         ['fast/rare.html'])

    def test_a_suite_filter_narrows_the_list(self) -> None:
        self.assertEqual([test.test_name for test in self._convicted(suite='api-tests')],
                         ['fast/rare.html'])

    def test_the_limit_is_honoured(self) -> None:
        self.assertEqual(len(self._convicted(limit=1)), 1)

    def test_a_truncated_list_reports_how_many_it_left_out(self) -> None:
        convicted = convictions.convicted_tests(self.connection, config.CLEAN_TREE, *WINDOW, limit=1)
        self.assertEqual((convicted.total, convicted.truncated), (2, 1))

    def test_an_untruncated_list_says_it_cut_nothing(self) -> None:
        convicted = convictions.convicted_tests(self.connection, config.CLEAN_TREE, *WINDOW)
        self.assertEqual((convicted.total, convicted.truncated), (2, 0))

    def test_a_test_convicted_on_two_queues_reports_two_queues(self) -> None:
        self.store_build(4, flaky={'fast/rare.html': config.CLEAN_TREE})
        by_name = {test.test_name: test for test in self._convicted()}
        self.assertEqual(by_name['fast/rare.html'].queues, 2)


class TestQueueActivity(fixtures.DatabaseTest):
    def test_a_queue_that_never_asked_is_still_listed(self) -> None:
        self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE})
        self.store_build(2, builder=fixtures.API_BUILDER, builder_id=9)
        activity = {queue.builder: queue for queue in convictions.queue_activity(self.connection, *WINDOW)}
        self.assertEqual(activity[fixtures.API_BUILDER].builds_queried, 0)
        self.assertEqual(activity[fixtures.API_BUILDER].convictions, 0)
        self.assertEqual(activity[fixtures.LAYOUT_BUILDER].builds_queried, 1)
        self.assertEqual(activity[fixtures.LAYOUT_BUILDER].convictions, 1)

    def test_query_failures_are_reported_per_queue(self) -> None:
        self.store_build(1, query_failed=['fast/a.html', 'fast/b.html'])
        activity = convictions.queue_activity(self.connection, *WINDOW)
        self.assertEqual(activity[0].query_failures, 2)

    def test_busiest_queue_first(self) -> None:
        self.store_build(1, flaky={})
        self.store_build(2, flaky={})
        self.store_build(3, flaky={}, builder=fixtures.API_BUILDER, builder_id=9)
        self.assertEqual([queue.builder for queue in convictions.queue_activity(self.connection, *WINDOW)],
                         [fixtures.LAYOUT_BUILDER, fixtures.API_BUILDER])
