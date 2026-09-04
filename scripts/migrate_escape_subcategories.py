#!/usr/bin/env python3
"""Fold ESCAPED_RARELY back into ESCAPED and give the table its currency columns.

    python3 -m scripts.migrate_escape_subcategories
    python3 -m scripts.migrate_escape_subcategories --database other.db

ESCAPED_RARELY was a second stored verdict for a conviction that excused something main had not been
failing on few failures rather than many. It is now one stored verdict with the rate read off
`runs_after` and `failed_after` wherever an escape is shown, so a rarity beside the counts can never
disagree with them. Every row stored under the retired name is the same answer and moves to ESCAPED.

The same rebuild adds `recent_runs`, `recent_failed` and `recent_checked_at`, which hold whether main
is still failing an escaped test. They arrive null, which is what says nobody has asked yet: the
assess pass fills them in for the escapes alone.

The table is rebuilt rather than altered because db.initialize() creates it with `CREATE TABLE IF NOT
EXISTS`, so an edit to schema.sql never reaches a database that already has one — neither the
narrowed CHECK constraint, which now rejects ESCAPED_RARELY, nor the three new columns.

Run once. A second run finds nothing to do and says so.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from ews_dashboard import config, db
from ews_dashboard.analysis import escapes
from scripts.migrate_verdict_names import (COPY_BATCH_ROWS, TABLE, live_table_sql,
                                           permitted_verdicts, schema_table_sql, verdict_counts)

TEMPORARY_TABLE = f'{TABLE}_subcategorised'

RETIRED_VERDICT = 'ESCAPED_RARELY'


def mapped_verdict(verdict: str) -> str:
    """What a stored verdict name should be called now."""
    return escapes.ESCAPED if verdict == RETIRED_VERDICT else verdict


def retired_rows(connection: sqlite3.Connection) -> int:
    return verdict_counts(connection).get(RETIRED_VERDICT, 0)


def _columns(table_sql: str) -> frozenset:
    """The column names a `CREATE TABLE` statement declares, as sqlite reads them.

    Created in a scratch database and read back through PRAGMA rather than parsed here: the verdict
    names inside the CHECK constraint sit on their own lines and any line-wise reading of the
    statement counts them as columns. The foreign key's target is not resolved at CREATE time, so the
    scratch database needs nothing else in it.
    """
    scratch = sqlite3.connect(':memory:')
    try:
        scratch.execute(table_sql)
        return frozenset(row[1] for row in scratch.execute('PRAGMA table_info(probe)'))
    finally:
        scratch.close()


def missing_columns(connection: sqlite3.Connection) -> frozenset:
    """Columns schema.sql declares that the live table does not have."""
    live = frozenset(row['name'] for row in connection.execute(f'PRAGMA table_info({TABLE})'))
    return _columns(schema_table_sql(name='probe')) - live


def extra_columns(connection: sqlite3.Connection) -> frozenset:
    """Columns the live table has that schema.sql no longer declares.

    `_copy_rows` only names the live table's own columns, so one of these left in place would abort
    the INSERT into the rebuilt table mid-transaction rather than being dropped along with the rest.
    """
    live = frozenset(row['name'] for row in connection.execute(f'PRAGMA table_info({TABLE})'))
    return live - _columns(schema_table_sql(name='probe'))


def needs_migration(connection: sqlite3.Connection) -> bool:
    """Whether any of the three halves of the problem is still present.

    The constraint, the rows and the columns fail separately: a database with no ESCAPED_RARELY row
    left can still permit the name, and one that permits exactly the right names can still be missing
    the currency columns, which is what makes a plain UPDATE impossible either way.
    """
    if permitted_verdicts(live_table_sql(connection)) != permitted_verdicts(schema_table_sql()):
        return True
    if missing_columns(connection) or extra_columns(connection):
        return True
    return retired_rows(connection) > 0


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
    """Copy the live table's own columns across, renaming the retired verdict on the way.

    Only the columns the live table has are named, so the ones this migration adds take their
    declared default of null: a row copied from before the currency check existed has not been
    checked, and that is exactly what null says.
    """
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
    """Rebuild the table under the current schema with the retired verdict folded into ESCAPED.

    Foreign keys go off for the duration, as sqlite's own table-rebuild recipe requires: the rows
    reference `build_verdicts`, and dropping the old table with them on would either refuse or
    cascade. `foreign_key_check` inside the transaction proves nothing was orphaned before anything
    is committed.
    """
    columns = [row['name'] for row in connection.execute(f'PRAGMA table_info({TABLE})')]
    extra = extra_columns(connection)
    if extra:
        raise RuntimeError(f'{TABLE} has {sorted(extra)}, which schema.sql no longer declares; '
                           'nothing was changed')
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
        unknown = '' if verdict in escapes.VERDICTS else ' (no bucket)'
        print(f'    {verdict:<{width}}  {count:>6}{unknown}')
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
            print(f'{path} is already migrated; no verdict to fold and no column to add')
            _print_counts('verdicts stored', verdict_counts(connection))
            return 0
        print(f'Rebuilding {TABLE} in {path}')
        _print_counts('before', verdict_counts(connection))
        retired, added = retired_rows(connection), sorted(missing_columns(connection))
        copied = migrate(connection)
        print(f'  copied {copied} rows, {retired} of them from {RETIRED_VERDICT}')
        print(f'  columns added: {", ".join(added) or "none"}')
        _print_counts('after', verdict_counts(connection))
    finally:
        connection.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
