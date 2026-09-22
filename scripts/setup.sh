#!/usr/bin/env bash
#
# One-command setup for a FRESH EWS Flake Dashboard database.
#
# What this does, in order:
#   1. installs Python dependencies (requirements.txt)
#   2. creates the sqlite database (python3 -m ews_dashboard.db)
#   3. runs the test suite with the correct discovery pattern
#   4. ingests builds and classifies false positives (network, minutes)
#
# What this deliberately does NOT do:
#   - It never runs escape detection. That needs a WebKit checkout and takes
#     roughly 20 minutes of serial results.webkit.org queries, which must run
#     detached with a log file, never inline. If you pass --checkout PATH this
#     script PRINTS the exact command to run afterwards; it does not run it.
#   - It never touches an existing database. If the target db already exists it
#     refuses unless you pass --force, so it cannot clobber a live database.
#   - It never migrates a live database. Migrations follow the backup-then-
#     migrate recipe in the handoff and are out of scope here.
#
# Usage:
#   scripts/setup.sh [--checkout PATH] [--days N] [--force] [--skip-refresh]
#
#   --checkout PATH   A WebKit checkout. Only used to print the escape-detection
#                     command at the end; escapes are never run by this script.
#   --days N          Ingest window in days (default: refresh.py's own default).
#   --force           Proceed even if the target database already exists.
#   --skip-refresh    Do steps 1-3 only; skip the network ingest.
#
# The database path follows EWS_DASHBOARD_DATABASE if set, else the repo default
# ews-dashboard.db (see ews_dashboard/config.py).

set -euo pipefail

CHECKOUT=""
DAYS=""
FORCE=0
SKIP_REFRESH=0

while [ $# -gt 0 ]; do
  case "$1" in
    --checkout)
      CHECKOUT="${2:-}"
      if [ -z "$CHECKOUT" ]; then
        echo "error: --checkout needs a path" >&2
        exit 2
      fi
      shift 2
      ;;
    --days)
      DAYS="${2:-}"
      if [ -z "$DAYS" ]; then
        echo "error: --days needs a number" >&2
        exit 2
      fi
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --skip-refresh)
      SKIP_REFRESH=1
      shift
      ;;
    -h|--help)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

# Run from the repository root regardless of where the script was invoked from.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"

# Resolve the database path the same way the app does: EWS_DASHBOARD_DATABASE
# overrides, else the repo-root default.
DB_PATH="${EWS_DASHBOARD_DATABASE:-$REPO_ROOT/ews-dashboard.db}"

echo "==> Repository:  $REPO_ROOT"
echo "==> Database:    $DB_PATH"
echo "==> Python:      $($PYTHON --version 2>&1)"
echo

# Refuse to clobber an existing database unless --force. This is the guard that
# keeps the script off a live 147MB database.
if [ -e "$DB_PATH" ] && [ "$FORCE" -ne 1 ]; then
  echo "error: database already exists at:" >&2
  echo "         $DB_PATH" >&2
  echo "       This script sets up a FRESH database and will not touch an" >&2
  echo "       existing one. Re-run with --force only if you are certain this" >&2
  echo "       is not a database you care about, or point" >&2
  echo "       EWS_DASHBOARD_DATABASE at a new path." >&2
  exit 1
fi

echo "==> [1/4] Installing dependencies"
$PYTHON -m pip install -r requirements.txt
echo

echo "==> [2/4] Creating the database"
$PYTHON -m ews_dashboard.db
echo

echo "==> [3/4] Running the test suite"
# The -p '*_test.py' pattern is mandatory: the test files use a _test.py suffix,
# and without this pattern unittest discovers nothing and still prints OK.
$PYTHON -m unittest discover -s tests -t . -p '*_test.py'
echo

if [ "$SKIP_REFRESH" -eq 1 ]; then
  echo "==> [4/4] Skipping ingest (--skip-refresh)"
else
  echo "==> [4/4] Ingesting builds and classifying false positives"
  echo "    (network; this is the slow step; escapes are NOT run here)"
  REFRESH_ARGS=(--skip-escapes)
  if [ -n "$DAYS" ]; then
    REFRESH_ARGS+=(--days "$DAYS")
  fi
  $PYTHON -m scripts.refresh "${REFRESH_ARGS[@]}"
fi
echo

echo "==> Done. Start the app with:"
echo "      $PYTHON -m flask --app ews_dashboard.web.app:create_app run"
echo

# Escape detection is opt-in and expensive. We only ever PRINT the command.
if [ -n "$CHECKOUT" ]; then
  DAYS_FOR_ESCAPES="${DAYS:-90}"
  LOG="escape-refresh-$(date +%Y%m%d-%H%M%S).log"
  echo "==> To populate the escapes page, run this DETACHED (it takes ~20 min of"
  echo "    serial results.webkit.org queries and must not run inline):"
  echo
  echo "      EWS_DASHBOARD_CHECKOUT='$CHECKOUT' \\"
  if [ "$DB_PATH" != "$REPO_ROOT/ews-dashboard.db" ]; then
    echo "      EWS_DASHBOARD_DATABASE='$DB_PATH' \\"
  fi
  echo "      nohup $PYTHON -u -m scripts.refresh --skip-ingest --days $DAYS_FOR_ESCAPES > '$LOG' 2>&1 &"
  echo
  echo "    Then watch it with:  tail -f '$LOG'"
else
  echo "==> The escapes page will be EMPTY until escape detection runs, which is"
  echo "    skipped without a WebKit checkout. Re-run with --checkout PATH to get"
  echo "    the exact detached command for it."
fi
