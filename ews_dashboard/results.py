"""Cache-aware reader for results.webkit.org's test history.

The endpoint behind this is /api/results-summary/<suite>/<test>, which returns nine outcome
percentages summing to 100 over roughly the last 99 runs ending at `ref`. It is a sliding window,
not history-up-to-a-commit: asking for an older ref moves the window, it does not truncate it. So
this module answers "how reliable is this test around here", and cannot answer "did this test
start failing at commit X" — that needs the per-run endpoint.

Every response is cached, including the absence of one. A configuration with no recorded history
answers 404, which is a real answer; re-asking it on every run is what made the prototype's
analysis pass take over an hour.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent import futures
from dataclasses import dataclass, replace
from typing import Iterable, Optional

from ews_dashboard import config

OUTCOMES = ('pass', 'fail', 'timeout', 'crash', 'image', 'audio', 'text', 'error', 'warning')

HTTP_TIMEOUT_SECONDS = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.5

CURRENT_TTL_SECONDS = 24 * 3600
NO_HISTORY_TTL_SECONDS = 7 * 24 * 3600

# The endpoint answers in about 1.6 seconds, so a few thousand lookups cost over an hour in series.
PREFETCH_WORKERS = 16

TRANSIENT_ERRORS = (
    urllib.error.URLError,
    http.client.IncompleteRead,
    ConnectionError,
    TimeoutError,
    json.JSONDecodeError,
)


class HistoryUnavailable(Exception):
    """results.webkit.org could not be reached or answered unusably. Distinct from a 404, which
    means the service is fine and has nothing recorded for that configuration."""


@dataclass(frozen=True)
class Configuration:
    suite: str
    platform: str
    style: str
    flavor: str = ''

    @classmethod
    def of_build(cls, build_row: sqlite3.Row) -> 'Configuration':
        return cls(
            suite=build_row['suite'],
            platform=build_row['platform'] or '',
            style=build_row['style'] or '',
            flavor=build_row['flavor'] or '',
        )

    def query_parameters(self) -> dict:
        parameters = {'platform': self.platform, 'style': self.style}
        if self.flavor:
            parameters['flavor'] = self.flavor
        return parameters


@dataclass(frozen=True)
class Query:
    test_name: str
    configuration: Configuration
    # '' asks about the tip of the tree. Anything else is a WebKit identifier or SHA.
    commit_ref: str = ''


@dataclass(frozen=True)
class Answer:
    """A cache hit. `outcomes` is None when the hit records that upstream has no history."""

    outcomes: Optional[dict]

    @property
    def pass_rate(self) -> Optional[float]:
        """How often this test passes, or None when nothing is recorded for the configuration.

        A warning counts as a pass, because EWS's own pre-existing check compares
        `data.get('pass', 100) + data.get('warning', 0)` against the same threshold
        (results_db.py). Counting passes alone would call a warning-heavy test pre-existing where
        EWS blamed the author, which is the opposite of what this dashboard is measuring.
        """
        if self.outcomes is None:
            return None
        return (self.outcomes.get('pass') or 0.0) + (self.outcomes.get('warning') or 0.0)


def cached_answer(connection: sqlite3.Connection, query: Query) -> Optional[Answer]:
    """One test's history from the cache alone, or None when no refresh has stored one.

    Reaches no network, so a page can explain a classification without becoming a slow request.
    A miss on the commit falls back to the tip-of-tree row because `History._resolved` drops an
    unregistered commit before caching, and deciding whether a commit is registered is itself a
    request this must not make.
    """
    candidates = (query, replace(query, commit_ref='')) if query.commit_ref else (query,)
    for candidate in candidates:
        row = _cache_row(connection, candidate)
        if row is not None and not _expired(row, candidate):
            return _answer_of(row)
    return None


def _answer_of(row: sqlite3.Row) -> Answer:
    if not row['has_history']:
        return Answer(None)
    return Answer({outcome: row[f'{outcome}_pct'] for outcome in OUTCOMES})


def _cache_row(connection: sqlite3.Connection, query: Query) -> Optional[sqlite3.Row]:
    configuration = query.configuration
    return connection.execute(
        '''SELECT has_history, pass_pct, fail_pct, timeout_pct, crash_pct, image_pct,
                  audio_pct, text_pct, error_pct, warning_pct, fetched_at
           FROM results_summary_cache
           WHERE test_name = ? AND suite = ? AND platform = ? AND style = ? AND flavor = ?
             AND commit_ref = ?''',
        (query.test_name, configuration.suite, configuration.platform,
         configuration.style, configuration.flavor, query.commit_ref),
    ).fetchone()


def _expired(row: sqlite3.Row, query: Query) -> bool:
    age = int(time.time()) - row['fetched_at']
    if not row['has_history']:
        return age > NO_HISTORY_TTL_SECONDS
    if not query.commit_ref:
        return age > CURRENT_TTL_SECONDS
    return False


def _commit_refs_in(queries: 'list[Query]') -> 'list[str]':
    return sorted({query.commit_ref for query in queries if query.commit_ref})


class History:
    def __init__(self, connection: sqlite3.Connection,
                 base_url: str = config.RESULTS_URL) -> None:
        self.connection = connection
        self.base_url = base_url.rstrip('/')
        self.stats: Counter = Counter()
        self._registered_commits: dict = {}
        # Reentrant because the memo check records a statistic while already holding it.
        self._lock = threading.RLock()

    def pass_rate(self, query: Query) -> Optional[float]:
        return Answer(self.outcomes(query)).pass_rate

    def outcomes(self, query: Query) -> Optional[dict]:
        resolved = self._resolved(query)
        cached = self._read_cache(resolved)
        if cached is not None:
            self._record('cache_hit')
            return cached.outcomes
        self._record('cache_miss')
        outcomes = self._fetch(resolved)
        self._write_cache(resolved, outcomes)
        return outcomes

    def _resolved(self, query: Query) -> Query:
        """The query that will actually be sent.

        EWS drops the commit and asks about the tip of the tree when results.webkit.org does not
        know it (results_db.py `is_test_expected_to`), so this reproduces that. Resolving here
        rather than inside the fetch is what keeps a tip-of-tree answer out of a cache row keyed by
        a commit: such a row would look pinned, and `_expired` never expires a pinned row.
        """
        if not query.commit_ref or self.is_registered(query.commit_ref):
            return query
        self._record('commit_not_registered')
        return replace(query, commit_ref='')

    def prefetch(self, queries: Iterable[Query], workers: int = PREFETCH_WORKERS) -> None:
        """Warm the cache for many queries at once, so a later serial pass never waits on HTTP.

        Fetches run in a thread pool. Every sqlite call stays on the calling thread, which is what
        keeps the connection single-threaded. A query whose fetch fails is left uncached, so the
        serial pass retries it and reports its own outcome.

        Each answer is written as it arrives rather than after the pool drains, so an interrupted
        refresh keeps the thousands of lookups it already paid for.
        """
        requested = list(queries)
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            # Registration first: resolving a query consults it, and the memo only helps once
            # filled. A day of builds shares an order of magnitude fewer base commits than tests.
            list(pool.map(self.is_registered, _commit_refs_in(requested)))
            pending = self._uncached(self._resolved(query) for query in requested)
            answers = pool.map(self._fetch_without_raising, pending)
            for query, (fetched, outcomes) in zip(pending, answers):
                if fetched:
                    self._write_cache(query, outcomes)

    def is_registered(self, commit_ref: str) -> bool:
        """Whether results.webkit.org knows this commit.

        EWS makes the same pre-check before asking for history, so an unregistered commit means a
        tip-of-tree lookup rather than a pointless 404.
        """
        with self._lock:
            if commit_ref in self._registered_commits:
                self._record('commit_memo_hit')
                return self._registered_commits[commit_ref]
        query = urllib.parse.urlencode({'ref': commit_ref})
        answer = self._get(f'/api/commits?{query}')
        registered = bool(answer) and not (isinstance(answer, dict) and answer.get('status') == 'error')
        with self._lock:
            self._registered_commits[commit_ref] = registered
        return registered

    def _uncached(self, queries: Iterable[Query]) -> list:
        pending = []
        seen = set()
        for query in queries:
            if query in seen:
                continue
            seen.add(query)
            if self._read_cache(query) is None:
                pending.append(query)
        return pending

    def _record(self, name: str) -> None:
        with self._lock:
            self.stats[name] += 1

    def _fetch(self, query: Query) -> Optional[dict]:
        parameters = query.configuration.query_parameters()
        if query.commit_ref:
            parameters['ref'] = query.commit_ref
        path = (
            f'/api/results-summary/{query.configuration.suite}/'
            f"{urllib.parse.quote(query.test_name, safe='/')}"
            f'?{urllib.parse.urlencode(parameters)}'
        )
        answer = self._get(path)
        if not isinstance(answer, dict):
            return None
        return {outcome: answer.get(outcome) for outcome in OUTCOMES}

    def _fetch_without_raising(self, query: Query) -> tuple:
        """(whether the answer is usable, the answer). Runs on a pool thread: no sqlite here."""
        try:
            return True, self._fetch(query)
        except HistoryUnavailable:
            self._record('unavailable')
            return False, None

    def _get(self, path: str) -> Optional[object]:
        """Parsed JSON, or None when upstream says it has nothing for this request.

        404 and 400 are both "nothing recorded here": a test that has never run in a configuration
        answers 404, and a configuration the service does not recognize answers 400.
        """
        url = f'{self.base_url}{path}'
        last_error: Optional[Exception] = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                request = urllib.request.Request(url, headers={'Accept': 'application/json'})
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:
                if error.code in (400, 404):
                    self._record('no_history')
                    return None
                last_error = error
            except TRANSIENT_ERRORS as error:
                last_error = error
            if attempt + 1 < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise HistoryUnavailable(str(last_error))

    def _read_cache(self, query: Query) -> Optional[Answer]:
        """This exact query's cached answer. Unlike `cached_answer` it does not fall back to the tip
        of the tree, because `_resolved` has already decided which of the two this query is."""
        row = _cache_row(self.connection, query)
        if row is None or _expired(row, query):
            return None
        return _answer_of(row)

    def _write_cache(self, query: Query, outcomes: Optional[dict]) -> None:
        configuration = query.configuration
        with self.connection:
            self.connection.execute(
                '''INSERT OR REPLACE INTO results_summary_cache (
                    test_name, suite, platform, style, flavor, commit_ref, has_history,
                    pass_pct, fail_pct, timeout_pct, crash_pct,
                    image_pct, audio_pct, text_pct, error_pct, warning_pct, fetched_at
                ) VALUES (?,?,?,?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?)''',
                (
                    query.test_name, configuration.suite, configuration.platform,
                    configuration.style, configuration.flavor, query.commit_ref,
                    int(outcomes is not None),
                    *[(outcomes or {}).get(outcome) for outcome in OUTCOMES],
                    int(time.time()),
                ),
            )
