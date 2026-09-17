#!/bin/sh
# Brings up the DEVELOPMENT_ONLY foundation: PostgreSQL 17 + Temporal 1.31.2 +
# the MinIO object stores (the versioned artifact bucket of P08, the shared
# inventory bucket of the in-cluster Control replicas and the Model Proxy's
# record/evidence bucket of P11) in the anvilkit-dev
# Compose project, the three owned schemas (Control's through its own
# repository's migration Job, knowledge/mcp through jobs/migration), and a
# kind cluster with the least-privilege launcher identity for host-side
# workers, rendered from the Workflow chart that owns it. Idempotent.
# Writes only under .local/dev/ (gitignored). Never touches anvilkit-local.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
LOCAL="$ROOT/.local/dev"
mkdir -p "$LOCAL"
export PATH="$PATH:$(go env GOPATH)/bin:$ROOT/.local/bin"

if [ ! -f "$LOCAL/postgres.env" ]; then
  PW=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
  printf 'POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=%s\nANVILKIT_DEV_PASSWORD=%s\n' "$PW" "$PW" > "$LOCAL/postgres.env"
  printf 'SQL_USER=temporal\nSQL_PASSWORD=%s\nPOSTGRES_USER=temporal\nPOSTGRES_PWD=%s\n' "$PW" "$PW" > "$LOCAL/temporal.env"
  chmod 600 "$LOCAL/postgres.env" "$LOCAL/temporal.env"
fi
PW=$(sed -n 's/^ANVILKIT_DEV_PASSWORD=//p' "$LOCAL/postgres.env")
if [ ! -f "$LOCAL/minio.env" ]; then
  # The artifact store's own credentials: a separate identity from the
  # database roles and from any inventory backend.
  MK=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 20)
  MS=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  printf 'MINIO_ROOT_USER=anvilkit-artifacts-%s\nMINIO_ROOT_PASSWORD=%s\n' "$MK" "$MS" > "$LOCAL/minio.env"
  chmod 600 "$LOCAL/minio.env"
fi
MUSER=$(sed -n 's/^MINIO_ROOT_USER=//p' "$LOCAL/minio.env")
MPASS=$(sed -n 's/^MINIO_ROOT_PASSWORD=//p' "$LOCAL/minio.env")
if [ ! -f "$LOCAL/minio-inventory.env" ]; then
  # The shared inventory's own user: a separate identity from the artifact
  # store's credentials (Control refuses one access key for both).
  IK=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 20)
  IS=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  printf 'ANVILKIT_INVENTORY_ACCESS_KEY_ID=anvilkit-inventory-%s\nANVILKIT_INVENTORY_SECRET_ACCESS_KEY=%s\n' "$IK" "$IS" > "$LOCAL/minio-inventory.env"
  chmod 600 "$LOCAL/minio-inventory.env"
fi
IUSER=$(sed -n 's/^ANVILKIT_INVENTORY_ACCESS_KEY_ID=//p' "$LOCAL/minio-inventory.env")
IPASS=$(sed -n 's/^ANVILKIT_INVENTORY_SECRET_ACCESS_KEY=//p' "$LOCAL/minio-inventory.env")
if [ ! -f "$LOCAL/minio-model-proxy.env" ]; then
  # The Model Proxy's record and evidence store (P11): its own user and
  # bucket, a permission boundary of its own beside the artifact store and
  # the inventory.
  PK=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 20)
  PS=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  printf 'ANVILKIT_MODEL_PROXY_STORE_ACCESS_KEY_ID=anvilkit-model-proxy-%s\nANVILKIT_MODEL_PROXY_STORE_SECRET_ACCESS_KEY=%s\n' "$PK" "$PS" > "$LOCAL/minio-model-proxy.env"
  chmod 600 "$LOCAL/minio-model-proxy.env"
fi
PUSER=$(sed -n 's/^ANVILKIT_MODEL_PROXY_STORE_ACCESS_KEY_ID=//p' "$LOCAL/minio-model-proxy.env")
PPASS=$(sed -n 's/^ANVILKIT_MODEL_PROXY_STORE_SECRET_ACCESS_KEY=//p' "$LOCAL/minio-model-proxy.env")

