"""The pages, and the promise that serving one cannot reach the network."""

from __future__ import annotations

import datetime
import re
import time
from unittest import mock

from ews_dashboard import config, results
from ews_dashboard.analysis import false_positive, trend
from ews_dashboard.web import app as web_app, formatting
from tests import fixtures

RELIABLE = 99.5
UNRELIABLE = 40.0


class WebTest(fixtures.DatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.client = web_app.create_app(self.database_path).test_client()

    def store_build(self, *arguments: object, **keywords: object) -> int:
        keywords.setdefault('started_at', trend.day_bounds(trend.today())[0] + 3600)
        return super().store_build(*arguments, **keywords)

    def classify_everything(self, pass_rates: dict) -> None:
        # Ends at the end of the UTC day, not at now, so a build stamped later today is still in
        # the window the pages query. Ending at time.time() left the suite failing until 01:00 UTC.
        today = trend.today()
        window = (trend.day_bounds(today)[0] - 86400, trend.day_bounds(today)[1])
        false_positive.rate(
            self.connection,
            false_positive.live_classifier(self.connection, fixtures.StubHistory(pass_rates)),
            *window,
        )

    def record_refresh(self, finished_at: int) -> None:
        with self.connection:
            self.connection.execute(
                'INSERT INTO refresh_runs (started_at, finished_at) VALUES (?, ?)',
                (finished_at - 60, finished_at),
            )

    def page(self, path: str = '/') -> str:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)


class TestLanding(WebTest):
    def test_an_empty_database_renders_and_says_so_rather_than_claiming_a_perfect_score(self) -> None:
        page = self.page('/')
        self.assertIn('No classified builds', page)
        self.assertIn(f'<span class="value">{formatting.MISSING}</span>', page)
        self.assertIn('0 of 0 failing builds', page)

    def test_a_never_refreshed_database_is_stale(self) -> None:
        self.assertIn('freshness stale', self.page('/'))

    def test_a_recent_refresh_is_not_stale(self) -> None:
        self.record_refresh(int(time.time()) - 300)
        self.assertNotIn('freshness stale', self.page('/'))

    def test_the_rate_appears_with_the_counts_behind_it(self) -> None:
        self.store_build(1, first=['fast/pre.html'], second=['fast/pre.html'], clean=[])
        self.store_build(2, first=['fast/real.html'], second=['fast/real.html'], clean=[])
        self.classify_everything({'fast/pre.html': UNRELIABLE, 'fast/real.html': RELIABLE})
        page = self.page('/')
        self.assertIn('<span class="value">50%</span>', page)
        self.assertIn('1 of 2 failing builds', page)

    def test_an_unclassified_build_is_disclosed_rather_than_scored(self) -> None:
        self.store_build(1, first=['fast/a.html'], second=['fast/a.html'], clean=[])
        page = self.page('/')
        self.assertIn('1 failing builds in this window have not been classified yet', page)

    def test_convictions_are_listed_per_rule_including_the_rules_that_never_fired(self) -> None:
        self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE})
        page = self.page('/')
        for rule in config.FLAKINESS_RULES:
            self.assertIn(rule, page)

    def test_the_trend_is_drawn_as_inline_svg_with_hover_titles(self) -> None:
        self.store_build(1, first=['fast/pre.html'], second=['fast/pre.html'], clean=[])
        self.classify_everything({'fast/pre.html': UNRELIABLE})
        page = self.page('/')
        self.assertIn('<svg', page)
        self.assertIn('builds blamed an author for noise', page)
        self.assertNotIn('<script', page)

    def test_a_window_choice_is_linkable_and_an_unknown_one_falls_back(self) -> None:
        self.assertIn('last 30 days', self.page('/?days=30').lower())
        self.assertIn('last 7 days', self.page('/?days=nonsense').lower())
        self.assertIn('last 7 days', self.page('/?days=9999').lower())


