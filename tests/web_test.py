"""The pages, and the promise that serving one cannot reach the network."""

from __future__ import annotations

import datetime
import re
import time
from unittest import mock
from urllib.parse import parse_qs, quote, urlsplit

from ews_dashboard import config, results, suites
from ews_dashboard.analysis import escapes, false_positive, freshness, trend
from ews_dashboard.web import app as web_app, formatting
from tests import fixtures

RELIABLE = 99.5
UNRELIABLE = 40.0


class WebTest(fixtures.DatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.client = web_app.create_app(self.database_path).test_client()

    def store_build(self, *arguments: object, **keywords: object) -> int:
        keywords.setdefault('started_at', int(time.time()) - 3600)
        return super().store_build(*arguments, **keywords)

    def classify_everything(self, pass_rates: dict, days: int = 1) -> None:
        now = int(time.time())
        false_positive.rate(
            self.connection,
            false_positive.live_classifier(self.connection, fixtures.StubHistory(pass_rates)),
            now - days * 86400,
            now,
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


class TestWindow(WebTest):
    """Anchored to the end of the UTC day, a window held less than it claimed: half an hour past
    midnight the shortest one held half an hour of EWS, and a build from yesterday evening was
    outside a window said to cover a week."""

    JUST_AFTER_MIDNIGHT = int(datetime.datetime(2026, 8, 27, 0, 30,
                                               tzinfo=datetime.timezone.utc).timestamp())

    def page_at_midnight(self, path: str) -> str:
        with mock.patch('ews_dashboard.web.app.time.time',
                        return_value=self.JUST_AFTER_MIDNIGHT):
            return self.page(path)

    def test_a_window_reaches_a_full_span_back_from_now(self) -> None:
        with mock.patch('ews_dashboard.web.app.time.time',
                        return_value=self.JUST_AFTER_MIDNIGHT):
            window = web_app.Window.of_days(7)
        self.assertEqual((window.since, window.until),
                         (self.JUST_AFTER_MIDNIGHT - 7 * 86400, self.JUST_AFTER_MIDNIGHT))

    def test_a_build_from_the_far_edge_of_the_window_is_still_inside_it(self) -> None:
        """Six days and an hour old: inside a rolling week, outside a week that ended at midnight."""
        self.store_build(1, first=['fast/a.html'], second=['fast/a.html'], clean=[],
                         started_at=self.JUST_AFTER_MIDNIGHT - 6 * 86400 - 3600)
        self.assertNotIn('No failing builds in this window.',
                         self.page_at_midnight('/explore?days=7'))

    def test_a_build_older_than_the_window_is_left_out(self) -> None:
        self.store_build(1, first=['fast/a.html'], second=['fast/a.html'], clean=[],
                         started_at=self.JUST_AFTER_MIDNIGHT - 7 * 86400 - 3600)
        self.assertIn('No failing builds in this window.',
                      self.page_at_midnight('/explore?days=7'))


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


class TestFreshnessDismissal(WebTest):
    """Dismissing the freshness banner is a request rather than a click handler, since the app ships
    no JavaScript and a banner hidden on one page has to stay hidden on the next."""

    BANNER = 'class="freshness'

    def dismiss_link(self, path: str = '/') -> str:
        found = re.search(r'<a class="dismiss" href="([^"]+)"', self.page(path))
        self.assertIsNotNone(found, f'no dismissal control on {path}')
        return found.group(1).replace('&amp;', '&')

    def test_the_control_names_what_it_dismisses_and_where_it_returns(self) -> None:
        link = urlsplit(self.dismiss_link('/explore?days=14'))
        self.assertEqual(link.path, '/dismiss-freshness')
        arguments = parse_qs(link.query)
        self.assertEqual(arguments['state'], [freshness.current(self.connection).signature])
        self.assertEqual(arguments['next'], ['/explore?days=14'])

    def test_dismissing_returns_to_the_page_it_was_dismissed_from(self) -> None:
        response = self.client.get(self.dismiss_link('/explore?days=14'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/explore?days=14'),
                        response.headers['Location'])

    def test_a_dismissal_holds_on_every_page_and_not_only_the_one_it_was_made_from(self) -> None:
        self.client.get(self.dismiss_link('/'))
        self.assertNotIn(self.BANNER, self.page('/'))
        self.assertNotIn(self.BANNER, self.page('/explore'))

    def test_a_dismissal_does_not_outlive_the_banner_it_dismissed(self) -> None:
        """Hiding "nothing has refreshed this database yet" must not go on to hide a refresh that
        died, so the banner comes back once it has something else to say."""
        self.client.get(self.dismiss_link('/'))
        self.record_refresh(int(time.time()) - 300)
        self.assertIn(self.BANNER, self.page('/'))

    def test_a_dismissal_cannot_send_a_reader_off_the_site(self) -> None:
        for target in ('https://example.com/', '//example.com/', '/\\example.com'):
            response = self.client.get(f'/dismiss-freshness?state=x&next={quote(target)}')
            self.assertEqual(response.headers['Location'], '/', target)

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

    def test_the_chart_spans_the_window_the_rest_of_the_page_counts_over(self) -> None:
        self.assertIn('Blame noise over 14 days', self.page('/?days=14'))
        self.assertIn('Blame noise over 7 days', self.page('/'))

    def rolling_segments(self, page: str) -> int:
        """How many segments the rolling line the page drew is made of, one per pair of covered days."""
        drawn = re.findall(r'<line class="rolling"', page)
        self.assertTrue(drawn, 'the page drew no rolling line')
        return len(drawn)

    def test_a_rolling_average_is_linkable_and_an_unknown_one_falls_back(self) -> None:
        self.store_build(1, first=['fast/pre.html'], second=['fast/pre.html'], clean=[])
        self.classify_everything({'fast/pre.html': UNRELIABLE})
        self.assertIn('14-day rolling', self.page('/?rolling=14'))
        self.assertIn(f'{trend.ROLLING_DAYS}-day rolling', self.page('/?rolling=5'))
        self.assertIn(f'{trend.ROLLING_DAYS}-day rolling', self.page('/?rolling=nonsense'))

    def test_a_longer_rolling_average_still_covers_days_a_shorter_one_has_dropped(self) -> None:
        self.store_build(1, first=['fast/pre.html'], second=['fast/pre.html'], clean=[],
                         started_at=int(time.time()) - 5 * 86400)
        self.classify_everything({'fast/pre.html': UNRELIABLE}, days=6)
        self.assertGreater(self.rolling_segments(self.page('/?rolling=14')),
                           self.rolling_segments(self.page('/?rolling=3')))

    def _blamed_here_clean_elsewhere(self) -> None:
        """One blamed build on the layout queue, one clean build on another, so a filter that does
        not narrow and a filter that narrows to the wrong queue both read differently."""
        self.store_build(1, first=['fast/pre.html'], second=['fast/pre.html'], clean=[])
        self.store_build(2, first=['fast/real.html'], second=['fast/real.html'], clean=[],
                         builder=fixtures.GTK_BUILDER, builder_id=11)
        self.classify_everything({'fast/pre.html': UNRELIABLE, 'fast/real.html': RELIABLE})

    def test_a_queue_filter_narrows_the_cards_and_the_chart(self) -> None:
        self._blamed_here_clean_elsewhere()
        whole = self.page('/')
        self.assertIn('<span class="value">50%</span>', whole)
        self.assertIn('1 of 2 failing builds', whole)
        self.assertIn('1 of 2 builds blamed an author for noise', whole)

        narrowed = self.page(f'/?builder={fixtures.GTK_BUILDER}')
        self.assertIn('<span class="value">0%</span>', narrowed)
        self.assertIn('0 of 1 failing builds', narrowed)
        self.assertIn('0 of 1 builds blamed an author for noise', narrowed)

    def test_a_suite_filter_narrows_the_cards_and_the_chart(self) -> None:
        self.store_build(1, first=['fast/pre.html'], second=['fast/pre.html'], clean=[])
        self.store_build(2, first=['api/pre.html'], second=['api/pre.html'], clean=[],
                         builder=fixtures.API_BUILDER, builder_id=9)
        self.classify_everything({'fast/pre.html': UNRELIABLE, 'api/pre.html': UNRELIABLE})
        api = suites.suite_for_builder(fixtures.API_BUILDER).name
        narrowed = self.page(f'/?suite={api}')
        self.assertIn('1 of 1 failing builds', narrowed)
        self.assertIn('1 of 1 builds blamed an author for noise', narrowed)

    def test_an_unknown_queue_is_ignored_rather_than_emptying_the_page(self) -> None:
        self._blamed_here_clean_elsewhere()
        self.assertIn('1 of 2 failing builds', self.page('/?builder=not-a-queue'))

    def rail_link_to(self, page: str, builder: str) -> str:
        """The href of the queue rail's entry for one queue."""
        entry = re.search(rf'<a class="entry[^"]*" href="([^"]*{re.escape(builder)}[^"]*)"', page)
        self.assertIsNotNone(entry, f'the rail listed no entry for {builder}')
        return entry.group(1)

    def test_the_queue_rail_keeps_the_rest_of_the_scope_in_its_links(self) -> None:
        self._blamed_here_clean_elsewhere()
        link = self.rail_link_to(self.page('/?days=14&rolling=3'), fixtures.GTK_BUILDER)
        self.assertIn('days=14', link)
        self.assertIn('rolling=3', link)

    def test_the_chosen_queue_is_marked_in_the_rail_and_another_is_not(self) -> None:
        self._blamed_here_clean_elsewhere()
        page = self.page(f'/?builder={fixtures.GTK_BUILDER}')
        self.assertRegex(page, rf'class="entry selected" href="[^"]*{re.escape(fixtures.GTK_BUILDER)}')
        self.assertNotRegex(page, rf'class="entry selected" href="[^"]*{re.escape(fixtures.LAYOUT_BUILDER)}')


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
        self.assertNotIn(f'<span class="state state-{false_positive.CLEAN}">', page)

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


class TestExploreBuildFilter(WebTest):
    """The builds pane's own filter: a details and a GET form, since the app ships no JavaScript and
    a narrowed pane has to be a URL a reader can link and go back from."""

    def listed_builds(self, page: str) -> list:
        """The builds the pane listed, as the ids its entries link to."""
        return re.findall(r'<a class="entry[^"]*" href="[^"]*build=(\d+)', page)

    def entry_link(self, page: str, pattern: str) -> str:
        found = re.search(rf'<a class="entry[^"]*" href="([^"]*{re.escape(pattern)}[^"]*)"', page)
        self.assertIsNotNone(found, f'the page rendered no entry matching {pattern}')
        return found.group(1).replace('&amp;', '&')

    def three_states(self) -> dict:
        """One build in each of three states, and three sizes with them: all noise showing one test,
        a real failure showing one, and a partly-noise build showing three."""
        builds = {
            'noise': self.store_build(1, first=['fast/pre.html'], second=['fast/pre.html'],
                                      clean=[]),
            'real': self.store_build(2, first=['fast/real.html'], second=['fast/real.html'],
                                     clean=[]),
            'mixed': self.store_build(3, first=['fast/pre.html', 'fast/other.html',
                                                'fast/real.html'],
                                      second=['fast/pre.html', 'fast/other.html',
                                              'fast/real.html'],
                                      clean=[]),
        }
        self.classify_everything({'fast/pre.html': UNRELIABLE, 'fast/other.html': UNRELIABLE,
                                 'fast/real.html': RELIABLE})
        return builds

    def test_a_state_filter_lists_only_the_builds_in_that_state(self) -> None:
        builds = self.three_states()
        self.assertEqual(self.listed_builds(self.page(f'/explore?state={false_positive.FALSE_RED}')),
                         [str(builds['noise'])])

    def test_two_states_asked_for_together_list_the_builds_in_either_of_them(self) -> None:
        builds = self.three_states()
        page = self.page(f'/explore?state={false_positive.FALSE_RED}'
                         f'&state={false_positive.PARTIAL_FP}')
        self.assertEqual(sorted(self.listed_builds(page)),
                         sorted((str(builds['noise']), str(builds['mixed']))))

    def test_a_build_no_refresh_has_reached_is_filterable_as_unclassified(self) -> None:
        self.three_states()
        unreached = self.store_build(4, first=['fast/new.html'], second=['fast/new.html'], clean=[])
        self.assertEqual(self.listed_builds(self.page(f'/explore?state={formatting.UNCLASSIFIED}')),
                         [str(unreached)])

    def test_a_range_keeps_the_builds_sitting_exactly_on_either_bound(self) -> None:
        builds = self.three_states()
        small = sorted((str(builds['noise']), str(builds['real'])))
        self.assertEqual(sorted(self.listed_builds(self.page('/explore?min_shown=1&max_shown=1'))),
                         small)
        self.assertEqual(self.listed_builds(self.page('/explore?min_shown=3')),
                         [str(builds['mixed'])])
        self.assertEqual(sorted(self.listed_builds(self.page('/explore?min_shown=1&max_shown=3'))),
                         sorted(str(each) for each in builds.values()))

    def test_a_state_that_is_not_a_state_is_ignored_rather_than_emptying_the_pane(self) -> None:
        builds = self.three_states()
        self.assertEqual(sorted(self.listed_builds(self.page('/explore?state=NotAState'))),
                         sorted(str(each) for each in builds.values()))

    def test_a_bound_that_is_not_a_number_is_ignored_rather_than_raising(self) -> None:
        builds = self.three_states()
        self.assertEqual(sorted(self.listed_builds(self.page('/explore?min_shown=&max_shown=lots'))),
                         sorted(str(each) for each in builds.values()))

    def test_a_minimum_above_the_maximum_narrows_to_nothing_rather_than_failing(self) -> None:
        self.three_states()
        page = self.page('/explore?min_shown=3&max_shown=1')
        self.assertEqual(self.listed_builds(page), [])
        self.assertIn('No failing builds in this window match this filter.', page)

    def test_a_narrowed_pane_counts_against_what_matched_not_every_failing_build(self) -> None:
        self.three_states()
        self.assertIn('<span class="tally">1 shown of 1 matching</span>',
                      self.page(f'/explore?state={false_positive.FALSE_RED}'))

    def test_an_unnarrowed_pane_still_counts_against_every_failing_build(self) -> None:
        self.three_states()
        with mock.patch.object(web_app, 'BUILDS_SHOWN', 2):
            self.assertIn('<span class="tally">2 of 3</span>', self.page('/explore'))

    def test_a_narrowed_pane_says_how_many_matched_beyond_the_page_it_shows(self) -> None:
        self.three_states()
        with mock.patch.object(web_app, 'BUILDS_SHOWN', 2):
            page = self.page('/explore?min_shown=1')
        self.assertEqual(len(self.listed_builds(page)), 2)
        self.assertIn('<span class="tally">2 shown of 3 matching</span>', page)

    def test_a_matching_build_older_than_the_page_is_still_listed(self) -> None:
        """Narrowing the page instead of the window read as no build in 90 days having been all
        noise when the only build that was is older than the newest `BUILDS_SHOWN`."""
        now = int(time.time())
        noise = self.store_build(1, first=['fast/pre.html'], second=['fast/pre.html'], clean=[],
                                 started_at=now - 4 * 3600)
        for number in (2, 3):
            self.store_build(number, first=['fast/real.html'], second=['fast/real.html'], clean=[],
                             started_at=now - number * 600)
        self.classify_everything({'fast/pre.html': UNRELIABLE, 'fast/real.html': RELIABLE})
        with mock.patch.object(web_app, 'BUILDS_SHOWN', 2):
            page = self.page(f'/explore?state={false_positive.FALSE_RED}')
        self.assertEqual(self.listed_builds(page), [str(noise)])
        self.assertIn('<span class="tally">1 shown of 1 matching</span>', page)
        self.assertNotIn('No failing builds in this window match this filter.', page)

    def test_the_filter_travels_with_the_links_the_page_renders_and_holds_when_one_is_followed(self) -> None:
        builds = self.three_states()
        page = self.page(f'/explore?state={false_positive.FALSE_RED}&min_shown=1&max_shown=2')
        build_link = self.entry_link(page, f'build={builds["noise"]}')
        for link in (build_link, self.entry_link(page, f'builder={fixtures.LAYOUT_BUILDER}')):
            self.assertIn(f'state={false_positive.FALSE_RED}', link)
            self.assertIn('min_shown=1', link)
            self.assertIn('max_shown=2', link)
        self.assertEqual(self.listed_builds(self.page(build_link)), [str(builds['noise'])])

    def test_a_chosen_state_comes_back_checked_so_the_dropdown_reads_as_what_it_did(self) -> None:
        self.three_states()
        page = self.page(f'/explore?state={false_positive.FALSE_RED}')
        self.assertIn(f'<input type="checkbox" name="state" value="{false_positive.FALSE_RED}"'
                      ' checked>', page)
        self.assertIn(f'<input type="checkbox" name="state" value="{false_positive.CLEAN}">', page)


class TestEscapes(WebTest):
    """The escape page, which shows what main did with a convicted test after the change landed."""

    def _stored_escape(self, build_id: int, test_name: str, verdict: str,
                       failed_after: int = 3, runs_after: int = 3) -> None:
        with self.connection:
            self.connection.execute(
                '''INSERT INTO escape_verdicts (
                    build_id, test_name, verdict, runs_before, failed_before, runs_after,
                    failed_after, window_ends_at, decided_at
                ) VALUES (?,?,?,?,?,?,?,?,?)''',
                (build_id, test_name, verdict, 4, 0, runs_after, failed_after,
                 int(time.time()), int(time.time())),
            )

    def test_a_window_with_nothing_decided_says_so_rather_than_reading_as_no_escapes(self) -> None:
        self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE}, pr_id=1, pr_title='One')
        page = self.page('/escapes')
        self.assertIn('No escapes decided in this window', page)
        self.assertIn('1 not looked for', page)

    def test_an_escape_is_listed_with_the_runs_behind_it(self) -> None:
        build_id = self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE}, pr_id=72555,
                                    pr_title='One')
        self._stored_escape(build_id, 'fast/a.html', escapes.ESCAPED)
        page = self.page('/escapes')
        self.assertIn('fast/a.html', page)
        self.assertIn('3 of 3', page)
        self.assertIn('pull/72555', page)

    def test_the_rate_is_over_what_main_answered_and_not_over_every_conviction(self) -> None:
        """A conviction main ran nothing about belongs in no denominator: counting it would report
        an escape rate that falls as coverage falls."""
        first = self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE}, pr_id=1,
                                 pr_title='One')
        second = self.store_build(2, flaky={'fast/b.html': config.CLEAN_TREE}, pr_id=2,
                                  pr_title='Two')
        third = self.store_build(3, flaky={'fast/c.html': config.CLEAN_TREE}, pr_id=3,
                                 pr_title='Three')
        self._stored_escape(first, 'fast/a.html', escapes.ESCAPED)
        self._stored_escape(second, 'fast/b.html', escapes.CONTAINED, failed_after=0)
        self._stored_escape(third, 'fast/c.html', escapes.NO_RUNS, failed_after=0, runs_after=0)
        page = self.page('/escapes')
        self.assertIn('<span class="value">50%</span>', page)
        self.assertIn('1 of 2 convictions main answered', page)

    def test_serving_the_page_never_reaches_results_webkit_org(self) -> None:
        build_id = self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE}, pr_id=1,
                                    pr_title='One')
        self._stored_escape(build_id, 'fast/a.html', escapes.ESCAPED)
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=AssertionError('a page made an HTTP request')):
            self.assertEqual(self.client.get('/escapes').status_code, 200)

    def test_a_queue_that_convicted_nothing_escaping_shows_the_page_narrowed(self) -> None:
        build_id = self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE}, pr_id=1,
                                    pr_title='One')
        self._stored_escape(build_id, 'fast/a.html', escapes.ESCAPED)
        self.store_build(2, flaky={'fast/b.html': config.CLEAN_TREE}, builder=fixtures.GTK_BUILDER,
                         builder_id=9, pr_id=2, pr_title='Two')
        page = self.page(f'/escapes?builder={fixtures.GTK_BUILDER}')
        self.assertNotIn('fast/a.html', page)
        self.assertIn('No escapes decided in this window', page)

    def test_a_verdict_drilled_into_lists_its_convictions_and_why(self) -> None:
        build_id = self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE}, pr_id=72555,
                                    pr_title='One')
        self._stored_escape(build_id, 'fast/a.html', escapes.CONTAINED, failed_after=0,
                            runs_after=5)
        page = self.page(f'/escapes?verdict={escapes.CONTAINED}')
        self.assertIn('Main ran it 5 times after the landing and never failed it.', page)
        self.assertIn('pull/72555', page)

    def test_a_verdict_that_is_not_a_verdict_is_ignored_rather_than_refused(self) -> None:
        build_id = self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE}, pr_id=1,
                                    pr_title='One')
        self._stored_escape(build_id, 'fast/a.html', escapes.CONTAINED, failed_after=0)
        page = self.page('/escapes?verdict=NONSENSE')
        self.assertNotIn('never failed it', page)

    def test_the_verdict_being_drilled_into_survives_a_change_of_queue_or_window(self) -> None:
        """The drill-down is a query parameter, so a link that dropped it would close the card the
        moment a reader narrowed the page."""
        build_id = self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE}, pr_id=1,
                                    pr_title='One')
        self._stored_escape(build_id, 'fast/a.html', escapes.CONTAINED, failed_after=0)
        page = self.page(f'/escapes?verdict={escapes.CONTAINED}')
        self.assertRegex(page, rf'class="badge active" href="[^"]*verdict={escapes.CONTAINED}')
        self.assertRegex(page, rf'class="entry[^"]*" href="[^"]*verdict={escapes.CONTAINED}')

    def test_the_escaped_count_links_to_the_card_that_already_lists_them(self) -> None:
        build_id = self.store_build(1, flaky={'fast/a.html': config.CLEAN_TREE}, pr_id=1,
                                    pr_title='One')
        self._stored_escape(build_id, 'fast/a.html', escapes.ESCAPED)
        page = self.page('/escapes')
        self.assertIn('<a href="#ESCAPED">', page)
        self.assertIn('<div class="section" id="ESCAPED">', page)
        self.assertNotIn(f'verdict={escapes.ESCAPED}', page)


class TestMethodologyDisclosure(WebTest):
    """The methodology prose went unread sitting open at the bottom of a page, so it is collapsed
    behind a details, which the browser opens without the JavaScript this app does not ship."""

    DISCLOSURE = '<details class="caveats-disclosure">'

    def assert_collapsed(self, path: str, prose: str) -> None:
        page = self.page(path)
        self.assertIn(self.DISCLOSURE, page)
        self.assertNotIn('caveats-disclosure" open', page)
        self.assertIn(prose, page)

    def test_the_landing_methodology_is_present_and_collapsed(self) -> None:
        self.assert_collapsed('/', 'every rate here is a floor on blame noise')

    def test_the_escape_methodology_is_present_and_collapsed(self) -> None:
        self.assert_collapsed('/escapes', 'Every other number on this dashboard is a floor')


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
