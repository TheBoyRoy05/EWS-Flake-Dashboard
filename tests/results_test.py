"""History caches every answer, including the absence of one.

The prototype re-asked results.webkit.org about every 404 on every run, which is what made its
analysis pass take over an hour, so the negative cache is the behaviour these tests are here for.
"""

from __future__ import annotations

import io
import json
import time
import unittest
import urllib.error
from typing import Optional
from unittest import mock

from ews_dashboard import results
from tests import fixtures

CONFIGURATION = results.Configuration(suite='layout-tests', platform='mac', style='release',
                                      flavor='wk2')
# A distinct value per outcome, so a column that lands in the wrong one is visible rather than
# hidden behind a row of zeroes.
SUMMARY = {'pass': 90.0, 'fail': 4.0, 'timeout': 1.0, 'crash': 0.5, 'image': 1.5,
           'audio': 0.25, 'text': 0.75, 'error': 0.5, 'warning': 1.5}
PASS_RATE = SUMMARY['pass'] + SUMMARY['warning']


def _responder(payload: object) -> object:
    """A fresh response per call, since urlopen's body can only be read once."""
    def urlopen(request: object, timeout: Optional[int] = None) -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
        response.__exit__.return_value = False
        return response
    return urlopen


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError('https://results.webkit.org', code, 'no', {}, None)


