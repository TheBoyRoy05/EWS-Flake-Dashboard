#!/usr/bin/env python3
"""Fold the two escape verdict names 3761a9a retired into the one that replaced them.

    python3 -m scripts.migrate_verdict_names
    python3 -m scripts.migrate_verdict_names --database other.db

FLAKY_ON_MAIN and ALREADY_FAILING were merged into FAILS_ON_MAIN. A database created before that
change still holds the rows decided under the old names, and still has the CHECK constraint that
permits them and rejects the name that replaced them: schema.sql was updated, but the live table
survived on `CREATE TABLE IF NOT EXISTS` and was never rebuilt. The constraint therefore has to be
replaced before the names can be rewritten, which is why this copies the table rather than running
an UPDATE, which the old constraint would reject.

The runs either side of the landing are copied across untouched. They are the evidence that main
failed the test without the change, and the reason these rows are migrated rather than dropped.

Run once. A second run finds nothing to do and says so.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from typing import Optional

from ews_dashboard import config, db
from ews_dashboard.analysis import escapes

TABLE = 'escape_verdicts'
TEMPORARY_TABLE = f'{TABLE}_migrated'

COPY_BATCH_ROWS = 500

# The names 3761a9a retired, and what each became. Both meant main failing the test without the
# change in it, which is the one thing FAILS_ON_MAIN says.
RETIRED_VERDICTS = {
    'FLAKY_ON_MAIN': escapes.FAILS_ON_MAIN,
    'ALREADY_FAILING': escapes.FAILS_ON_MAIN,
}


def mapped_verdict(verdict: str) -> str:
    """What a stored verdict name should be called now."""
    return RETIRED_VERDICTS.get(verdict, verdict)


def permitted_verdicts(table_sql: str) -> frozenset:
    """The verdict names a `CREATE TABLE` statement's CHECK constraint allows.

    Read out of the statement rather than assumed, because the whole reason for this migration is a
    live table whose constraint and schema.sql's had silently drifted apart.
    """
    match = re.search(r'verdict\s+IN\s*\((.*?)\)', table_sql, re.DOTALL | re.IGNORECASE)
    if not match:
        return frozenset()
    return frozenset(re.findall(r"'([^']+)'", match.group(1)))


def schema_table_sql(table: str = TABLE, name: Optional[str] = None) -> str:
    """The `CREATE TABLE` statement schema.sql declares, optionally under a different name.

    Taken from the file so the rebuilt table carries whatever the current schema permits; retyping
    the constraint list here would be the same mistake that let the live table fall behind.
    """
    with open(db.SCHEMA_PATH) as schema_file:
        schema = schema_file.read()
    match = re.search(
        rf'^CREATE TABLE (?:IF NOT EXISTS )?{table}\s*\((.*?)^\);',
        schema, re.DOTALL | re.MULTILINE,
    )
    if not match:
        raise LookupError(f'{db.SCHEMA_PATH} declares no table {table}')
    return f'CREATE TABLE {name or table} ({match.group(1)})'


def live_table_sql(connection: sqlite3.Connection, table: str = TABLE) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
    ).fetchone()
    if row is None:
        raise LookupError(f'no table {table} in this database')
    return row['sql']


def verdict_counts(connection: sqlite3.Connection) -> dict:
    return {
        row['verdict']: row['convictions']
        for row in connection.execute(
            f'SELECT verdict, COUNT(*) AS convictions FROM {TABLE} GROUP BY verdict '
            'ORDER BY verdict',
        )
    }


def needs_migration(connection: sqlite3.Connection) -> bool:
    """Whether either half of the problem is still present.

    The rows and the constraint are separate failures: a database can have no row left under a
    retired name and still reject the name that replaced it, which is the state that made a plain
    UPDATE impossible.
    """
    if permitted_verdicts(live_table_sql(connection)) != permitted_verdicts(schema_table_sql()):
        return True
    return any(verdict in RETIRED_VERDICTS for verdict in verdict_counts(connection))


def _companion_objects(connection: sqlite3.Connection) -> list:
    """The indexes and triggers on the table, which dropping it takes with it.

    `sql IS NULL` marks the index sqlite derives from the PRIMARY KEY; that one comes back with the
    CREATE TABLE and must not be replayed.
    """
    return [row['sql'] for row in connection.execute(
        '''SELECT sql FROM sqlite_master
           WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL''',
        (TABLE,),
    )]


def _copy_rows(connection: sqlite3.Connection, columns: list) -> int:
    quoted = ', '.join(f'"{column}"' for column in columns)
    placeholders = ', '.join('?' * len(columns))
    verdict_at = columns.index('verdict')
    cursor = connection.execute(f'SELECT {quoted} FROM {TABLE}')
    copied = 0
    while True:
        batch = cursor.fetchmany(COPY_BATCH_ROWS)
        if not batch:
            return copied
        rewritten = []
        for row in batch:
            values = list(row)
            values[verdict_at] = mapped_verdict(values[verdict_at])
            rewritten.append(values)
        connection.executemany(
            f'INSERT INTO {TEMPORARY_TABLE} ({quoted}) VALUES ({placeholders})', rewritten,
        )
        copied += len(rewritten)


def migrate(connection: sqlite3.Connection) -> int:
    """Rebuild the table under the current schema with the retired names rewritten.

    Foreign keys go off for the duration, as sqlite's own table-rebuild recipe requires: the rows
    reference `build_verdicts`, and dropping the old table with them on would either refuse or
    cascade. `foreign_key_check` inside the transaction proves nothing was orphaned before anything
    is committed.
    """
    columns = [row['name'] for row in connection.execute(f'PRAGMA table_info({TABLE})')]
    companions = _companion_objects(connection)

    connection.execute('PRAGMA foreign_keys = OFF')
    connection.execute('BEGIN IMMEDIATE')
    try:
        connection.execute(f'DROP TABLE IF EXISTS {TEMPORARY_TABLE}')
        connection.execute(schema_table_sql(name=TEMPORARY_TABLE))
        copied = _copy_rows(connection, columns)
        connection.execute(f'DROP TABLE {TABLE}')
        connection.execute(f'ALTER TABLE {TEMPORARY_TABLE} RENAME TO {TABLE}')
        for statement in companions:
            connection.execute(statement)
        orphaned = connection.execute('PRAGMA foreign_key_check').fetchall()
        if orphaned:
            raise RuntimeError(f'{len(orphaned)} rows would be left orphaned; nothing was changed')
    except BaseException:
        connection.execute('ROLLBACK')
        connection.execute('PRAGMA foreign_keys = ON')
        raise
    connection.execute('COMMIT')
    connection.execute('PRAGMA foreign_keys = ON')
    return copied


def _print_counts(label: str, counts: dict) -> None:
    print(f'  {label}')
    if not counts:
        print('    no rows')
        return
    width = max(len(verdict) for verdict in counts)
    for verdict, count in counts.items():
        retired = ' (retired)' if verdict in RETIRED_VERDICTS else ''
        print(f'    {verdict:<{width}}  {count:>6}{retired}')
    print(f'    {"total":<{width}}  {sum(counts.values()):>6}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--database', default=None)
    arguments = parser.parse_args()

    path = arguments.database or config.database_path()
    if not os.path.exists(path):
        print(f'no database at {path}', file=sys.stderr)
        return 1

    connection = db.connect(path)
    try:
        if not needs_migration(connection):
            print(f'{path} is already migrated; no verdict names to rewrite')
            _print_counts('verdicts stored', verdict_counts(connection))
            return 0
        print(f'Migrating {TABLE} in {path}')
        _print_counts('before', verdict_counts(connection))
        copied = migrate(connection)
        print(f'  copied {copied} rows under the current constraint')
        _print_counts('after', verdict_counts(connection))
    finally:
        connection.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
