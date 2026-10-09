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
if [ ! -f "$LOCAL/minio-knowledge.env" ]; then
  # Knowledge's own object-store user (P15): uploads, source copies and
  # parser results in the anvilkit-knowledge bucket only.
  KK=$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')
  KS=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  printf 'ANVILKIT_KNOWLEDGE_OBJECTS_ACCESS_KEY_ID=anvilkit-knowledge-%s\nANVILKIT_KNOWLEDGE_OBJECTS_SECRET_ACCESS_KEY=%s\n' "$KK" "$KS" > "$LOCAL/minio-knowledge.env"
  chmod 600 "$LOCAL/minio-knowledge.env"
fi
if [ ! -f "$LOCAL/minio-removals.env" ]; then
  # P23: Knowledge's removal inventory user: the anvilkit-memory-removals
  # bucket only (list, read and create; never delete).
  RK=$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')
  RS=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  printf 'ANVILKIT_KNOWLEDGE_REMOVALS_ACCESS_KEY_ID=anvilkit-removals-%s\nANVILKIT_KNOWLEDGE_REMOVALS_SECRET_ACCESS_KEY=%s\n' "$RK" "$RS" > "$LOCAL/minio-removals.env"
  chmod 600 "$LOCAL/minio-removals.env"
fi
if [ ! -f "$LOCAL/qdrant.env" ]; then
  # P16: Qdrant's API key (Knowledge is its only client).
  QK=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  printf 'QDRANT__SERVICE__API_KEY=%s\n' "$QK" > "$LOCAL/qdrant.env"
  chmod 600 "$LOCAL/qdrant.env"
fi
QKEY=$(sed -n 's/^QDRANT__SERVICE__API_KEY=//p' "$LOCAL/qdrant.env")
PUSER=$(sed -n 's/^ANVILKIT_MODEL_PROXY_STORE_ACCESS_KEY_ID=//p' "$LOCAL/minio-model-proxy.env")
PPASS=$(sed -n 's/^ANVILKIT_MODEL_PROXY_STORE_SECRET_ACCESS_KEY=//p' "$LOCAL/minio-model-proxy.env")

# --wait only on the long-running services; schema setup runs as their dependency and
# the namespace and bucket steps are one-shots that must not trip --wait on re-runs.
docker compose -f "$ROOT/deploy/dev/compose.yaml" up -d --wait --wait-timeout 180 postgres temporal minio nats valkey-queue valkey-cache qdrant oidc
# P0.3: the development OIDC provider answers discovery once its JVM is up.
i=0
until curl -sf http://127.0.0.1:25556/anvilkit/.well-known/openid-configuration >/dev/null; do
  i=$((i + 1)); [ "$i" -lt 120 ] || { echo "the development OIDC provider did not answer discovery" >&2; exit 1; }
  sleep 1
done
docker compose -f "$ROOT/deploy/dev/compose.yaml" run --rm --no-deps temporal-namespace >/dev/null
docker compose -f "$ROOT/deploy/dev/compose.yaml" run --rm --no-deps minio-setup >/dev/null
# P14: the three domain streams of the event catalog on the JetStream.
docker compose -f "$ROOT/deploy/dev/compose.yaml" run --rm --no-deps nats-setup >/dev/null

# Control's schema is service-owned: its repository's migration Job
# (services/agent/control, cmd/anvilkit-migration) applies it; the
# knowledge and mcp schemas stay with the parent's jobs/migration until
# those services own them.
# The foundation's PostgreSQL is plaintext: -development admits it (P0.6;
# every other DSN must be sslmode=verify-full).
(cd "$ROOT/services/agent/control" && GOWORK=off go run ./cmd/anvilkit-migration -development \
  -dsn "postgres://anvilkit_control_migrator:${PW}@127.0.0.1:25432/anvilkit_control?sslmode=disable")