class TestHistory(fixtures.DatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.history = results.History(self.connection)

    def _query(self, test_name: str = 'fast/a.html', commit_ref: str = '') -> results.Query:
        return results.Query(test_name, CONFIGURATION, commit_ref)

    def test_a_summary_is_fetched_once_and_then_read_from_the_cache(self) -> None:
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=_responder(SUMMARY)) as urlopen:
            self.assertEqual(self.history.pass_rate(self._query()), PASS_RATE)
            self.assertEqual(self.history.pass_rate(self._query()), PASS_RATE)
            self.assertEqual(urlopen.call_count, 1)
            self.assertEqual(self.history.stats['cache_hit'], 1)

    def test_every_outcome_survives_the_round_trip_through_the_cache(self) -> None:
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=_responder(SUMMARY)) as urlopen:
            self.assertEqual(self.history.outcomes(self._query()), SUMMARY)
            self.assertEqual(self.history.outcomes(self._query()), SUMMARY)
            self.assertEqual(urlopen.call_count, 1)

    def test_a_warning_counts_as_a_pass_the_way_ews_counts_it(self) -> None:
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=_responder(SUMMARY)):
            self.assertEqual(self.history.pass_rate(self._query()),
                             SUMMARY['pass'] + SUMMARY['warning'])

    def test_a_configuration_with_no_history_is_cached_as_having_none(self) -> None:
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=_http_error(404)) as urlopen:
            self.assertIsNone(self.history.pass_rate(self._query()))
            self.assertIsNone(self.history.pass_rate(self._query()))
            self.assertEqual(urlopen.call_count, 1)
            self.assertEqual(self.history.stats['no_history'], 1)
            self.assertEqual(self.history.stats['cache_hit'], 1)

    def test_a_rejected_configuration_is_cached_the_same_way_as_a_missing_test(self) -> None:
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=_http_error(400)) as urlopen:
            self.assertIsNone(self.history.pass_rate(self._query()))
            self.assertIsNone(self.history.pass_rate(self._query()))
            self.assertEqual(urlopen.call_count, 1)

    def test_an_unreachable_service_is_not_cached_and_does_not_look_like_a_missing_test(self) -> None:
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=urllib.error.URLError('offline')), \
                mock.patch('ews_dashboard.results.time.sleep'):
            with self.assertRaises(results.HistoryUnavailable):
                self.history.pass_rate(self._query())
            self.assertEqual(self.connection.execute(
                'SELECT COUNT(*) FROM results_summary_cache').fetchone()[0], 0)

    def test_a_transient_failure_is_retried(self) -> None:
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=[urllib.error.URLError('reset'), _responder(SUMMARY)(None)]), \
                mock.patch('ews_dashboard.results.time.sleep'):
            self.assertEqual(self.history.pass_rate(self._query()), PASS_RATE)

    def test_a_stale_tip_of_tree_answer_is_re_fetched(self) -> None:
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=_responder(SUMMARY)) as urlopen:
            self.history.pass_rate(self._query())
            self._age_cache(results.CURRENT_TTL_SECONDS + 60)
            self.history.pass_rate(self._query())
            self.assertEqual(urlopen.call_count, 2)

    def test_an_answer_pinned_to_a_commit_never_expires(self) -> None:
        pinned = self._query(commit_ref=fixtures.IDENTIFIER)
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=_responder(SUMMARY)) as urlopen:
            self.history.pass_rate(pinned)
            self._age_cache(10 * results.NO_HISTORY_TTL_SECONDS)
            self.history.pass_rate(pinned)
            # Two calls, not four: the summary is cached and the registration answer is memoized in
            # memory, where _age_cache cannot reach it.
            self.assertEqual(urlopen.call_count, 2)

    def test_prefetch_checks_each_distinct_commit_once_before_asking_about_tests(self) -> None:
        first, second = f'{fixtures.IDENTIFIER}', '314547@main'
        queries = [self._query('fast/a.html', commit_ref=first),
                   self._query('fast/b.html', commit_ref=first),
                   self._query('fast/a.html', commit_ref=second)]
        commit_calls = []

        def answer(request: object, timeout: Optional[int] = None) -> object:
            if '/api/commits' in request.full_url:
                commit_calls.append(request.full_url)
                return _responder([{'identifier': first}])(request)
            return _responder(SUMMARY)(request)

        with mock.patch('ews_dashboard.results.urllib.request.urlopen', side_effect=answer):
            self.history.prefetch(queries, workers=2)
        self.assertEqual(len(commit_calls), 2)
        self.assertEqual(self.history.stats['commit_memo_hit'], 3)

    def test_no_history_is_re_asked_eventually_because_a_test_may_start_running(self) -> None:
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=[_http_error(404), _responder(SUMMARY)(None)]):
            self.assertIsNone(self.history.pass_rate(self._query()))
            self._age_cache(results.NO_HISTORY_TTL_SECONDS + 60)
            self.assertEqual(self.history.pass_rate(self._query()), PASS_RATE)

    def test_an_unregistered_commit_falls_back_to_the_tip_of_the_tree(self) -> None:
        def answer(request: object, timeout: Optional[int] = None) -> object:
            if '/api/commits' in request.full_url:
                return _responder([])(request)
            self.assertNotIn('ref=', request.full_url)
            return _responder(SUMMARY)(request)

        with mock.patch('ews_dashboard.results.urllib.request.urlopen', side_effect=answer):
            self.assertEqual(self.history.pass_rate(self._query(commit_ref='999999@main')), PASS_RATE)
            self.assertEqual(self.history.stats['commit_not_registered'], 1)

    def test_a_tip_of_tree_fallback_is_cached_as_one_and_so_still_expires(self) -> None:
        """A row cached under the commit would look pinned, and a pinned row never expires."""
        def answer(request: object, timeout: Optional[int] = None) -> object:
            if '/api/commits' in request.full_url:
                return _responder([])(request)
            return _responder(SUMMARY)(request)

        unregistered = self._query(commit_ref='999999@main')
        with mock.patch('ews_dashboard.results.urllib.request.urlopen', side_effect=answer):
            self.history.pass_rate(unregistered)
            self.assertEqual([row['commit_ref'] for row in self.connection.execute(
                'SELECT commit_ref FROM results_summary_cache')], [''])
            self._age_cache(results.CURRENT_TTL_SECONDS + 60)
            summary_calls = []

            def counted(request: object, timeout: Optional[int] = None) -> object:
                if '/api/results-summary' in request.full_url:
                    summary_calls.append(request.full_url)
                return answer(request, timeout)

            with mock.patch('ews_dashboard.results.urllib.request.urlopen', side_effect=counted):
                self.history.pass_rate(unregistered)
            self.assertEqual(len(summary_calls), 1)

    def test_prefetch_asks_once_per_distinct_query(self) -> None:
        queries = [self._query('fast/a.html'), self._query('fast/b.html'), self._query('fast/a.html')]
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=_responder(SUMMARY)) as urlopen:
            self.history.prefetch(queries, workers=2)
            self.assertEqual(urlopen.call_count, 2)
            self.assertEqual(self.history.pass_rate(self._query('fast/b.html')), PASS_RATE)
            self.assertEqual(urlopen.call_count, 2)

    def test_prefetch_keeps_the_answers_it_already_read_when_a_later_fetch_dies(self) -> None:
        """Writing only once the pool had drained cost a killed refresh its whole phase."""
        def answer(request: object, timeout: Optional[int] = None) -> object:
            if 'fast/b.html' in request.full_url:
                raise RuntimeError('killed')
            return _responder(SUMMARY)(request)

        with mock.patch('ews_dashboard.results.urllib.request.urlopen', side_effect=answer):
            with self.assertRaises(RuntimeError):
                self.history.prefetch([self._query('fast/a.html'), self._query('fast/b.html')],
                                      workers=1)
        self.assertEqual([row['test_name'] for row in self.connection.execute(
            'SELECT test_name FROM results_summary_cache')], ['fast/a.html'])

    def test_prefetch_leaves_an_unreachable_query_uncached_for_the_serial_pass(self) -> None:
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=urllib.error.URLError('offline')), \
                mock.patch('ews_dashboard.results.time.sleep'):
            self.history.prefetch([self._query()], workers=1)
            self.assertEqual(self.connection.execute(
                'SELECT COUNT(*) FROM results_summary_cache').fetchone()[0], 0)
            self.assertEqual(self.history.stats['unavailable'], 1)

    def _age_cache(self, seconds: int) -> None:
        with self.connection:
            self.connection.execute('UPDATE results_summary_cache SET fetched_at = ?',
                                    (int(time.time()) - seconds,))


