#!/usr/bin/env bash
# deploy_and_test.sh — Build image, deploy to local K8s, run full e2e tests.
#
# Prerequisites:
#   - colima / docker-desktop / k3s running
#   - kubectl configured
#   - MinIO on NodePort 30900 (minioadmin/minioadmin)
#   - MySQL on NodePort 30306 (lcp/lcp_dev_pwd, database 'lcp')
#
# Usage:
#   ./scripts/deploy_and_test.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "==== 1. Build Docker image ===="
docker build -t lcp:dev .

echo ""
echo "==== 2. Apply K8s manifests ===="
kubectl apply -f deploy/k8s/00-namespace-config.yaml
kubectl apply -f deploy/k8s/10-api-deployment.yaml
kubectl apply -f deploy/k8s/20-worker-deployment.yaml

echo ""
echo "==== 3. Wait for rollout ===="
kubectl -n lcp rollout status deployment/lcp-server --timeout=120s
kubectl -n lcp rollout status deployment/lcp-worker --timeout=120s

echo ""
echo "==== 4. Verify pods ===="
kubectl -n lcp get pods -o wide

echo ""
echo "==== 5. Health check ===="
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:30808/healthz >/dev/null 2>&1; then
        echo "REST /healthz: OK"
        break
    fi
    [ "$i" -eq 30 ] && { echo "FAIL: REST API not reachable after 30s"; exit 1; }
    sleep 1
done

echo ""
echo "==== 6. Readiness check ===="
READY=$(curl -s http://127.0.0.1:30808/readyz | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','?'))")
echo "readyz: $READY"

echo ""
echo "==== 7. Run E2E smoke tests ===="
export LCP_DB_DSN="mysql+aiomysql://lcp:lcp_dev_pwd@127.0.0.1:30306/lcp"
export LCP_LANCE_STORAGE_ENDPOINT="http://127.0.0.1:30900"
export LCP_LANCE_STORAGE_ACCESS_KEY="minioadmin"
export LCP_LANCE_STORAGE_SECRET_KEY="minioadmin"
export LCP_LANCE_STORAGE_REGION="us-east-1"
export LCP_LANCE_STORAGE_ALLOW_HTTP="true"
export LCP_LANCE_STORAGE_PATH_STYLE="true"
export LCP_ENFORCE_TENANT_RLS="false"

PYTHONPATH=src .venv/bin/python scripts/e2e_full_test.py

echo ""
echo "==== DONE ===="
