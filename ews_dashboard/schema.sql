-- SQLite schema for the EWS flakiness dashboard.
--
-- Two rules shape it. Buildbot's own arrays are stored as JSON-encoded TEXT verbatim, so a
-- reader can always see what the bot reported. Anything the web layer filters, groups or
-- counts by is exploded into its own row at ingest time, so no page parses JSON to answer a
-- question.

PRAGMA foreign_keys = ON;
-- WAL, not the default rollback journal, because a scheduled refresh writes while the web app is
-- serving: a rollback-journal commit needs an exclusive lock, so every page request would wait out
-- whichever transaction the refresh is in. The mode persists in the file once set here.
PRAGMA journal_mode = WAL;


CREATE TABLE IF NOT EXISTS build_verdicts (
    build_id                    INTEGER PRIMARY KEY,
    builder                     TEXT    NOT NULL,
    builder_id                  INTEGER NOT NULL,
    build_number                INTEGER NOT NULL,

    pr_id                       INTEGER,
    sha                         TEXT,
    change_id                   TEXT,
    -- WebKit commit identifier of the main commit the pull request was rebased onto, e.g.
    -- '314546@main'. This is the "before the patch" point every history lookup is asked about.
    identifier                  TEXT,

    platform                    TEXT,
    style                       TEXT,
    flavor                      TEXT,
    suite                       TEXT    NOT NULL,

    verdict                     TEXT    NOT NULL CHECK (verdict IN (
                                    'SUCCESS', 'WARNINGS', 'FAILURE', 'SKIPPED',
                                    'EXCEPTION', 'RETRY', 'CANCELLED', 'UNKNOWN'
                                )),

    first_run_failures          TEXT,
    second_run_failures         TEXT,
    clean_tree_run_failures     TEXT,

    -- The bot's own crash-storm signal: first_results_exceed_failure_limit or its second-run
    -- counterpart, set by steps.py when a run bails out at EXIT_AFTER_FAILURES.
    exceeded_failure_limit      INTEGER NOT NULL DEFAULT 0,

    -- Whether this build consulted results.webkit.org for flakiness at all. A build that asked
    -- and convicted nothing leaves no flakiness_verdicts rows, so without this flag it is
    -- indistinguishable from a build that never asked.
    flakiness_query_ran         INTEGER NOT NULL DEFAULT 0,

    started_at                  INTEGER,
    complete_at                 INTEGER
);

CREATE INDEX IF NOT EXISTS index_verdicts_pull_request ON build_verdicts(pr_id);
CREATE INDEX IF NOT EXISTS index_verdicts_verdict_time ON build_verdicts(verdict, started_at);
CREATE INDEX IF NOT EXISTS index_verdicts_suite_time   ON build_verdicts(suite, started_at);
CREATE INDEX IF NOT EXISTS index_verdicts_builder_time ON build_verdicts(builder, started_at);
CREATE INDEX IF NOT EXISTS index_verdicts_queried      ON build_verdicts(flakiness_query_ran, started_at);


-- One row per test the EWS flakiness classifier was asked about and answered for, exploded from
-- the results-db_{first,second}_run_flaky* build properties.
--
-- `rule` is the READ-side verdict RunWebKitTests reached — CleanTree, DirtyTree or BetweenBuilds
-- — and is not the write-side flaky_type stored in Cassandra. The two vocabularies differ.
CREATE TABLE IF NOT EXISTS flakiness_verdicts (
    build_id                 INTEGER NOT NULL REFERENCES build_verdicts(build_id) ON DELETE CASCADE,
    -- 1 for the first test run, 2 for the rerun with the change still applied. The rerun
    -- re-queries the same tests, so its answer is the one the author saw; latest_flakiness_verdicts
    -- selects it.
    run_number               INTEGER NOT NULL CHECK (run_number IN (1, 2)),
    test_name                TEXT    NOT NULL,

    rule                     TEXT    CHECK (rule IN ('CleanTree', 'DirtyTree', 'BetweenBuilds')),
    query_failed             INTEGER NOT NULL DEFAULT 0,
    -- 0 only for a BetweenBuilds conviction the bot itself flagged as having no within-build
    -- evidence; NULL for every other rule, which has nothing to say about it.
    within_build_evidence    INTEGER,

    PRIMARY KEY (build_id, run_number, test_name)
);