# --wait only on the long-running services; schema setup runs as their dependency and
# the namespace and bucket steps are one-shots that must not trip --wait on re-runs.
docker compose -f "$ROOT/deploy/dev/compose.yaml" up -d --wait --wait-timeout 180 postgres temporal minio
docker compose -f "$ROOT/deploy/dev/compose.yaml" run --rm --no-deps temporal-namespace >/dev/null
docker compose -f "$ROOT/deploy/dev/compose.yaml" run --rm --no-deps minio-setup >/dev/null

# Control's schema is service-owned: its repository's migration Job
# (services/agent/control, cmd/anvilkit-migration) applies it; the
# knowledge and mcp schemas stay with the parent's jobs/migration until
# those services own them.
(cd "$ROOT/services/agent/control" && GOWORK=off go run ./cmd/anvilkit-migration \
  -dsn "postgres://anvilkit_control_migrator:${PW}@127.0.0.1:25432/anvilkit_control?sslmode=disable")
for domain in knowledge mcp; do
  (cd "$ROOT/jobs/migration" && go run ./cmd/anvilkit-migration -domain "$domain" \
    -dsn "postgres://anvilkit_${domain}_migrator:${PW}@127.0.0.1:25432/anvilkit_${domain}?sslmode=disable")
done

if ! kind get clusters 2>/dev/null | grep -qx anvilkit-dev; then
  kind create cluster --config "$ROOT/deploy/dev/kind.yaml" --wait 120s
fi
kubectl --context kind-anvilkit-dev apply -f "$ROOT/deploy/dev/k8s/namespaces.yaml" >/dev/null
# The launcher identity of a worker outside the cluster (the integration
# scenarios and manual runs start the Workflow on the host): the Workflow
# chart is the single owner of the launcher RBAC, so the host-side identity
# is that chart's ServiceAccount, Role and RoleBinding rendered under the
# release name anvilkit-agent-workflow-host into anvilkit-components; the
# in-cluster release (deploy/dev/workflow-chart.sh) creates its own from the
# same templates. The dev-script-managed copy of earlier passes
# (ServiceAccount anvilkit-agent-workflow and Role/RoleBinding
# anvilkit-agent-workflow-launcher in anvilkit-components, applied by kubectl)
# is removed so the chart release can own those names.
for kind_name in serviceaccount/anvilkit-agent-workflow role/anvilkit-agent-workflow-launcher rolebinding/anvilkit-agent-workflow-launcher; do
  if kubectl --context kind-anvilkit-dev -n anvilkit-components get "$kind_name" >/dev/null 2>&1 \
     && [ "$(kubectl --context kind-anvilkit-dev -n anvilkit-components get "$kind_name" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}')" != "Helm" ]; then
    kubectl --context kind-anvilkit-dev -n anvilkit-components delete "$kind_name" >/dev/null
  fi
done
helm template anvilkit-agent-workflow-host "$ROOT/services/agent/workflow/deploy/chart" -n anvilkit-components \
  --set temporal.address=unused --set control.address=unused --set launcher.backend=kind-anvilkit-dev \
  --set launcher.imageRegistry=unused --set launcher.sidecarControlAddress=unused \
  -s templates/serviceaccount.yaml -s templates/rbac.yaml \
  | kubectl --context kind-anvilkit-dev -n anvilkit-components apply -f - >/dev/null

# P09 (DEVELOPMENT_ONLY runtime inputs of the trusted Job boundary; none of
# them qualifies gVisor, the RKE2/Cilium combination or a production registry):
# a local image registry on the kind network reached by digest through the
# node's containerd hosts configuration, the components node pool label, the
# candidate syscall profile on the node, Kyverno 1.19.1 and the P09 policies,
# and the MinIO artifact store attached to the kind network so a Job's sidecar
# can reach the presigned upload it was issued.
NODE=anvilkit-dev-control-plane
REGISTRY_IMAGE=registry:3@sha256:1be55279f18a2fe1a74edf2664cac61c1bea305b7b4642dab412e7affdcb3e33
if ! docker ps -a --format '{{.Names}}' | grep -qx anvilkit-dev-registry; then
  docker run -d --restart=always --name anvilkit-dev-registry --network kind -p 127.0.0.1:5001:5000 "$REGISTRY_IMAGE" >/dev/null
