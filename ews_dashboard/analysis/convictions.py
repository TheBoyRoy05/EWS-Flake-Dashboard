"""What the flakiness classifier convicted, by rule and by test.

Every count is over the answer that stood for each test — the rerun's where there is one, the first
run's otherwise, which is what latest_flakiness_verdicts selects. Convictions are counted per
(build, test), so one test convicted in twelve builds counts twelve times: what matters is how much
noise the rule absorbed, not how many distinct tests are unreliable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from ews_dashboard import config, results

WINDOW = 'build.started_at >= :since AND build.started_at < :until'


@dataclass(frozen=True)
class ConvictedTest:
    test_name: str
    rule: str
    convictions: int
    queues: int
    last_seen: int
    builder: str
    builder_id: int
    build_number: int
    pr_id: Optional[int]
    configuration: results.Configuration


@dataclass(frozen=True)
class Convictions:
    """One rule's convicted tests, and how many the query would have returned unlimited, so a page
    can say what it cut rather than presenting a truncated list as the whole of it."""

    tests: list
    total: int

    @property
    def truncated(self) -> int:
        return max(0, self.total - len(self.tests))


@dataclass(frozen=True)
class QueueActivity:
    builder: str
    builds_queried: int
    convictions: int
    query_failures: int


def _filters(suite: Optional[str], builder: Optional[str] = None) -> tuple:
    """Extra WHERE clauses for a query that has build_verdicts aliased as `build`, and their
    parameters. Returned together so a caller cannot bind one without the other."""
    conditions, parameters = '', {}
    if suite is not None:
        conditions += ' AND build.suite = :suite'
        parameters['suite'] = suite
    if builder is not None:
        conditions += ' AND build.builder = :builder'
        parameters['builder'] = builder
    return conditions, parameters


def _window_parameters(since: int, until: int, suite: Optional[str],
                       builder: Optional[str]) -> tuple:
    conditions, parameters = _filters(suite, builder)
    parameters.update({'since': since, 'until': until})
    return conditions, parameters


def by_rule(connection: sqlite3.Connection, since: int, until: int, suite: Optional[str] = None,
            builder: Optional[str] = None) -> dict:
    """Convictions per rule, including rules that never fired, so a zero shows up as a zero."""
    conditions, parameters = _window_parameters(since, until, suite, builder)
    counted = {
        row['rule']: row['convictions']
        for row in connection.execute(
            f'''SELECT verdict.rule, COUNT(*) AS convictions
                FROM latest_flakiness_verdicts AS verdict
                JOIN build_verdicts AS build USING (build_id)
                WHERE verdict.rule IS NOT NULL AND {WINDOW}{conditions}
                GROUP BY verdict.rule''',
            parameters,
        )
    }
    return {rule: counted.get(rule, 0) for rule in config.FLAKINESS_RULES}


def builds_queried(connection: sqlite3.Connection, since: int, until: int,
                   suite: Optional[str] = None, builder: Optional[str] = None) -> int:
    conditions, parameters = _window_parameters(since, until, suite, builder)
    return connection.execute(
        f'''SELECT COUNT(*) FROM build_verdicts AS build
            WHERE build.flakiness_query_ran = 1 AND {WINDOW}{conditions}''',
        parameters,
    ).fetchone()[0]


def query_failures(connection: sqlite3.Connection, since: int, until: int,
                   suite: Optional[str] = None, builder: Optional[str] = None) -> int:
    """Tests the classifier asked about and got no answer for — the read path's own error rate."""
    conditions, parameters = _window_parameters(since, until, suite, builder)
    return connection.execute(
        f'''SELECT COUNT(*)
            FROM latest_flakiness_verdicts AS verdict
            JOIN build_verdicts AS build USING (build_id)
            WHERE verdict.query_failed = 1 AND {WINDOW}{conditions}''',
        parameters,
    ).fetchone()[0]


