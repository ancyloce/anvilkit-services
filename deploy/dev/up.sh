#!/bin/sh
# Brings up the DEVELOPMENT_ONLY foundation: PostgreSQL 17 + Temporal 1.31.2 in
# the anvilkit-dev Compose project, the three owned schemas at 00001_init.sql,
# and a kind cluster with the least-privilege launcher identity. Idempotent.
# Writes only under .local/dev/ (gitignored). Never touches anvilkit-local.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
LOCAL="$ROOT/.local/dev"
mkdir -p "$LOCAL"
export PATH="$PATH:$(go env GOPATH)/bin"

if [ ! -f "$LOCAL/postgres.env" ]; then
  PW=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
  printf 'POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=%s\nANVILKIT_DEV_PASSWORD=%s\n' "$PW" "$PW" > "$LOCAL/postgres.env"
  printf 'SQL_USER=temporal\nSQL_PASSWORD=%s\nPOSTGRES_USER=temporal\nPOSTGRES_PWD=%s\n' "$PW" "$PW" > "$LOCAL/temporal.env"
  chmod 600 "$LOCAL/postgres.env" "$LOCAL/temporal.env"
fi
PW=$(sed -n 's/^ANVILKIT_DEV_PASSWORD=//p' "$LOCAL/postgres.env")

# --wait only on the long-running services; schema setup runs as their dependency and
# the namespace step is a one-shot that must not trip --wait on re-runs.
docker compose -f "$ROOT/deploy/dev/compose.yaml" up -d --wait --wait-timeout 180 postgres temporal
docker compose -f "$ROOT/deploy/dev/compose.yaml" run --rm --no-deps temporal-namespace >/dev/null

for domain in control knowledge mcp; do
  (cd "$ROOT/jobs/migration" && go run ./cmd/anvilkit-migration -domain "$domain" \
    -dsn "postgres://anvilkit_${domain}_migrator:${PW}@127.0.0.1:25432/anvilkit_${domain}?sslmode=disable")
done

if ! kind get clusters 2>/dev/null | grep -qx anvilkit-dev; then
  kind create cluster --config "$ROOT/deploy/dev/kind.yaml" --wait 120s
fi
kubectl --context kind-anvilkit-dev apply -f "$ROOT/deploy/dev/k8s/workflow-launcher.yaml" >/dev/null
TOKEN=$(kubectl --context kind-anvilkit-dev -n anvilkit-components create token anvilkit-agent-workflow --duration 24h)
SERVER=$(kubectl config view --raw -o jsonpath='{.clusters[?(@.name=="kind-anvilkit-dev")].cluster.server}')
CA=$(kubectl config view --raw -o jsonpath='{.clusters[?(@.name=="kind-anvilkit-dev")].cluster.certificate-authority-data}')
cat > "$LOCAL/launcher.kubeconfig" <<KUBE
apiVersion: v1
kind: Config
clusters:
  - name: anvilkit-dev
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA}
users:
  - name: anvilkit-agent-workflow
    user:
      token: ${TOKEN}
contexts:
  - name: anvilkit-dev
    context:
      cluster: anvilkit-dev
      user: anvilkit-agent-workflow
      namespace: anvilkit-components
current-context: anvilkit-dev
KUBE
chmod 600 "$LOCAL/launcher.kubeconfig"

if [ ! -f "$LOCAL/api-principals.json" ]; then
  TA=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  TB=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  cat > "$LOCAL/api-principals.json" <<JSON
{
  "${TA}": {"tenantId": "tenant_a", "projectId": "proj_a", "actorId": "user_a", "roles": ["author"]},
  "${TB}": {"tenantId": "tenant_b", "projectId": "proj_b", "actorId": "user_b", "roles": ["author"]}
}
JSON
  chmod 600 "$LOCAL/api-principals.json"
fi
TA=$(python3 -c "import json,sys; d=json.load(open('$LOCAL/api-principals.json')); print(next(k for k,v in d.items() if v['tenantId']=='tenant_a'))")

cat > "$LOCAL/env.sh" <<ENV
# source this file: DEVELOPMENT_ONLY connection settings for the replacement services
export ANVILKIT_DEV_CONTROL_DSN="postgres://anvilkit_control_app:${PW}@127.0.0.1:25432/anvilkit_control?sslmode=disable"
export ANVILKIT_DEV_CONTROL_MIGRATOR_DSN="postgres://anvilkit_control_migrator:${PW}@127.0.0.1:25432/anvilkit_control?sslmode=disable"
export ANVILKIT_DEV_TEMPORAL_ADDRESS="127.0.0.1:27233"
export KUBECONFIG="$LOCAL/launcher.kubeconfig"
# Service configuration for manual runs (see deploy/dev/README.md): each
# service reads its reviewed config.yaml; these are the allowed placement
# overrides and the secret (database URL) supplied only through the environment.
export ANVILKIT_CONTROL_CONFIG="$ROOT/services/anvilkit-agent-control/config.yaml"
export ANVILKIT_API_CONFIG="$ROOT/services/agent/api/config.yaml"
export ANVILKIT_WORKFLOW_CONFIG="$ROOT/services/anvilkit-agent-workflow/config.yaml"
export ANVILKIT_CONTROL_DATABASE_URL="\$ANVILKIT_DEV_CONTROL_DSN"
export ANVILKIT_CONTROL_INVENTORY_DIR="$LOCAL/inventory"
export ANVILKIT_CONTROL_TEMPORAL_ADDRESS="\$ANVILKIT_DEV_TEMPORAL_ADDRESS"
export ANVILKIT_API_CONTROL_ADDRESS="127.0.0.1:9101"
export ANVILKIT_API_AUTH_MODE="fixture"
export ANVILKIT_API_PRINCIPALS_FILE="$LOCAL/api-principals.json"
export ANVILKIT_WORKFLOW_TEMPORAL_ADDRESS="\$ANVILKIT_DEV_TEMPORAL_ADDRESS"
export ANVILKIT_WORKFLOW_CONTROL_ADDRESS="127.0.0.1:9101"
export ANVILKIT_WORKFLOW_KUBECONFIG="$LOCAL/launcher.kubeconfig"
export ANVILKIT_WORKFLOW_LAUNCH_BACKEND="kind-anvilkit-dev"
export ANVILKIT_DEV_API_TOKEN_A="${TA}"
ENV
echo "dev foundation ready; source $LOCAL/env.sh"