# P14: the owner queue relay identities and the Knowledge outbox forwarder
# identity that migration 00002 grants to (postgres-init.sh creates them on a
# fresh data directory; an existing foundation gets them here, idempotently).
for role in anvilkit_knowledge_relay anvilkit_mcp_relay anvilkit_knowledge_forwarder anvilkit_knowledge_store_migrator anvilkit_knowledge_store; do
  docker compose -f "$ROOT/deploy/dev/compose.yaml" exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d postgres -tAc \
    "SELECT 1 FROM pg_roles WHERE rolname = '$role'" | grep -qx 1 \
    || docker compose -f "$ROOT/deploy/dev/compose.yaml" exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d postgres -c \
      "CREATE ROLE $role LOGIN PASSWORD '$PW'" >/dev/null
done
for domain in knowledge mcp; do
  (cd "$ROOT/jobs/migration" && go run ./cmd/anvilkit-migration -domain "$domain" -development \
    -dsn "postgres://anvilkit_${domain}_migrator:${PW}@127.0.0.1:25432/anvilkit_${domain}?sslmode=disable")
done
# P17: the PostgresStore vendor schema of the memory projection, migrated by
# the Store's own migrations under its separate identity (Knowledge's
# store-migrate entry; the service runs with ensureTables: false).
if [ -f "$ROOT/services/agent/knowledge/dist/storemigrate.js" ]; then
  (cd "$ROOT/services/agent/knowledge" && ANVILKIT_KNOWLEDGE_STORE_MIGRATION_DEVELOPMENT=true ANVILKIT_KNOWLEDGE_STORE_MIGRATION_URL="postgres://anvilkit_knowledge_store_migrator:${PW}@127.0.0.1:25432/anvilkit_knowledge?sslmode=disable" \
    node dist/storemigrate.js)
else
  echo "UNEXECUTED: the memory Store schema (build services/agent/knowledge first, then re-run up.sh)" >&2
fi

if ! kind get clusters 2>/dev/null | grep -qx anvilkit-dev; then
  kind create cluster --config "$ROOT/deploy/dev/kind.yaml" --wait 120s
fi
kubectl --context kind-anvilkit-dev apply -f "$ROOT/deploy/dev/k8s/namespaces.yaml" >/dev/null
# P0.1: cert-manager (pinned) and the DEVELOPMENT_ONLY workload PKI
# (deploy/dev/k8s/pki.yaml): the charts' Certificates and the host-side
# leaves of deploy/dev/certs.sh are issued by the same ClusterIssuer
# anvilkit-dev-ca, so in-cluster services, host-side processes and the
# integration harness share one trust chain. The CA private key stays in
# the cluster Secret.
CERT_MANAGER_DIR="$LOCAL/cert-manager"
CERT_MANAGER_VERSION=v1.21.2
mkdir -p "$CERT_MANAGER_DIR"
if [ ! -f "$CERT_MANAGER_DIR/cert-manager.yaml" ]; then
  curl -sSL -o "$CERT_MANAGER_DIR/cert-manager.yaml" "https://github.com/cert-manager/cert-manager/releases/download/$CERT_MANAGER_VERSION/cert-manager.yaml"
fi
echo "e03b668ec8675214af6b0a671699d088f2601fa3878e0dbe1b41d3feafd1879f  $CERT_MANAGER_DIR/cert-manager.yaml" | sha256sum -c --quiet
kubectl --context kind-anvilkit-dev apply --server-side -f "$CERT_MANAGER_DIR/cert-manager.yaml" >/dev/null
for d in cert-manager cert-manager-cainjector cert-manager-webhook; do
  kubectl --context kind-anvilkit-dev -n cert-manager rollout status "deploy/$d" --timeout=240s >/dev/null
done
sh "$ROOT/deploy/dev/certs.sh" >/dev/null
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
  --set development.enabled=true --set temporal.tls.mode=development \
  --set identity.trustDomain=anvilkit.local --set identity.certificate.issuerRef.name=anvilkit-dev-ca \
  -s templates/serviceaccount.yaml -s templates/rbac.yaml -s templates/certificate.yaml \
  | kubectl --context kind-anvilkit-dev -n anvilkit-components apply -f - >/dev/null