fi
docker start anvilkit-dev-registry >/dev/null
docker exec "$NODE" sh -c 'mkdir -p "/etc/containerd/certs.d/localhost:5001" && printf "server = \"http://anvilkit-dev-registry:5000\"\n\n[host.\"http://anvilkit-dev-registry:5000\"]\n  capabilities = [\"pull\", \"resolve\"]\n" > "/etc/containerd/certs.d/localhost:5001/hosts.toml"'
if ! docker exec "$NODE" grep -q 'config_path = "/etc/containerd/certs.d"' /etc/containerd/config.toml; then
  docker exec "$NODE" sh -c 'printf "\n[plugins.\"io.containerd.grpc.v1.cri\".registry]\n  config_path = \"/etc/containerd/certs.d\"\n" >> /etc/containerd/config.toml && systemctl restart containerd'
fi
kubectl --context kind-anvilkit-dev label node "$NODE" anvilkit.io/pool=components --overwrite >/dev/null
docker exec "$NODE" mkdir -p /var/lib/kubelet/seccomp/anvilkit
docker cp "$ROOT/deploy/policies/seccomp/anvilkit-candidate.json" "$NODE:/var/lib/kubelet/seccomp/anvilkit/candidate.json"
docker exec "$NODE" chmod 0644 /var/lib/kubelet/seccomp/anvilkit/candidate.json
KYVERNO_DIR="$LOCAL/kyverno"
KYVERNO_VERSION=v1.19.1
mkdir -p "$KYVERNO_DIR"
if [ ! -f "$KYVERNO_DIR/install.yaml" ]; then
  curl -sSL -o "$KYVERNO_DIR/install.yaml" "https://github.com/kyverno/kyverno/releases/download/$KYVERNO_VERSION/install.yaml"
fi
if [ ! -x "$KYVERNO_DIR/kyverno" ]; then
  curl -sSL -o "$KYVERNO_DIR/kyverno-cli.tar.gz" "https://github.com/kyverno/kyverno/releases/download/$KYVERNO_VERSION/kyverno-cli_${KYVERNO_VERSION}_linux_x86_64.tar.gz"
  tar -xzf "$KYVERNO_DIR/kyverno-cli.tar.gz" -C "$KYVERNO_DIR" kyverno
fi
echo "d3322cb346d3d42dd0f41e230b0d1d7bc5619960e1c36fdac4d9151d724b88e6  $KYVERNO_DIR/install.yaml" | sha256sum -c --quiet
echo "b38228f367fc0fdc2b08f4c83ea50ac5f16c60ff8d62d76a66157c33c47b70ae  $KYVERNO_DIR/kyverno-cli.tar.gz" | sha256sum -c --quiet
kubectl --context kind-anvilkit-dev apply --server-side -f "$KYVERNO_DIR/install.yaml" >/dev/null
kubectl --context kind-anvilkit-dev -n kyverno rollout status deploy/kyverno-admission-controller --timeout=240s >/dev/null
kubectl --context kind-anvilkit-dev apply -f "$ROOT/deploy/policies/kyverno/anvilkit-components-jobs.yaml" -f "$ROOT/deploy/policies/kyverno/anvilkit-components-registries.dev.yaml" -f "$ROOT/deploy/policies/network/anvilkit-components-egress.yaml" >/dev/null
# The foundation's PostgreSQL, Temporal and MinIO join the kind network so
# the Control and Workflow releases in the cluster (and a Job's sidecar)
# reach them by their kind-network addresses; the loopback-only published
# ports stay the host side's entry.
for c in anvilkit-dev-minio-1 anvilkit-dev-postgres-1 anvilkit-dev-temporal-1; do
  if ! docker network inspect kind --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}' | grep -qx "$c"; then
    docker network connect kind "$c"
  fi
done
KIND_GATEWAY=$(docker network inspect kind --format '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}} {{end}}{{end}}' | tr ' ' '\n' | grep -m1 '^[0-9]')
MINIO_KIND_IP=$(docker inspect anvilkit-dev-minio-1 --format '{{(index .NetworkSettings.Networks "kind").IPAddress}}')
POSTGRES_KIND_IP=$(docker inspect anvilkit-dev-postgres-1 --format '{{(index .NetworkSettings.Networks "kind").IPAddress}}')
TEMPORAL_KIND_IP=$(docker inspect anvilkit-dev-temporal-1 --format '{{(index .NetworkSettings.Networks "kind").IPAddress}}')
TOKEN=$(kubectl --context kind-anvilkit-dev -n anvilkit-components create token anvilkit-agent-workflow-host --duration 24h)
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
  - name: anvilkit-agent-workflow-host
    user:
      token: ${TOKEN}