def convicted_tests(
    connection: sqlite3.Connection,
    rule: str,
    since: int,
    until: int,
    suite: Optional[str] = None,
    builder: Optional[str] = None,
    limit: int = 100,
) -> 'Convictions':
    """Tests convicted under one rule, most-convicted first, each with a build to link to.

    The builder, build number and configuration columns are bare in an aggregate query alongside
    MAX(started_at), which sqlite documents as taking their values from the row that produced the
    maximum — so they describe the most recent conviction rather than an arbitrary one.
    """
    conditions, parameters = _filters(suite, builder)
    parameters.update({'rule': rule, 'since': since, 'until': until, 'limit': limit})
    total = connection.execute(
        f'''SELECT COUNT(DISTINCT verdict.test_name)
            FROM latest_flakiness_verdicts AS verdict
            JOIN build_verdicts AS build USING (build_id)
            WHERE verdict.rule = :rule AND {WINDOW}{conditions}''',
        parameters,
    ).fetchone()[0]
    rows = connection.execute(
        f'''SELECT verdict.test_name,
                   COUNT(*) AS convictions,
                   COUNT(DISTINCT build.builder) AS queues,
                   MAX(build.started_at) AS last_seen,
                   build.builder, build.builder_id, build.build_number, build.pr_id,
                   build.suite, build.platform, build.style, build.flavor
            FROM latest_flakiness_verdicts AS verdict
            JOIN build_verdicts AS build USING (build_id)
            WHERE verdict.rule = :rule AND {WINDOW}{conditions}
            GROUP BY verdict.test_name
            ORDER BY convictions DESC, last_seen DESC
            LIMIT :limit''',
        parameters,
    ).fetchall()
    return Convictions(
        tests=[
            ConvictedTest(
                test_name=row['test_name'],
                rule=rule,
                convictions=row['convictions'],
                queues=row['queues'],
                last_seen=row['last_seen'],
                builder=row['builder'],
                builder_id=row['builder_id'],
                build_number=row['build_number'],
                pr_id=row['pr_id'],
                configuration=results.Configuration.of_build(row),
            )
            for row in rows
        ],
        total=total,
    )


def _counts_by_builder(connection: sqlite3.Connection, sql: str, parameters: dict) -> dict:
    return {row['builder']: row['total'] for row in connection.execute(sql, parameters)}


def queue_activity(connection: sqlite3.Connection, since: int, until: int,
                   suite: Optional[str] = None) -> list:
    """Per queue: how often it asked, how often it convicted, how often the query failed.

    Only RunWebKitTests and ReRunWebKitTests set the properties behind builds_queried (steps.py).
    They ask only when a run had failures, the pull request targets main, and the limit held.
    So a zero can mean the queue's builds passed, not that it skipped the read path.
    An api-tests queue is always zero: its steps set no flakiness property at all.
    """
    conditions, parameters = _filters(suite)
    parameters.update({'since': since, 'until': until})
    queried = _counts_by_builder(
        connection,
        f'''SELECT build.builder, SUM(build.flakiness_query_ran) AS total
            FROM build_verdicts AS build WHERE {WINDOW}{conditions} GROUP BY build.builder''',
        parameters,
    )
    convicted = _counts_by_builder(
        connection,
        f'''SELECT build.builder, COUNT(*) AS total
            FROM latest_flakiness_verdicts AS verdict
            JOIN build_verdicts AS build USING (build_id)
            WHERE verdict.rule IS NOT NULL AND {WINDOW}{conditions}
            GROUP BY build.builder''',
        parameters,
    )
    failed = _counts_by_builder(
        connection,
        f'''SELECT build.builder, COUNT(*) AS total
            FROM latest_flakiness_verdicts AS verdict
            JOIN build_verdicts AS build USING (build_id)
            WHERE verdict.query_failed = 1 AND {WINDOW}{conditions}
            GROUP BY build.builder''',
        parameters,
    )
    activity = [
        QueueActivity(
            builder=builder,
            builds_queried=queried.get(builder) or 0,
            convictions=convicted.get(builder) or 0,
            query_failures=failed.get(builder) or 0,
        )
        for builder in sorted(set(queried) | set(convicted) | set(failed))
    ]
    return sorted(activity, key=lambda queue: (-queue.builds_queried, queue.builder))
