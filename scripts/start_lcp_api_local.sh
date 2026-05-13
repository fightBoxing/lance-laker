#!/usr/bin/env bash
# Starts the LCP REST API in the background against the local k8s MinIO + MySQL.
# Logs to .codebuddy/logs/lcp-api.log; PID written to .codebuddy/logs/lcp-api.pid.
#
# Idempotent: stops any prior instance started by this script first.

set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p .codebuddy/logs

PID_FILE=".codebuddy/logs/lcp-api.pid"
LOG_FILE=".codebuddy/logs/lcp-api.log"

# Stop previous instance if still alive.
if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "stopping previous LCP API pid=$OLD_PID"
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

export LCP_DB_DSN="mysql+aiomysql://lcp:lcp_dev_pwd@127.0.0.1:30306/lcp"
export LCP_REST_HOST="127.0.0.1"
export LCP_REST_PORT="8090"
export LCP_MTLS_REQUIRE_CLIENT_CERT="false"
export LCP_ENFORCE_TENANT_RLS="false"
export LCP_LANCE_STORAGE_ENDPOINT="http://127.0.0.1:30900"
export LCP_LANCE_STORAGE_ACCESS_KEY="minioadmin"
export LCP_LANCE_STORAGE_SECRET_KEY="minioadmin"
export LCP_LANCE_STORAGE_ALLOW_HTTP="true"
export LCP_LANCE_STORAGE_PATH_STYLE="true"
export LCP_APP_ENV="local-smoke"

nohup .venv/bin/python -m uvicorn lcp.api.rest.main:app \
    --host "$LCP_REST_HOST" --port "$LCP_REST_PORT" \
    > "$LOG_FILE" 2>&1 &

API_PID=$!
echo "$API_PID" > "$PID_FILE"
echo "started LCP API pid=$API_PID port=$LCP_REST_PORT log=$LOG_FILE"