CREATE INDEX IF NOT EXISTS index_flakiness_rule ON flakiness_verdicts(rule);
CREATE INDEX IF NOT EXISTS index_flakiness_test ON flakiness_verdicts(test_name);


-- The answer that stood for each test: the highest run number that said anything about it. Scoped
-- per test rather than per build, because the rerun only asks about the tests still failing, so a
-- build-wide MAX would drop a first-run conviction the rerun never revisited.
CREATE VIEW IF NOT EXISTS latest_flakiness_verdicts AS
SELECT verdict.*
FROM flakiness_verdicts AS verdict
WHERE verdict.run_number = (
    SELECT MAX(later.run_number)
    FROM flakiness_verdicts AS later
    WHERE later.build_id = verdict.build_id AND later.test_name = verdict.test_name
);


CREATE TABLE IF NOT EXISTS builds_ingested (
    builder_id    INTEGER NOT NULL,
    build_number  INTEGER NOT NULL,
    fetched_at    INTEGER NOT NULL,

    PRIMARY KEY (builder_id, build_number)
);


-- Cached responses from results.webkit.org's results-summary endpoint, which returns nine
-- outcome percentages summing to 100.
--
-- Invalidation, by row kind:
--   commit_ref = ''  and has_history = 1  tip-of-tree lookup; expires (CURRENT_TTL_SECONDS)
--   commit_ref = ref and has_history = 1  a window ending at a fixed commit; never expires
--   has_history = 0                       upstream has no history for this configuration, which
--                                         is an answer worth keeping, but history can appear
--                                         later, so it expires (NO_HISTORY_TTL_SECONDS)
CREATE TABLE IF NOT EXISTS results_summary_cache (
    test_name     TEXT NOT NULL,
    suite         TEXT NOT NULL,
    platform      TEXT NOT NULL,
    style         TEXT NOT NULL,
    flavor        TEXT NOT NULL DEFAULT '',
    commit_ref    TEXT NOT NULL DEFAULT '',

    has_history   INTEGER NOT NULL DEFAULT 1,

    pass_pct      INTEGER,
    fail_pct      INTEGER,
    timeout_pct   INTEGER,
    crash_pct     INTEGER,
    image_pct     INTEGER,
    audio_pct     INTEGER,
    text_pct      INTEGER,
    error_pct     INTEGER,
    warning_pct   INTEGER,

    fetched_at    INTEGER NOT NULL,

    PRIMARY KEY (test_name, suite, platform, style, flavor, commit_ref)
);


-- Cached per-build false-positive classification, keyed by the threshold it was computed under.
--
-- Safe to share across every overlapping window the trend asks for, because a build's
-- classification depends only on its own failure lists and the history at classification time.
--
-- Invalidation: re-ingesting a build deletes its rows, and a row that had to give up on any test
-- expires after UNDETERMINED_TTL_SECONDS, since the history it lacked may exist by now. A row
-- that classified every test keeps its answer.
CREATE TABLE IF NOT EXISTS build_classifications (
    build_id                     INTEGER NOT NULL REFERENCES build_verdicts(build_id) ON DELETE CASCADE,
    threshold_pct                INTEGER NOT NULL,

    -- NULL means the build surfaced no tests to the author at all.
    bucket                       TEXT CHECK (bucket IN (
                                     'CLEAN', 'PARTIAL_FP', 'FALSE_RED', 'UNDETERMINED'
                                 )),
    surfaced_total               INTEGER NOT NULL,
    surfaced_pre_existing        INTEGER NOT NULL,
    surfaced_real                INTEGER NOT NULL,
    surfaced_undetermined        INTEGER NOT NULL,

    classified_at                INTEGER NOT NULL,

    PRIMARY KEY (build_id, threshold_pct)
);


-- One row per refresh, so every page can say how old its numbers are. `error` is set when a run
-- died, so a run with neither a finish nor an error is one still going.
CREATE TABLE IF NOT EXISTS refresh_runs (
    started_at       INTEGER PRIMARY KEY,
    finished_at      INTEGER,
    builders_walked  INTEGER NOT NULL DEFAULT 0,
    builds_ingested  INTEGER NOT NULL DEFAULT 0,
    builds_failed    INTEGER NOT NULL DEFAULT 0,
    error            TEXT
);


CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_index  INTEGER PRIMARY KEY,
    applied_at       INTEGER NOT NULL
);
