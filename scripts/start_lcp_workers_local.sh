#!/usr/bin/env bash
# Starts the lifecycle worker + index watcher in the background against
# the local k8s MinIO + MySQL.  Logs to .codebuddy/logs/{worker,watcher}.log;
# PIDs written to .codebuddy/logs/{worker,watcher}.pid.
#
# Idempotent: stops any prior instance started by this script first.
#
# Why one script and not two: the two processes are *paired* — the
# watcher produces INDEX_OPTIMIZE tasks and the worker consumes them.
# Bringing them up together avoids a window where one is alive without
# the other and writes look "stuck".

set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p .codebuddy/logs

WORKER_PID_FILE=".codebuddy/logs/worker.pid"
WORKER_LOG_FILE=".codebuddy/logs/worker.log"
WATCHER_PID_FILE=".codebuddy/logs/watcher.pid"
WATCHER_LOG_FILE=".codebuddy/logs/watcher.log"

# Stop previous instances if still alive.
for PIDFILE in "$WORKER_PID_FILE" "$WATCHER_PID_FILE"; do
    if [[ -f "$PIDFILE" ]]; then
        OLD_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
        if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
            echo "stopping previous process pid=$OLD_PID ($PIDFILE)"
            kill "$OLD_PID" 2>/dev/null || true
        fi
        rm -f "$PIDFILE"
    fi
done
sleep 1

# Same env block as start_lcp_api_local.sh so all three processes share
# one source of truth for DB DSN + lance storage credentials.
export LCP_DB_DSN="mysql+aiomysql://lcp:lcp_dev_pwd@127.0.0.1:30306/lcp"
export LCP_ENFORCE_TENANT_RLS="false"
export LCP_LANCE_STORAGE_ENDPOINT="http://127.0.0.1:30900"
export LCP_LANCE_STORAGE_ACCESS_KEY="minioadmin"
export LCP_LANCE_STORAGE_SECRET_KEY="minioadmin"
export LCP_LANCE_STORAGE_ALLOW_HTTP="true"
export LCP_LANCE_STORAGE_PATH_STYLE="true"
export LCP_APP_ENV="local-smoke"

# Watcher poll interval (seconds).  Pre-existing callers can still
# override via ``LCP_INDEX_WATCHER_INTERVAL_SECONDS=<n> bash ...``
# without editing this file.  5s is tighter than the 10s production
# default to keep local smoke turnaround snappy.
: "${LCP_INDEX_WATCHER_INTERVAL_SECONDS:=5}"
export LCP_INDEX_WATCHER_INTERVAL_SECONDS

# 1. Lifecycle worker (task consumer).
nohup .venv/bin/python -m lcp.workers.lifecycle_worker \
    --idle-seconds=2 --busy-seconds=0.1 \
    > "$WORKER_LOG_FILE" 2>&1 &
WORKER_PID=$!
echo "$WORKER_PID" > "$WORKER_PID_FILE"
echo "started worker  pid=$WORKER_PID  log=$WORKER_LOG_FILE"

# 2. Index watcher (task producer).
#    Interval comes from LCP_INDEX_WATCHER_INTERVAL_SECONDS (env);
#    no --interval-seconds flag here so the env value wins.
nohup .venv/bin/python -m lcp.workers.index_watcher_cli \
    > "$WATCHER_LOG_FILE" 2>&1 &
WATCHER_PID=$!
echo "$WATCHER_PID" > "$WATCHER_PID_FILE"
echo "started watcher pid=$WATCHER_PID log=$WATCHER_LOG_FILE  interval=${LCP_INDEX_WATCHER_INTERVAL_SECONDS}s"

echo ""
echo "tail logs with:"
echo "  tail -f $WORKER_LOG_FILE"
echo "  tail -f $WATCHER_LOG_FILE"
