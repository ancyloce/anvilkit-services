#!/bin/sh
# Brings the Compose placement up on the server, idempotently (bash or dash):
# generates the secrets that do not exist yet as restricted files under
# ${ANVILKIT_HOME}/secrets (never printed), issues the PKI (pki.sh), pulls the
# digest-pinned images named by images.env, starts the foundation, runs the
# one-shot setup and migration services in order, then starts the services and
# waits for their health checks. Writes only under ${ANVILKIT_HOME}; deletes
# nothing (down.sh stops; volumes stay unless --volumes is asked for).
#
#   ANVILKIT_HOME=/srv/anvilkit sh deploy/compose/up.sh            # everything
#   sh deploy/compose/up.sh foundation|setup|migrate|services|status
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
HOME_DIR=${ANVILKIT_HOME:-/srv/anvilkit}
export ANVILKIT_HOME="$HOME_DIR"
SECRETS="$HOME_DIR/secrets"
COMPOSE="docker compose --project-directory $HERE --env-file $HERE/images.env -f $HERE/compose.yaml"
step=${1:-all}

random() { head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c "$1"; }
write_secret() { # name content ; files are 0644 inside the 0700 directory (read by the containers' own users)
  [ -f "$SECRETS/$1" ] && return 0
  printf '%s' "$2" > "$SECRETS/$1.new" && chmod 644 "$SECRETS/$1.new" && mv "$SECRETS/$1.new" "$SECRETS/$1"
}

secrets() {
  mkdir -p "$SECRETS" && chmod 700 "$SECRETS"
  [ -f "$HERE/images.env" ] || { echo "images.env is missing: run images.sh on a SIGNED release manifest first" >&2; exit 2; }
  if [ ! -f "$SECRETS/postgres.env" ]; then
    PW=$(random 32)
    write_secret postgres.env "POSTGRES_USER=postgres
POSTGRES_PASSWORD=$PW
ANVILKIT_DEV_PASSWORD=$PW
"
    write_secret temporal.env "SQL_USER=temporal
SQL_PASSWORD=$PW
POSTGRES_USER=temporal
POSTGRES_PWD=$PW
"
  fi
  PW=$(sed -n 's/^ANVILKIT_DEV_PASSWORD=//p' "$SECRETS/postgres.env")
  for role in control_app control_migrator knowledge_app knowledge_migrator knowledge_relay knowledge_forwarder knowledge_store knowledge_store_migrator knowledge_cleanup mcp_app mcp_migrator mcp_relay; do
    db=anvilkit_${role%%_*}
    write_secret "$(echo "$role" | tr '_' '-')-dsn" "postgres://anvilkit_${role}:${PW}@postgres:5432/${db}?sslmode=disable"
  done
  if [ ! -f "$SECRETS/minio.env" ]; then
    write_secret minio.env "MINIO_ROOT_USER=anvilkit-artifacts-$(random 20)
MINIO_ROOT_PASSWORD=$(random 32)
"
  fi
  write_secret artifacts-access-key-id "$(sed -n 's/^MINIO_ROOT_USER=//p' "$SECRETS/minio.env")"
  write_secret artifacts-secret-access-key "$(sed -n 's/^MINIO_ROOT_PASSWORD=//p' "$SECRETS/minio.env")"
  [ -f "$SECRETS/minio-inventory.env" ] || write_secret minio-inventory.env "ANVILKIT_INVENTORY_ACCESS_KEY_ID=anvilkit-inventory-$(random 20)
ANVILKIT_INVENTORY_SECRET_ACCESS_KEY=$(random 32)
"
  write_secret inventory-access-key-id "$(sed -n 's/^ANVILKIT_INVENTORY_ACCESS_KEY_ID=//p' "$SECRETS/minio-inventory.env")"
  write_secret inventory-secret-access-key "$(sed -n 's/^ANVILKIT_INVENTORY_SECRET_ACCESS_KEY=//p' "$SECRETS/minio-inventory.env")"
  [ -f "$SECRETS/minio-model-proxy.env" ] || write_secret minio-model-proxy.env "ANVILKIT_MODEL_PROXY_STORE_ACCESS_KEY_ID=anvilkit-model-proxy-$(random 20)
ANVILKIT_MODEL_PROXY_STORE_SECRET_ACCESS_KEY=$(random 32)
"
  write_secret model-proxy-store-access-key-id "$(sed -n 's/^ANVILKIT_MODEL_PROXY_STORE_ACCESS_KEY_ID=//p' "$SECRETS/minio-model-proxy.env")"
  write_secret model-proxy-store-secret-access-key "$(sed -n 's/^ANVILKIT_MODEL_PROXY_STORE_SECRET_ACCESS_KEY=//p' "$SECRETS/minio-model-proxy.env")"
  [ -f "$SECRETS/minio-knowledge.env" ] || write_secret minio-knowledge.env "ANVILKIT_KNOWLEDGE_OBJECTS_ACCESS_KEY_ID=anvilkit-knowledge-$(random 20 | tr 'A-Z' 'a-z')
ANVILKIT_KNOWLEDGE_OBJECTS_SECRET_ACCESS_KEY=$(random 32)
"
  [ -f "$SECRETS/minio-removals.env" ] || write_secret minio-removals.env "ANVILKIT_KNOWLEDGE_REMOVALS_ACCESS_KEY_ID=anvilkit-removals-$(random 20 | tr 'A-Z' 'a-z')
ANVILKIT_KNOWLEDGE_REMOVALS_SECRET_ACCESS_KEY=$(random 32)
"
  [ -f "$SECRETS/qdrant.env" ] || write_secret qdrant.env "QDRANT__SERVICE__API_KEY=$(random 32)
"
  write_secret qdrant-api-key "$(sed -n 's/^QDRANT__SERVICE__API_KEY=//p' "$SECRETS/qdrant.env")"
  write_secret queue-url "redis://valkey-queue:6379"
  if [ ! -f "$SECRETS/api-principals.json" ]; then
    # DEVELOPMENT_ONLY bearer principals of the API (auth.mode fixture): one
    # token per tenant; the token values stay in this file only.
    write_secret api-principals.json "{
  \"$(random 32)\": {\"tenantId\": \"tenant_a\", \"projectId\": \"proj_a\", \"actorId\": \"user_a\", \"roles\": [\"author\"]},
  \"$(random 32)\": {\"tenantId\": \"tenant_b\", \"projectId\": \"proj_b\", \"actorId\": \"user_b\", \"roles\": [\"author\"]}
}
"
  fi
  # The Workflow's launcher identity: no cluster exists here (see README.md,
  # blocker B-1), so the kubeconfig names an unreachable loopback API server.
  write_secret launcher.kubeconfig "apiVersion: v1