class TestExplore(WebTest):
    def test_a_convicted_test_links_to_its_history_and_its_build(self) -> None:
        self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE})
        page = self.page('/explore')
        self.assertIn('fast/a.html', page)
        self.assertIn('results.webkit.org/?suite=layout-tests&amp;test=fast%2Fa.html'
                      '&amp;platform=mac&amp;style=release&amp;flavor=wk2', page)
        self.assertIn('ews-build.webkit.org/#/builders/7/builds/1', page)
        self.assertIn('github.com/WebKit/WebKit/pull/12345', page)

    def test_a_rule_with_nothing_convicted_says_so(self) -> None:
        self.assertIn('Nothing convicted under this rule', self.page('/explore'))

    def test_a_builder_filter_narrows_the_page(self) -> None:
        self.store_build(1, flaky={'fast/layout.html': config.CLEAN_TREE})
        self.store_build(2, flaky={'fast/api.html': config.CLEAN_TREE},
                         builder=fixtures.API_BUILDER, builder_id=9)
        page = self.page(f'/explore?builder={fixtures.API_BUILDER}')
        self.assertIn('fast/api.html', page)
        self.assertNotIn('fast/layout.html', page)

    def test_an_unknown_builder_is_ignored_rather_than_filtering_everything_out(self) -> None:
        self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE})
        self.assertIn('fast/a.html', self.page('/explore?builder=not-a-queue'))

    def test_a_queue_that_never_asked_about_flakiness_is_visible(self) -> None:
        self.store_build(1, builder=fixtures.API_BUILDER, builder_id=9)
        self.assertIn(fixtures.API_BUILDER, self.page('/explore'))

    def _convict_many(self, count: int) -> None:
        """One build per convicted test, since a rule's list is one row per test."""
        for number in range(1, count + 1):
            self.store_build(number, flaky={f'fast/flake{number:03d}.html': config.CLEAN_TREE})

    def test_a_rule_previews_its_worst_offenders_and_links_to_the_rest(self) -> None:
        self._convict_many(web_app.RULE_TESTS_PREVIEWED + 3)
        page = self.page('/explore')
        listed = page.count('all configurations')
        self.assertEqual(listed, web_app.RULE_TESTS_PREVIEWED)
        self.assertIn(f'Show all {web_app.RULE_TESTS_PREVIEWED + 3}', page)
        self.assertIn(f'rule={config.CLEAN_TREE}', page)

    def test_one_rule_asked_for_by_name_is_listed_alone_and_deeply(self) -> None:
        self._convict_many(web_app.RULE_TESTS_PREVIEWED + 3)
        page = self.page(f'/explore?rule={config.CLEAN_TREE}')
        self.assertEqual(page.count('all configurations'), web_app.RULE_TESTS_PREVIEWED + 3)
        self.assertNotIn('Show all', page)
        for rule in config.FLAKINESS_RULES:
            if rule != config.CLEAN_TREE:
                self.assertNotIn(f'<div class="section" id="{rule}">', page)

    def test_a_rule_name_that_is_not_a_rule_falls_back_to_previewing_every_rule(self) -> None:
        self._convict_many(1)
        page = self.page('/explore?rule=NotARule')
        for rule in config.FLAKINESS_RULES:
            self.assertIn(f'<div class="section" id="{rule}">', page)