# P0.1: the access sidecar's identity (the Certificate the Workflow chart
# renders into anvilkit-components; the launcher mounts its Secret into the
# sidecar container only) must be issued before a harness Job is launched.
kubectl --context kind-anvilkit-dev -n anvilkit-components wait certificate/anvilkit-job-access-sidecar-identity --for=condition=Ready --timeout=120s >/dev/null

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
# P0.5: the Job-boundary admission chart every environment installs (the
# components and parser template/digest policies, this environment's
# registry allowlist) and the DaemonSet that places the candidate seccomp
# profile on every node; the same chart is the qualification combination's.
# Kyverno's admission controller runs three replicas behind a PDB, so losing
# one does not stop admission.
kubectl --context kind-anvilkit-dev -n kyverno scale deploy/kyverno-admission-controller --replicas=3 >/dev/null
kubectl --context kind-anvilkit-dev apply -f - >/dev/null <<'PDB'
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: {name: kyverno-admission-controller, namespace: kyverno}
spec:
  minAvailable: 2
  selector: {matchLabels: {app.kubernetes.io/component: admission-controller, app.kubernetes.io/instance: kyverno}}
PDB
kubectl --context kind-anvilkit-dev -n kyverno rollout status deploy/kyverno-admission-controller --timeout=240s >/dev/null
for legacy in validatingpolicy/anvilkit-components-jobs validatingpolicy/anvilkit-components-pods validatingpolicy/anvilkit-components-ephemeral validatingpolicy/anvilkit-components-registries validatingpolicy/anvilkit-parsing-jobs validatingpolicy/anvilkit-parsing-pods validatingpolicy/anvilkit-parsing-ephemeral validatingpolicy/anvilkit-parsing-registries; do
  # Policies applied by earlier versions of this script are adopted by the chart.
  if kubectl --context kind-anvilkit-dev get "$legacy" >/dev/null 2>&1 \
     && [ "$(kubectl --context kind-anvilkit-dev get "$legacy" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}')" != "Helm" ]; then
    kubectl --context kind-anvilkit-dev delete "$legacy" >/dev/null
  fi
done
helm --kube-context kind-anvilkit-dev upgrade --install anvilkit-job-admission "$ROOT/deploy/policies/chart" -n kyverno \
  -f "$ROOT/deploy/dev/values/anvilkit-job-admission.yaml" --wait --timeout 4m >/dev/null
kubectl --context kind-anvilkit-dev -n kube-system rollout status ds/anvilkit-seccomp-installer --timeout=180s >/dev/null
# The development foundation's own network isolation of the Job namespaces:
# default deny and the egress rule generated below from this machine's
# addresses (its Control and object store run outside the cluster).
kubectl --context kind-anvilkit-dev apply -f "$ROOT/deploy/policies/network/anvilkit-components-egress.yaml" -f "$ROOT/deploy/policies/network/anvilkit-parsing-egress.yaml" >/dev/null
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
# P15: the Knowledge parser launcher's identity for host-side runs: the
# Knowledge chart's ServiceAccount, Role and RoleBinding rendered under the
# release name anvilkit-agent-knowledge-host into anvilkit-parsing (the
# chart owns the launcher RBAC), a 24 h token in its own kubeconfig, and the
# egress rule that admits the object store (by its kind-network address)
# for the parser Pods' trusted stager.
helm template anvilkit-agent-knowledge-host "$ROOT/services/agent/knowledge/deploy/chart" -n anvilkit-parsing \
  --set parser.enabled=true --set serviceAccount.name=anvilkit-agent-knowledge-host --set database.secret.name=unused --set nats.url=unused \
  --set forwarder.database.secret.name=unused --set relay.database.secret.name=unused --set relay.queue.secret.name=unused \
  --set development.enabled=true --set nats.tls.mode=development --set identity.certificate.issuerRef.name=anvilkit-dev-ca \
  -s templates/serviceaccount.yaml -s templates/rbac.yaml \
  | kubectl --context kind-anvilkit-dev -n anvilkit-parsing apply -f - >/dev/null
cat <<POLICY | kubectl --context kind-anvilkit-dev apply -f - >/dev/null
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: anvilkit-parsing-object-store
  namespace: anvilkit-parsing
spec:
  podSelector:
    matchLabels:
      anvilkit.io/job-kind: parser
  policyTypes: ["Egress"]
  egress:
    - to:
        - ipBlock:
            cidr: ${MINIO_KIND_IP}/32
      ports:
        - protocol: TCP
          port: 9000