contexts:
  - name: anvilkit-dev
    context:
      cluster: anvilkit-dev
      user: anvilkit-agent-workflow-host
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
if [ ! -f "$LOCAL/model-proxy-principals.json" ]; then
  # The Model Proxy's DEVELOPMENT_ONLY bearer principals (P11): one token for
  # the Workflow's Activities, one for the access sidecar of a Job, one for
  # Control's original-identity query.
  PW_TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  PS_TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  PC_TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  cat > "$LOCAL/model-proxy-principals.json" <<JSON
{
  "${PW_TOKEN}": {"principalId": "anvilkit-agent-workflow", "kind": "workflow"},
  "${PS_TOKEN}": {"principalId": "anvilkit-job-access-sidecar", "kind": "sidecar"},
  "${PC_TOKEN}": {"principalId": "anvilkit-agent-control", "kind": "control"}
}
JSON
  chmod 600 "$LOCAL/model-proxy-principals.json"
fi
PW_TOKEN=$(python3 -c "import json; d=json.load(open('$LOCAL/model-proxy-principals.json')); print(next(k for k,v in d.items() if v['kind']=='workflow'))")
PS_TOKEN=$(python3 -c "import json; d=json.load(open('$LOCAL/model-proxy-principals.json')); print(next(k for k,v in d.items() if v['kind']=='sidecar'))")
PC_TOKEN=$(python3 -c "import json; d=json.load(open('$LOCAL/model-proxy-principals.json')); print(next(k for k,v in d.items() if v['kind']=='control'))")