class TestCachedAnswer(fixtures.DatabaseTest):
    """What a page gets: the cache alone, and never a request."""

    def _query(self, test_name: str = 'fast/a.html', commit_ref: str = '') -> results.Query:
        return results.Query(test_name, CONFIGURATION, commit_ref)

    def _forbidden_urlopen(self, request: object, timeout: Optional[int] = None) -> object:
        raise AssertionError(f'cached_answer reached the network for {request}')

    def test_neither_a_hit_nor_a_miss_reaches_the_network(self) -> None:
        self.cache_answer(self._query('fast/hit.html'), SUMMARY)
        with mock.patch('ews_dashboard.results.urllib.request.urlopen',
                        side_effect=self._forbidden_urlopen):
            self.assertEqual(results.cached_answer(
                self.connection, self._query('fast/hit.html')).pass_rate, PASS_RATE)
            self.assertIsNone(results.cached_answer(self.connection, self._query('fast/miss.html')))

    def test_a_row_cached_for_the_builds_own_commit_answers_a_query_on_that_commit(self) -> None:
        self.cache_answer(self._query(commit_ref=fixtures.IDENTIFIER), SUMMARY)
        self.cache_answer(self._query(), {**SUMMARY, 'pass': 30.0})
        answer = results.cached_answer(self.connection, self._query(commit_ref=fixtures.IDENTIFIER))
        self.assertEqual(answer.pass_rate, PASS_RATE)

    def test_a_commit_with_no_row_of_its_own_falls_back_to_the_tip_of_the_tree(self) -> None:
        """`History._resolved` drops an unregistered commit before caching, and deciding whether a
        commit is registered is itself a request this must not make."""
        self.cache_answer(self._query(), SUMMARY)
        answer = results.cached_answer(self.connection, self._query(commit_ref='999999@main'))
        self.assertEqual(answer.pass_rate, PASS_RATE)

    def test_a_tip_of_tree_query_consults_no_other_row(self) -> None:
        self.cache_answer(self._query(commit_ref=fixtures.IDENTIFIER), SUMMARY)
        with mock.patch('ews_dashboard.results._cache_row',
                        wraps=results._cache_row) as cache_row:
            self.assertIsNone(results.cached_answer(self.connection, self._query()))
            self.assertEqual(cache_row.call_count, 1)

    def test_an_expired_tip_of_tree_row_reads_as_no_answer_rather_than_as_no_history(self) -> None:
        self.cache_answer(self._query(), SUMMARY, age_seconds=results.CURRENT_TTL_SECONDS + 60)
        self.assertIsNone(results.cached_answer(self.connection, self._query()))

    def test_a_row_recording_that_upstream_has_no_history_answers_with_no_pass_rate(self) -> None:
        self.cache_answer(self._query(), None)
        answer = results.cached_answer(self.connection, self._query())
        self.assertEqual(answer, results.Answer(None))
        self.assertIsNone(answer.pass_rate)


class TestAnswer(unittest.TestCase):
    def test_a_warning_counts_towards_the_pass_rate_because_ews_own_check_counts_it(self) -> None:
        self.assertEqual(results.Answer(SUMMARY).pass_rate,
                         SUMMARY['pass'] + SUMMARY['warning'])
