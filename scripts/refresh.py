#!/usr/bin/env python3
"""Bring the database up to date: ingest builds, then classify what they surfaced.

This is the only thing in the repository that talks to the network, so it is also the only thing
that can be slow. Classification asks results.webkit.org about one test in one configuration at one
commit, which takes about 1.6 seconds, so every lookup a window needs is collected first and warmed
in parallel; the classify pass afterwards reads the cache and does no I/O of its own.

Run it from cron or by hand. The web app never runs it, and a page served while it is halfway
through shows the builds it has already finished plus a count of the ones it has not.
"""

from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
import time
from typing import Optional

from ews_dashboard import buildbot, db, ingest, results
from ews_dashboard.analysis import false_positive, trend

DEFAULT_DAYS = 14


def _window(days: int) -> tuple:
    until = trend.day_bounds(trend.today())[1]
    return until - days * 86400, until


def _begin_run(connection: sqlite3.Connection, started_at: int) -> None:
    with connection:
        connection.execute('INSERT OR REPLACE INTO refresh_runs (started_at) VALUES (?)',
                           (started_at,))


def _finish_run(connection: sqlite3.Connection, started_at: int, report: ingest.IngestReport,
                builders_walked: int) -> None:
    with connection:
        connection.execute(
            '''UPDATE refresh_runs
               SET finished_at = ?, builders_walked = ?, builds_ingested = ?, builds_failed = ?
               WHERE started_at = ?''',
            (int(time.time()), builders_walked,
             report.outcomes['ingested'] + report.outcomes['reingested'], report.failed, started_at),
        )


def _fail_run(connection: sqlite3.Connection, started_at: int, error: BaseException) -> None:
    """Record why a run died, so the pages can say the numbers stopped moving on purpose.

    `finished_at` stays null: a failed run did not finish, and every freshness answer treats it as
    the stale run it is.
    """
    with connection:
        connection.execute('UPDATE refresh_runs SET error = ? WHERE started_at = ?',
                           (f'{type(error).__name__}: {error}', started_at))


def ingest_builds(connection: sqlite3.Connection, client: buildbot.BuildbotClient, since: int,
                  builder: Optional[str], force: bool) -> tuple:
    names = [builder] if builder else ingest.dashboard_builder_names(client)
    report = ingest.IngestReport()
    for name in names:
        print(f'  {name}', flush=True)
        report.add(ingest.ingest_builder(connection, client, name, since=since, force=force))
    return report, len(names)


def classify_builds(connection: sqlite3.Connection, history: results.History,
                    since: int, until: int) -> false_positive.Counts:
    builds = false_positive.failing_builds(connection, since, until)
    history.prefetch(false_positive.pending_queries(connection, builds))
    return false_positive.rate(
        connection, false_positive.live_classifier(connection, history), since, until,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--days', type=int, default=DEFAULT_DAYS,
                        help=f'how far back to walk builds (default {DEFAULT_DAYS})')
    parser.add_argument('--builder', help='one builder rather than every builder EWS exposes')
    parser.add_argument('--force', action='store_true',
                        help='re-read builds already stored, discarding their classifications')
    parser.add_argument('--skip-ingest', action='store_true',
                        help='classify what is already stored without touching Buildbot')
    parsed = parser.parse_args()

    since, until = _window(parsed.days)
    db.initialize()
    connection = db.connect()
    started_at = int(time.time())
    _begin_run(connection, started_at)
    try:
        report, builders_walked = _refresh(connection, parsed, since, until)
    except Exception as error:
        _fail_run(connection, started_at, error)
        connection.close()
        print(f'refresh failed: {type(error).__name__}: {error}', file=sys.stderr)
        raise
    _finish_run(connection, started_at, report, builders_walked)
    connection.close()
    return 0


def _refresh(connection: sqlite3.Connection, parsed: argparse.Namespace,
             since: int, until: int) -> tuple:
    report, builders_walked = ingest.IngestReport(), 0
    if not parsed.skip_ingest:
        walked_from = datetime.datetime.fromtimestamp(since, datetime.timezone.utc).date()
        print(f'Ingesting builds since {walked_from.isoformat()}')
        report, builders_walked = ingest_builds(
            connection, buildbot.BuildbotClient(), since, parsed.builder, parsed.force,
        )
        for error in report.errors[:10]:
            print(f'  {error}', file=sys.stderr)
        print(f'  {dict(report.outcomes)}, {report.failed} failed')

    print('Classifying author-visible failures')
    history = results.History(connection)
    counts = classify_builds(connection, history, since, until)
    print(f'  {counts.classifiable} classifiable builds, '
          f'{counts.author_fp_rate_pct}% blamed an author for noise')
    print(f'  results.webkit.org: {dict(history.stats)}')
    return report, builders_walked


if __name__ == '__main__':
    sys.exit(main())