class TestExploreDrillDown(WebTest):
    """The builds pane and the pane for one selected build."""

    def cache_history(self, build_id: int, test_name: str, pass_pct: float) -> None:
        """Write the history row a refresh would have left for one of a build's surfaced tests, so
        the detail pane has an answer to read without a fetch of its own."""
        build_row = self.stored_build(build_id)
        self.cache_answer(
            results.Query(test_name, results.Configuration.of_build(build_row),
                          false_positive.base_commit_of(build_row) or ''),
            {'pass': pass_pct, 'fail': 100.0 - pass_pct, 'timeout': 0.0, 'crash': 0.0,
             'image': 0.0, 'audio': 0.0, 'text': 0.0, 'error': 0.0, 'warning': 0.0},
        )

    def verdict_span(self, verdict: str) -> str:
        """One verdict as the page shows it: the class it is styled by and the word it reads as.
        Spelled out rather than asked of `formatting`, so a filter that stopped distinguishing
        states cannot make its own expectation."""
        return f'<span class="state state-{verdict}">{formatting.VERDICT_WORDS[verdict]}</span>'

    def entry_classes(self, page: str, build_id: int) -> str:
        """The classes on one build's entry in the builds pane, found by the build it links to."""
        entry = re.search(rf'<a class="(entry[^"]*)" href="[^"]*build={build_id}"', page)
        self.assertIsNotNone(entry, f'no builds-pane entry linking to build {build_id}')
        return entry.group(1)

    def test_a_selected_build_lists_its_surfaced_tests_with_the_word_each_verdict_reads_as(self) -> None:
        build_id = self.store_build(1, first=['fast/pre.html', 'fast/real.html'],
                                    second=['fast/pre.html', 'fast/real.html'], clean=[])
        self.cache_history(build_id, 'fast/pre.html', UNRELIABLE)
        self.cache_history(build_id, 'fast/real.html', RELIABLE)
        self.classify_everything({'fast/pre.html': UNRELIABLE, 'fast/real.html': RELIABLE})
        page = self.page(f'/explore?build={build_id}')
        self.assertIn('fast/pre.html', page)
        self.assertIn('fast/real.html', page)
        self.assertIn(self.verdict_span(false_positive.PRE_EXISTING), page)
        self.assertIn(self.verdict_span(false_positive.REAL), page)

    def test_the_selected_build_is_marked_selected_in_the_builds_pane_and_another_build_is_not(self) -> None:
        selected = self.store_build(1, first=['fast/a.html'], second=['fast/a.html'], clean=[])
        other = self.store_build(2, first=['fast/b.html'], second=['fast/b.html'], clean=[])
        page = self.page(f'/explore?build={selected}')
        self.assertIn('selected', self.entry_classes(page, selected))
        self.assertNotIn('selected', self.entry_classes(page, other))

    def test_a_state_that_blames_noise_marks_its_builds_pane_entry_and_a_clean_one_does_not(self) -> None:
        noise = self.store_build(1, first=['fast/pre.html'], second=['fast/pre.html'], clean=[])
        clean = self.store_build(2, first=['fast/real.html'], second=['fast/real.html'], clean=[])
        self.classify_everything({'fast/pre.html': UNRELIABLE, 'fast/real.html': RELIABLE})
        page = self.page('/explore')
        self.assertIn('noise', self.entry_classes(page, noise))
        self.assertNotIn('noise', self.entry_classes(page, clean))

    def test_a_build_id_that_is_unknown_or_not_a_number_prompts_for_a_build_rather_than_raising(self) -> None:
        prompt = 'Pick a build to see the tests it showed its author'
        self.assertIn(prompt, self.page('/explore?build=999999'))
        self.assertIn(prompt, self.page('/explore?build=nonsense'))

    def test_a_test_filter_shows_only_the_surfaced_tests_whose_names_match_it(self) -> None:
        build_id = self.store_build(1, first=['fast/kept.html', 'fast/hidden.html'],
                                    second=['fast/kept.html', 'fast/hidden.html'], clean=[])
        page = self.page(f'/explore?build={build_id}&test=kept')
        self.assertIn('fast/kept.html', page)
        self.assertNotIn('fast/hidden.html', page)

    def test_a_test_filter_that_matches_nothing_says_so_rather_than_reading_as_an_empty_build(self) -> None:
        build_id = self.store_build(1, first=['fast/a.html'], second=['fast/a.html'], clean=[])
        page = self.page(f'/explore?build={build_id}&test=nomatch')
        self.assertIn('None of the 1 tests this build showed its author match', page)
        self.assertNotIn('This build showed its author no new failures.', page)

    def test_a_build_whose_history_cannot_be_believed_says_why_and_reads_as_not_looked_up(self) -> None:
        build_id = self.store_build(1, first=['fast/a.html'], second=['fast/a.html'], clean=[],
                                    identifier=None)
        page = self.page(f'/explore?build={build_id}')
        self.assertIn(false_positive.REASON_DESCRIPTIONS[false_positive.NO_BASE_COMMIT], page)
        self.assertIn(self.verdict_span(false_positive.UNQUERIED), page)

    def test_a_failing_build_no_refresh_has_classified_reads_as_unclassified_not_as_clean(self) -> None:
        build_id = self.store_build(1, first=['fast/a.html'], second=['fast/a.html'], clean=[])
        page = self.page(f'/explore?build={build_id}')
        self.assertIn('<span class="state state-unknown">unclassified</span>', page)
        self.assertIn('No refresh has classified this build yet, so nothing below has been looked '
                      'up.', page)
        self.assertNotIn(formatting.BUCKET_WORDS[false_positive.CLEAN], page)

    def test_the_builds_pane_discloses_how_many_failing_builds_it_is_not_showing(self) -> None:
        for number in (1, 2, 3):
            self.store_build(number, first=['fast/a.html'], second=['fast/a.html'], clean=[])
        with mock.patch.object(web_app, 'BUILDS_SHOWN', 2):
            page = self.page('/explore')
            self.assertIn('<span class="tally">2 of 3</span>', page)

    def test_a_build_that_surfaced_nothing_to_its_author_says_so_in_place_of_a_table(self) -> None:
        build_id = self.store_build(1, first=['fast/a.html'], second=[], clean=[])
        self.assertIn('This build showed its author no new failures.',
                      self.page(f'/explore?build={build_id}'))

    def test_a_classified_build_that_surfaced_nothing_reads_as_nothing_shown_not_unclassified(self) -> None:
        """It has no bucket and nothing to refresh, so reading as unclassified would send a reader
        to a refresh that would change nothing."""
        build_id = self.store_build(1, first=['fast/a.html'], second=[], clean=[])
        self.classify_everything({})
        page = self.page(f'/explore?build={build_id}')
        self.assertIn(f'<span class="state state-{formatting.NO_SURFACED}">'
                      f'{formatting.BUCKET_WORDS[formatting.NO_SURFACED]}</span>', page)
        self.assertNotIn('<span class="state state-unknown">unclassified</span>', page)


