"""Connection and schema management for the dashboard's sqlite store.

    python3 -m ews_dashboard.db             create the database
    python3 -m ews_dashboard.db --status    row counts per table
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Optional

from ews_dashboard import config

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'schema.sql')

BUSY_TIMEOUT_MILLISECONDS = 30000

# Append-only, and never edited in place: a database records how many of these it has run, so
# reordering or rewriting one silently skips it. schema.sql keeps the shape a fresh install gets,
# so every change belongs in both places.
MIGRATIONS: tuple[str, ...] = (
    'ALTER TABLE refresh_runs ADD COLUMN error TEXT',
    'ALTER TABLE build_verdicts ADD COLUMN pr_title TEXT',
    'ALTER TABLE escape_verdicts ADD COLUMN recent_runs INTEGER',
    'ALTER TABLE escape_verdicts ADD COLUMN recent_failed INTEGER',
    'ALTER TABLE escape_verdicts ADD COLUMN recent_checked_at INTEGER',
    'ALTER TABLE escape_verdicts ADD COLUMN landed_at INTEGER',
)

REPORTED_TABLES = (
    'build_verdicts',
    'flakiness_verdicts',
    'builds_ingested',
    'builder_coverage',
    'landings',
    'results_summary_cache',
    'test_runs_cache',
    'build_classifications',
    'escape_verdicts',
    'refresh_runs',
)


def register_functions(connection: sqlite3.Connection) -> None:
    """Register the derived figures a query may order or narrow by, as the Python that computes them.

    This is what lets `ORDER BY escape_strength(...)` and the figure printed in the cell beside it be
    one definition rather than two: the alternative is a stored column or the Wilson formula
    re-spelled in SQL, either of which can drift from `strength_for_counts` and disagree with the
    counts shown next to it.

    `analysis.escapes` is imported here rather than at module scope. Checked, not assumed: it imports
    `config`, `queues`, `results` and `webkit_checkout`, none of which import this module, so a
    module-level import would not cycle today. It is deferred anyway for two reasons — this module is
    the bottom of the stack and everything else imports it, so depending on `analysis` inverts that
    and makes the first analysis module that wants `db.connect` a cycle; and `escapes` pulls in
    `results`, so `python3 -m ews_dashboard.db` would load the HTTP layer to create a table.

    `deterministic=True` lets sqlite treat a call as constant for one row, which is what an ORDER BY
    over a function of two stored columns wants; both functions are pure.
    """
    from ews_dashboard.analysis import escapes
    for name, arity, function in escapes.SQL_FUNCTIONS:
        connection.create_function(name, arity, function, deterministic=True)


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    connection = sqlite3.connect(path or config.database_path(), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute(f'PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}')
    register_functions(connection)
    return connection


def _apply_migrations(connection: sqlite3.Connection) -> int:
    applied = connection.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0]
    for index, statement in enumerate(MIGRATIONS[applied:], start=applied):
        try:
            with connection:
                connection.execute(statement)
                _record_migration(connection, index)
        except sqlite3.OperationalError as error:
            # A standalone scripts/migrate_*.py may have already added this column on this
            # database; that leaves MIGRATIONS's own ADD COLUMN redundant, not wrong, so record
            # it as applied rather than aborting the rest of the list.
            if 'duplicate column name' not in str(error):
                raise
            with connection:
                _record_migration(connection, index)
    return len(MIGRATIONS) - applied


def _record_migration(connection: sqlite3.Connection, index: int) -> None:
    connection.execute(
        'INSERT INTO schema_migrations (migration_index, applied_at) VALUES (?, ?)',
        (index, int(time.time())),
    )


def _is_fresh(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'build_verdicts'",
    ).fetchone()[0] == 0


def initialize(path: Optional[str] = None) -> None:
    """Create the database, or bring an existing one forward.

    schema.sql is the final shape, migrations included, so a database it just created has already
    had every migration applied and must be stamped as such. Replaying them would fail on the first
    ALTER TABLE, which adds a column schema.sql already declared.
    """
    with open(SCHEMA_PATH) as schema_file:
        schema = schema_file.read()
    connection = connect(path)
    try:
        fresh = _is_fresh(connection)
        connection.executescript(schema)
        if not fresh:
            _apply_migrations(connection)
            return
        with connection:
            for index in range(len(MIGRATIONS)):
                _record_migration(connection, index)
    finally:
        connection.close()


def row_counts(connection: sqlite3.Connection) -> dict:
    return {
        table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        for table in REPORTED_TABLES
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--database', default=None)
    parser.add_argument('--status', action='store_true', help='report row counts and exit')
    arguments = parser.parse_args()

    path = arguments.database or config.database_path()
    if arguments.status and not os.path.exists(path):
        print(f'no database at {path}', file=sys.stderr)
        return 1
    if not arguments.status:
        initialize(path)
        print(f'initialized {path}')

    connection = connect(path)
    try:
        counts = row_counts(connection)
    finally:
        connection.close()
    width = max(len(table) for table in counts)
    for table, count in counts.items():
        print(f'  {table:<{width}}  {count:>9}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
