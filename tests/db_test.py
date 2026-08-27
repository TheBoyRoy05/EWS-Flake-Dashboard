"""Schema creation and the migration ledger.

schema.sql already carries every migration's effect, so the ledger exists to keep a fresh install
from replaying an ALTER TABLE against a column it already has.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from ews_dashboard import db
from tests import fixtures

REFRESH_RUNS_WITH_ERROR = ('    builds_failed    INTEGER NOT NULL DEFAULT 0,\n'
                           '    error            TEXT\n')
REFRESH_RUNS_WITHOUT_ERROR = '    builds_failed    INTEGER NOT NULL DEFAULT 0\n'

A_MIGRATION_THAT_CANNOT_BE_REPLAYED = 'ALTER TABLE build_verdicts ADD COLUMN builder TEXT'


def _schema_before_the_error_column() -> str:
    """schema.sql as it stood before MIGRATIONS[0], which is what an un-upgraded database holds."""
    with open(db.SCHEMA_PATH) as schema_file:
        schema = schema_file.read()
    if REFRESH_RUNS_WITH_ERROR not in schema:
        raise AssertionError('schema.sql no longer declares refresh_runs.error the way this undoes it')
    return schema.replace(REFRESH_RUNS_WITH_ERROR, REFRESH_RUNS_WITHOUT_ERROR)


class TestInitialize(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix='ews-dashboard-db-test-')
        self.database_path = os.path.join(self.directory, 'test.db')

    def tearDown(self) -> None:
        shutil.rmtree(self.directory)

    def _stamped_migrations(self) -> list:
        connection = db.connect(self.database_path)
        try:
            return [row['migration_index'] for row in connection.execute(
                'SELECT migration_index FROM schema_migrations ORDER BY migration_index')]
        finally:
            connection.close()

    def _columns(self, table: str) -> list:
        connection = db.connect(self.database_path)
        try:
            return [row['name'] for row in connection.execute(f'PRAGMA table_info({table})')]
        finally:
            connection.close()

    def _create_older_database(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.executescript(_schema_before_the_error_column())
        finally:
            connection.close()

    def test_a_fresh_database_records_every_migration_without_replaying_it(self) -> None:
        db.initialize(self.database_path)
        self.assertEqual(self._stamped_migrations(), list(range(len(db.MIGRATIONS))))
        self.assertIn('error', self._columns('refresh_runs'))

    def test_a_migration_appended_after_schema_sql_is_stamped_not_run_on_a_fresh_install(self) -> None:
        with mock.patch.object(db, 'MIGRATIONS',
                               db.MIGRATIONS + (A_MIGRATION_THAT_CANNOT_BE_REPLAYED,)):
            db.initialize(self.database_path)
            self.assertEqual(self._stamped_migrations(), [0, 1])

    def test_an_older_database_with_an_empty_ledger_has_the_pending_migrations_applied(self) -> None:
        self._create_older_database()
        self.assertNotIn('error', self._columns('refresh_runs'))
        self.assertEqual(self._stamped_migrations(), [])
        db.initialize(self.database_path)
        self.assertIn('error', self._columns('refresh_runs'))
        self.assertEqual(self._stamped_migrations(), list(range(len(db.MIGRATIONS))))

    def test_an_older_database_keeps_the_rows_it_already_had(self) -> None:
        self._create_older_database()
        connection = sqlite3.connect(self.database_path)
        try:
            with connection:
                connection.execute(
                    'INSERT INTO refresh_runs (started_at, builds_ingested) VALUES (?, ?)',
                    (fixtures.DEFAULT_BUILD_TIME, 12),
                )
        finally:
            connection.close()
        db.initialize(self.database_path)
        connection = db.connect(self.database_path)
        try:
            row = connection.execute('SELECT builds_ingested, error FROM refresh_runs').fetchone()
            self.assertEqual(row['builds_ingested'], 12)
            self.assertIsNone(row['error'])
        finally:
            connection.close()

    def test_initializing_twice_leaves_the_ledger_unchanged(self) -> None:
        db.initialize(self.database_path)
        first = self._stamped_migrations()
        db.initialize(self.database_path)
        self.assertEqual(self._stamped_migrations(), first)
        self.assertEqual(len(first), len(db.MIGRATIONS))

    def test_initializing_an_upgraded_database_again_applies_nothing(self) -> None:
        self._create_older_database()
        db.initialize(self.database_path)
        db.initialize(self.database_path)
        self.assertEqual(self._stamped_migrations(), list(range(len(db.MIGRATIONS))))
        self.assertEqual(self._columns('refresh_runs').count('error'), 1)


class TestConnect(fixtures.DatabaseTest):
    def test_a_page_reading_the_database_does_not_block_a_refresh_writing_it(self) -> None:
        """Under the default rollback journal a writer's commit waits out every open read, so an
        hourly refresh and a live web app would take turns. The timeout is shortened so a database
        that lost WAL fails here rather than stalling for thirty seconds."""
        for number in (1, 2):
            self.store_build(number)
        with mock.patch.object(db, 'BUSY_TIMEOUT_MILLISECONDS', 200):
            reader = db.connect(self.database_path)
            writer = db.connect(self.database_path)
        try:
            rows = reader.execute('SELECT build_id FROM build_verdicts')
            # One row of several, which is a page mid-render: the read lock is held until the last.
            rows.fetchone()
            with writer:
                writer.execute('INSERT INTO refresh_runs (started_at) VALUES (?)',
                               (fixtures.DEFAULT_BUILD_TIME,))
        finally:
            reader.close()
            writer.close()


class TestRowCounts(fixtures.DatabaseTest):
    def test_row_counts_reports_a_zero_for_every_table_it_names(self) -> None:
        self.assertEqual(db.row_counts(self.connection),
                         {table: 0 for table in db.REPORTED_TABLES})

    def test_row_counts_names_every_table_the_schema_creates_except_the_ledger(self) -> None:
        created = {row['name'] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(created - {'schema_migrations'}, set(db.REPORTED_TABLES))

    def test_row_counts_follows_an_ingested_build(self) -> None:
        self.store_build(number=140, first=['fast/events/drag.html'], second=[])
        counts = db.row_counts(self.connection)
        self.assertEqual(counts['build_verdicts'], 1)
        self.assertEqual(counts['builds_ingested'], 1)