class TestReadOnly(WebTest):
    def test_serving_a_page_never_reaches_results_webkit_org(self) -> None:
        self.store_build(1, first=['fast/a.html'], second=['fast/a.html'], clean=[])
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=AssertionError('a page made an HTTP request')):
            self.assertEqual(self.client.get('/').status_code, 200)
            self.assertEqual(self.client.get('/explore').status_code, 200)

    def test_serving_a_page_writes_no_classification(self) -> None:
        self.store_build(1, first=['fast/a.html'], second=['fast/a.html'], clean=[])
        self.page('/')
        self.assertEqual(self.connection.execute(
            'SELECT COUNT(*) FROM build_classifications').fetchone()[0], 0)

    def test_serving_a_selected_build_never_reaches_results_webkit_org(self) -> None:
        build_id = self.store_build(1, first=['fast/a.html'], second=['fast/a.html'], clean=[])
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=AssertionError('a page made an HTTP request')):
            self.assertEqual(self.client.get(f'/explore?build={build_id}').status_code, 200)

    def test_serving_a_selected_build_writes_no_classification(self) -> None:
        build_id = self.store_build(1, first=['fast/a.html'], second=['fast/a.html'], clean=[])
        self.page(f'/explore?build={build_id}')
        self.assertEqual(self.connection.execute(
            'SELECT COUNT(*) FROM build_classifications').fetchone()[0], 0)


class TestFormatting(WebTest):
    def test_an_age_reads_as_an_age(self) -> None:
        self.assertEqual(formatting.age(None), 'never')
        self.assertEqual(formatting.age(30), 'moments ago')
        self.assertEqual(formatting.age(3600), '1 hour ago')
        self.assertEqual(formatting.age(7200), '2 hours ago')
        self.assertEqual(formatting.age(86400 * 3), '3 days ago')

    def test_a_missing_number_is_not_a_zero(self) -> None:
        self.assertEqual(formatting.percent(None), formatting.MISSING)
        self.assertEqual(formatting.count(None), formatting.MISSING)
        self.assertEqual(formatting.moment(None), formatting.MISSING)
        self.assertEqual(formatting.percent(12.5), '12.5%')
        self.assertEqual(formatting.count(1234), '1,234')

    def test_a_moment_is_reported_in_utc(self) -> None:
        at = datetime.datetime(2026, 8, 20, 15, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(formatting.moment(int(at.timestamp())), '2026-08-20 15:30 UTC')