POLICY
KTOKEN=$(kubectl --context kind-anvilkit-dev -n anvilkit-parsing create token anvilkit-agent-knowledge-host --duration 24h)
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
cat > "$LOCAL/knowledge-launcher.kubeconfig" <<KUBE
apiVersion: v1
kind: Config
clusters:
  - name: anvilkit-dev
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA}
users:
  - name: anvilkit-agent-knowledge-host
    user:
      token: ${KTOKEN}
contexts:
  - name: anvilkit-dev
    context:
      cluster: anvilkit-dev
      user: anvilkit-agent-knowledge-host
      namespace: anvilkit-parsing
current-context: anvilkit-dev
KUBE
chmod 600 "$LOCAL/knowledge-launcher.kubeconfig"

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
# P0.3 (DEVELOPMENT_ONLY): the issuer of the development OIDC provider
# (deploy/dev/oidc/config.json); the API verifies its tokens under
# auth.mode oidc with audience anvilkit-agent-api, the harness obtains them
# by password grant for the development users.
export ANVILKIT_DEV_OIDC_ISSUER="http://127.0.0.1:25556/anvilkit"
# P14 (DEVELOPMENT_ONLY): the JetStream, the queue Valkey (BullMQ) and the
# separate cache Valkey; the owned databases of Knowledge and MCP with their
# app, relay, forwarder and migrator identities; the placements of the
# Knowledge and MCP owners, the Knowledge forwarder sidecar, the owner queue
# relays and the Background Worker for host-side runs and the integration
# scenario (services/agent/{knowledge,mcp,background-worker}). Secrets
# (database and queue URLs) are environment-only.
export ANVILKIT_DEV_NATS_URL="nats://127.0.0.1:24222"
export ANVILKIT_DEV_QUEUE_URL="redis://127.0.0.1:26379"
export ANVILKIT_DEV_CACHE_URL="redis://127.0.0.1:26380"
export ANVILKIT_DEV_KNOWLEDGE_DSN="postgres://anvilkit_knowledge_app:${PW}@127.0.0.1:25432/anvilkit_knowledge?sslmode=disable"
export ANVILKIT_DEV_KNOWLEDGE_RELAY_DSN="postgres://anvilkit_knowledge_relay:${PW}@127.0.0.1:25432/anvilkit_knowledge?sslmode=disable"
export ANVILKIT_DEV_KNOWLEDGE_FORWARDER_DSN="postgres://anvilkit_knowledge_forwarder:${PW}@127.0.0.1:25432/anvilkit_knowledge?sslmode=disable"
export ANVILKIT_DEV_KNOWLEDGE_MIGRATOR_DSN="postgres://anvilkit_knowledge_migrator:${PW}@127.0.0.1:25432/anvilkit_knowledge?sslmode=disable"
export ANVILKIT_DEV_KNOWLEDGE_STORE_DSN="postgres://anvilkit_knowledge_store:${PW}@127.0.0.1:25432/anvilkit_knowledge?sslmode=disable"
export ANVILKIT_DEV_KNOWLEDGE_STORE_MIGRATOR_DSN="postgres://anvilkit_knowledge_store_migrator:${PW}@127.0.0.1:25432/anvilkit_knowledge?sslmode=disable"
export ANVILKIT_DEV_MCP_DSN="postgres://anvilkit_mcp_app:${PW}@127.0.0.1:25432/anvilkit_mcp?sslmode=disable"
export ANVILKIT_DEV_MCP_RELAY_DSN="postgres://anvilkit_mcp_relay:${PW}@127.0.0.1:25432/anvilkit_mcp?sslmode=disable"
export ANVILKIT_DEV_MCP_MIGRATOR_DSN="postgres://anvilkit_mcp_migrator:${PW}@127.0.0.1:25432/anvilkit_mcp?sslmode=disable"
export ANVILKIT_KNOWLEDGE_CONFIG="$ROOT/services/agent/knowledge/config.yaml"
export ANVILKIT_KNOWLEDGE_DATABASE_URL="\$ANVILKIT_DEV_KNOWLEDGE_DSN"
export ANVILKIT_KNOWLEDGE_CONTROL_ADDRESS="127.0.0.1:9101"
export ANVILKIT_FORWARDER_DATABASE_URL="\$ANVILKIT_DEV_KNOWLEDGE_FORWARDER_DSN"
export ANVILKIT_FORWARDER_NATS_URL="\$ANVILKIT_DEV_NATS_URL"
export ANVILKIT_MCP_CONFIG="$ROOT/services/agent/mcp/config.yaml"
export ANVILKIT_MCP_DATABASE_URL="\$ANVILKIT_DEV_MCP_DSN"
export ANVILKIT_MCP_NATS_URL="\$ANVILKIT_DEV_NATS_URL"
export ANVILKIT_MCP_CONTROL_ADDRESS="127.0.0.1:9101"
export ANVILKIT_BACKGROUND_WORKER_CONFIG="$ROOT/services/agent/background-worker/config.yaml"
export ANVILKIT_BACKGROUND_WORKER_QUEUE_URL="\$ANVILKIT_DEV_QUEUE_URL"
export ANVILKIT_BACKGROUND_WORKER_NATS_URL="\$ANVILKIT_DEV_NATS_URL"
export ANVILKIT_BACKGROUND_WORKER_KNOWLEDGE_ADDRESS="127.0.0.1:9105"
export ANVILKIT_BACKGROUND_WORKER_MCP_ADDRESS="127.0.0.1:9106"
export ANVILKIT_BACKGROUND_WORKER_CONTRACTS_DIR="$ROOT/contracts"
# P15 (DEVELOPMENT_ONLY): Knowledge's object store (own bucket and user; the
# host endpoint for Knowledge, the kind-network endpoint the parser Pods'
# presigned URLs are signed for), the parser launcher's kubeconfig and
# registry, and the Inference placement (a container started by the
# integration scenario or by hand: docker run -p 127.0.0.1:29108:9108
# anvilkit-agent-inference:dev with ANVILKIT_INFERENCE_LISTEN=0.0.0.0:9108 and
# the development PKI's Inference certificate as
# ANVILKIT_INFERENCE_TLS_{CERT,KEY}_FILE: it serves HTTPS only, P0.6).
export ANVILKIT_KNOWLEDGE_OBJECTS_ENDPOINT="http://127.0.0.1:29000"
export ANVILKIT_KNOWLEDGE_OBJECTS_STAGE_ENDPOINT="http://${MINIO_KIND_IP}:9000"
export ANVILKIT_KNOWLEDGE_OBJECTS_CREDENTIALS_FILE="$LOCAL/minio-knowledge.env"
export ANVILKIT_KNOWLEDGE_KUBECONFIG="$LOCAL/knowledge-launcher.kubeconfig"
export ANVILKIT_KNOWLEDGE_IMAGE_REGISTRY="localhost:5001"
export ANVILKIT_DEV_INFERENCE_URL="https://127.0.0.1:29108"
# P16 (DEVELOPMENT_ONLY): the Qdrant node of the foundation and its API key.
export ANVILKIT_KNOWLEDGE_QDRANT_URL="http://127.0.0.1:26333"
export ANVILKIT_KNOWLEDGE_QDRANT_API_KEY="${QKEY}"
# P17 (DEVELOPMENT_ONLY): the runtime Store role of the memory projection.
export ANVILKIT_KNOWLEDGE_STORE_DATABASE_URL="\$ANVILKIT_DEV_KNOWLEDGE_STORE_DSN"
# P23 (DEVELOPMENT_ONLY): Knowledge's removal inventory (its own bucket and
# user on the foundation's MinIO, so not an independent failure domain here;
# records are scoped by each database's removal scope, so test lanes on
# other databases never read the development database's records).
export ANVILKIT_KNOWLEDGE_REMOVALS_ENDPOINT="http://127.0.0.1:29000"
export ANVILKIT_KNOWLEDGE_REMOVALS_CREDENTIALS_FILE="$LOCAL/minio-removals.env"
# P0.1 (DEVELOPMENT_ONLY PKI): the workload certificates of the host-side
# processes and the integration harness, issued by the cluster's
# cert-manager (deploy/dev/certs.sh) under the trust domain anvilkit.local
# and synchronized to .local/dev/certs/<principal>/{tls.crt,tls.key,ca.crt}.
# Every internal gRPC listener and client runs mTLS by default; the
# plaintext foundation services (Temporal, NATS, OTLP) are admitted only by
# each service's development.enabled guard; Inference serves HTTPS (P0.6).
export ANVILKIT_DEV_CERTS_DIR="$LOCAL/certs"
export ANVILKIT_DEV_TRUST_DOMAIN="anvilkit.local"
export ANVILKIT_CONTROL_IDENTITY_CERT_FILE="$LOCAL/certs/anvilkit-agent-control/tls.crt"
export ANVILKIT_CONTROL_IDENTITY_KEY_FILE="$LOCAL/certs/anvilkit-agent-control/tls.key"
export ANVILKIT_CONTROL_IDENTITY_CA_FILE="$LOCAL/certs/anvilkit-agent-control/ca.crt"
export ANVILKIT_API_IDENTITY_CERT_FILE="$LOCAL/certs/anvilkit-agent-api/tls.crt"
export ANVILKIT_API_IDENTITY_KEY_FILE="$LOCAL/certs/anvilkit-agent-api/tls.key"
export ANVILKIT_API_IDENTITY_CA_FILE="$LOCAL/certs/anvilkit-agent-api/ca.crt"
export ANVILKIT_WORKFLOW_IDENTITY_CERT_FILE="$LOCAL/certs/anvilkit-agent-workflow/tls.crt"
export ANVILKIT_WORKFLOW_IDENTITY_KEY_FILE="$LOCAL/certs/anvilkit-agent-workflow/tls.key"
export ANVILKIT_WORKFLOW_IDENTITY_CA_FILE="$LOCAL/certs/anvilkit-agent-workflow/ca.crt"
export ANVILKIT_KNOWLEDGE_IDENTITY_CERT_FILE="$LOCAL/certs/anvilkit-agent-knowledge/tls.crt"
export ANVILKIT_KNOWLEDGE_IDENTITY_KEY_FILE="$LOCAL/certs/anvilkit-agent-knowledge/tls.key"
export ANVILKIT_KNOWLEDGE_IDENTITY_CA_FILE="$LOCAL/certs/anvilkit-agent-knowledge/ca.crt"
export ANVILKIT_MCP_IDENTITY_CERT_FILE="$LOCAL/certs/anvilkit-agent-mcp/tls.crt"
export ANVILKIT_MCP_IDENTITY_KEY_FILE="$LOCAL/certs/anvilkit-agent-mcp/tls.key"
export ANVILKIT_MCP_IDENTITY_CA_FILE="$LOCAL/certs/anvilkit-agent-mcp/ca.crt"
export ANVILKIT_BACKGROUND_WORKER_IDENTITY_CERT_FILE="$LOCAL/certs/anvilkit-agent-background-worker/tls.crt"
export ANVILKIT_BACKGROUND_WORKER_IDENTITY_KEY_FILE="$LOCAL/certs/anvilkit-agent-background-worker/tls.key"
export ANVILKIT_BACKGROUND_WORKER_IDENTITY_CA_FILE="$LOCAL/certs/anvilkit-agent-background-worker/ca.crt"
export ANVILKIT_MODEL_PROXY_CONTROL_IDENTITY_CERT_FILE="$LOCAL/certs/anvilkit-agent-model-proxy/tls.crt"
export ANVILKIT_MODEL_PROXY_CONTROL_IDENTITY_KEY_FILE="$LOCAL/certs/anvilkit-agent-model-proxy/tls.key"
export ANVILKIT_MODEL_PROXY_CONTROL_IDENTITY_CA_FILE="$LOCAL/certs/anvilkit-agent-model-proxy/ca.crt"
export ANVILKIT_FORWARDER_DEVELOPMENT_ENABLED="true"
export ANVILKIT_FORWARDER_NATS_TLS_MODE="development"
export ANVILKIT_INTEGRATION_IDENTITY_DIR="$LOCAL/certs/anvilkit-agent-workflow"
ENV
echo "dev foundation ready; source $LOCAL/env.sh"