kind: Config
clusters:
  - name: none
    cluster:
      server: https://127.0.0.1:1
users:
  - name: none
    user:
      token: no-cluster
contexts:
  - name: none
    context: {cluster: none, user: none, namespace: anvilkit-components}
current-context: none
"
  echo "secrets present under $SECRETS ($(ls "$SECRETS" | wc -l) files)"
}

foundation() {
  sh "$HERE/pki.sh" issue
  $COMPOSE pull --quiet
  $COMPOSE up -d --wait --wait-timeout 300 postgres temporal minio nats valkey-queue valkey-cache qdrant oidc
}

setup() {
  $COMPOSE run --rm --no-deps temporal-namespace
  $COMPOSE run --rm --no-deps minio-setup
  $COMPOSE run --rm --no-deps nats-setup
}

migrate() {
  # Roles created by postgres-init.sh exist on a fresh data directory; an
  # existing database gets the P14/P17 identities here, idempotently.
  PW=$(sed -n 's/^ANVILKIT_DEV_PASSWORD=//p' "$SECRETS/postgres.env")
  for role in anvilkit_knowledge_relay anvilkit_mcp_relay anvilkit_knowledge_forwarder anvilkit_knowledge_store_migrator anvilkit_knowledge_store anvilkit_knowledge_cleanup; do
    $COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$role'" | grep -qx 1 \
      || $COMPOSE exec -T -e PW="$PW" postgres sh -c "psql -v ON_ERROR_STOP=1 -U postgres -d postgres -c \"CREATE ROLE $role LOGIN PASSWORD '\$PW'\"" >/dev/null
  done
  $COMPOSE run --rm --no-deps control-migration
  $COMPOSE run --rm --no-deps knowledge-migration
  $COMPOSE run --rm --no-deps mcp-migration
  $COMPOSE run --rm --no-deps knowledge-store-migration
}

services() {
  $COMPOSE up -d --wait --wait-timeout 420 anvilkit-agent-control anvilkit-agent-api anvilkit-agent-workflow anvilkit-agent-model-proxy \
    anvilkit-agent-inference anvilkit-agent-knowledge knowledge-forwarder knowledge-relay anvilkit-agent-mcp mcp-relay anvilkit-agent-background-worker
}

status() { $COMPOSE ps --format 'table {{.Name}}\t{{.Image}}\t{{.Status}}'; }

case "$step" in
  all) secrets; foundation; setup; migrate; services; status ;;
  secrets) secrets ;;
  foundation) secrets; foundation ;;
  setup) setup ;;
  migrate) migrate ;;
  services) services ;;
  status) status ;;
  *) echo "usage: $0 [all|secrets|foundation|setup|migrate|services|status]" >&2; exit 2 ;;
esac
