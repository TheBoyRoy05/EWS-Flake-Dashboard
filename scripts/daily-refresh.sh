#!/bin/sh
# One day's data refresh: ingest builds, then ask main what happened to each conviction.
#
# Written to be run unattended by launchd (see scripts/com.ewsdashboard.refresh.plist), which is why
# it assumes nothing about the environment: a launchd job inherits a minimal PATH and no shell
# profile, so every interpreter and binary here is named by absolute path.
#
# Safe to run by hand at any time. It is idempotent in the sense that matters: a second copy started
# while one is running exits immediately rather than competing for the database.

set -u

REPO=/Users/issacroy/Work/Flakes
CHECKOUT=/Users/issacroy/Work/WebKit
PYTHON=/usr/bin/python3
GIT=/usr/bin/git
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/refresh-$(/bin/date -u +%Y-%m-%d).log"
LOCK="$LOG_DIR/.refresh.lock"
KEEP_LOGS=14
DAYS=90

/bin/mkdir -p "$LOG_DIR"

say() {
    echo "$(/bin/date -u '+%Y-%m-%d %H:%M:%S UTC') $*" >> "$LOG"
}

# mkdir is atomic, so it is the lock. A stale lock from a killed run would block every later run, so
# the pid inside it is checked rather than trusted: a lock whose owner is gone is taken over.
if ! /bin/mkdir "$LOCK" 2>/dev/null; then
    if [ -f "$LOCK/pid" ] && /bin/kill -0 "$(/bin/cat "$LOCK/pid")" 2>/dev/null; then
        say "another refresh is running (pid $(/bin/cat "$LOCK/pid")); exiting"
        exit 0
    fi
    say "clearing a stale lock left by pid $(/bin/cat "$LOCK/pid" 2>/dev/null || echo unknown)"
    /bin/rm -rf "$LOCK"
    /bin/mkdir "$LOCK" || { say "could not take the lock; exiting"; exit 1; }
fi
echo $$ > "$LOCK/pid"
trap '/bin/rm -rf "$LOCK"' EXIT INT TERM

# Both passes only read public HTTP APIs, so with no route out they would spend their time timing out
# one query at a time. A wake with no network yet is the normal case for a laptop lid opening, so this
# waits a little rather than giving up at once.
reachable() {
    /usr/bin/curl -s -o /dev/null --max-time 10 https://results.webkit.org/api/commits 2>/dev/null
}
waited=0
while ! reachable; do
    if [ "$waited" -ge 300 ]; then
        say "results.webkit.org unreachable after ${waited}s; giving up until the next run"
        exit 0
    fi
    /bin/sleep 30
    waited=$((waited + 30))
done
[ "$waited" -gt 0 ] && say "network arrived after ${waited}s"

say "=== refresh starting (window ${DAYS}d) ==="

# Landing detection reads this checkout, and a stale one reports landed changes as never landed, so
# the fetch is part of the refresh rather than something to remember separately. A failure here is
# not fatal: the ingest below is still worth doing.
if [ -d "$CHECKOUT/.git" ]; then
    if "$GIT" -C "$CHECKOUT" fetch -q origin main 2>>"$LOG"; then
        say "fetched origin/main -> $("$GIT" -C "$CHECKOUT" rev-parse --short origin/main)"
    else
        say "WARNING: could not fetch origin/main; landing detection may read stale"
    fi
else
    say "WARNING: no checkout at $CHECKOUT; escape detection will be skipped"
fi

cd "$REPO" || { say "repo missing; exiting"; exit 1; }

say "--- ingesting builds ---"
if "$PYTHON" -u -m scripts.refresh --days "$DAYS" >> "$LOG" 2>&1; then
    say "ingest finished"
else
    say "ingest FAILED (exit $?)"
fi

# Escape detection is skipped silently unless this variable points at a checkout, which is why the
# path is passed explicitly here rather than left to the environment launchd provides.
if [ -d "$CHECKOUT/.git" ]; then
    say "--- asking main what happened to each conviction ---"
    if EWS_DASHBOARD_CHECKOUT="$CHECKOUT" \
        "$PYTHON" -u -m scripts.refresh --skip-ingest --days "$DAYS" >> "$LOG" 2>&1; then
        say "escape detection finished"
    else
        say "escape detection FAILED (exit $?)"
    fi
fi

say "=== refresh done ==="

# Keep the log directory from growing without bound.
/usr/bin/find "$LOG_DIR" -name 'refresh-*.log' -type f -mtime "+$KEEP_LOGS" -delete 2>/dev/null

exit 0