cat > "$LOCAL/env.sh" <<ENV
# source this file: DEVELOPMENT_ONLY connection settings for the replacement services
export ANVILKIT_DEV_CONTROL_DSN="postgres://anvilkit_control_app:${PW}@127.0.0.1:25432/anvilkit_control?sslmode=disable"
export ANVILKIT_DEV_CONTROL_MIGRATOR_DSN="postgres://anvilkit_control_migrator:${PW}@127.0.0.1:25432/anvilkit_control?sslmode=disable"
export ANVILKIT_DEV_TEMPORAL_ADDRESS="127.0.0.1:27233"
export KUBECONFIG="$LOCAL/launcher.kubeconfig"
# Service configuration for manual runs (see deploy/dev/README.md): each
# service reads its reviewed config.yaml; these are the allowed placement
# overrides and the secret (database URL) supplied only through the environment.
export ANVILKIT_CONTROL_CONFIG="$ROOT/services/agent/control/config.yaml"
export ANVILKIT_API_CONFIG="$ROOT/services/agent/api/config.yaml"
export ANVILKIT_WORKFLOW_CONFIG="$ROOT/services/agent/workflow/config.yaml"
export ANVILKIT_CONTROL_DATABASE_URL="\$ANVILKIT_DEV_CONTROL_DSN"
export ANVILKIT_CONTROL_INVENTORY_DIR="$LOCAL/inventory"
export ANVILKIT_CONTROL_TEMPORAL_ADDRESS="\$ANVILKIT_DEV_TEMPORAL_ADDRESS"
# The artifact store (P08): the MinIO of the foundation, its versioned bucket
# and its own credentials (secrets, environment-only).
export ANVILKIT_DEV_ARTIFACTS_ENDPOINT="http://127.0.0.1:29000"
export ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT="\$ANVILKIT_DEV_ARTIFACTS_ENDPOINT"
export ANVILKIT_CONTROL_ARTIFACTS_S3_BUCKET="anvilkit-artifacts"
export ANVILKIT_CONTROL_ARTIFACTS_S3_ACCESS_KEY_ID="${MUSER}"
export ANVILKIT_CONTROL_ARTIFACTS_S3_SECRET_ACCESS_KEY="${MPASS}"
export ANVILKIT_API_CONTROL_ADDRESS="127.0.0.1:9101"
export ANVILKIT_API_AUTH_MODE="fixture"
export ANVILKIT_API_PRINCIPALS_FILE="$LOCAL/api-principals.json"
export ANVILKIT_WORKFLOW_TEMPORAL_ADDRESS="\$ANVILKIT_DEV_TEMPORAL_ADDRESS"
export ANVILKIT_WORKFLOW_CONTROL_ADDRESS="127.0.0.1:9101"
export ANVILKIT_WORKFLOW_KUBECONFIG="$LOCAL/launcher.kubeconfig"
export ANVILKIT_WORKFLOW_LAUNCH_BACKEND="kind-anvilkit-dev"
export ANVILKIT_WORKFLOW_IMAGE_REGISTRY="localhost:5001"
# P09 (DEVELOPMENT_ONLY): the kind network gateway (a host-side Control that
# listens on 0.0.0.0 is reached from a Job Pod at this address), the artifact
# store as the kind network sees it (Control's S3 endpoint for a run whose
# uploads come from inside the cluster) and the Kyverno CLI for offline checks.
export ANVILKIT_DEV_KIND_GATEWAY="${KIND_GATEWAY}"
export ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER="http://${MINIO_KIND_IP}:9000"
export ANVILKIT_DEV_REGISTRY="localhost:5001"
# The foundation as the kind cluster reaches it (deploy/dev/{control,workflow}-chart.sh):
# the database and Temporal on the kind network, the shared inventory
# bucket with its own credentials (secrets, environment-only).
export ANVILKIT_DEV_CONTROL_DSN_CLUSTER="postgres://anvilkit_control_app:${PW}@${POSTGRES_KIND_IP}:5432/anvilkit_control?sslmode=disable"
export ANVILKIT_DEV_CONTROL_MIGRATOR_DSN_CLUSTER="postgres://anvilkit_control_migrator:${PW}@${POSTGRES_KIND_IP}:5432/anvilkit_control?sslmode=disable"
export ANVILKIT_DEV_TEMPORAL_ADDRESS_CLUSTER="${TEMPORAL_KIND_IP}:7233"
export ANVILKIT_DEV_INVENTORY_BUCKET="anvilkit-inventory"
export ANVILKIT_DEV_INVENTORY_ACCESS_KEY_ID="${IUSER}"
export ANVILKIT_DEV_INVENTORY_SECRET_ACCESS_KEY="${IPASS}"
# The Model Proxy (P11): its reviewed file, the contracts it validates
# against, the Control placement, its DEVELOPMENT_ONLY bearer principals and
# its record/evidence store on the MinIO (own bucket and user; secrets,
# environment-only). Host-side runs and the integration scenarios keep the
# filesystem store; the chart uses the bucket. No route credential is set
# here: the controlled route is enabled only by the scenarios' own copies.
export ANVILKIT_MODEL_PROXY_CONFIG="$ROOT/services/agent/model-proxy/config.yaml"
export ANVILKIT_MODEL_PROXY_CONTRACTS_DIR="$ROOT/contracts"
export ANVILKIT_MODEL_PROXY_CONTROL_ADDRESS="127.0.0.1:9101"
export ANVILKIT_MODEL_PROXY_PRINCIPALS_FILE="$LOCAL/model-proxy-principals.json"
export ANVILKIT_MODEL_PROXY_STORE_DIR="$LOCAL/model-proxy-store"
export ANVILKIT_DEV_MODEL_PROXY_BUCKET="anvilkit-model-proxy"
export ANVILKIT_DEV_MODEL_PROXY_ACCESS_KEY_ID="${PUSER}"
export ANVILKIT_DEV_MODEL_PROXY_SECRET_ACCESS_KEY="${PPASS}"
export ANVILKIT_DEV_MODEL_PROXY_TOKEN_WORKFLOW="${PW_TOKEN}"
export ANVILKIT_DEV_MODEL_PROXY_TOKEN_SIDECAR="${PS_TOKEN}"
export ANVILKIT_DEV_MODEL_PROXY_TOKEN_CONTROL="${PC_TOKEN}"
export ANVILKIT_KYVERNO_CLI="$KYVERNO_DIR/kyverno"
export ANVILKIT_DEV_API_TOKEN_A="${TA}"
ENV
echo "dev foundation ready; source $LOCAL/env.sh"
