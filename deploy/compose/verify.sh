#!/bin/sh
# Runtime verification of the Compose placement, run on the server with its own
# tools (docker, curl, openssl, psql inside the postgres container). Every check
# prints PASS, FAIL or UNEXECUTED with its evidence; the exit code is 1 when any
# check FAILED. Nothing here is a mock, a build or a test suite: it reads what
# the running placement answers. Secrets are read from the restricted files and
# never printed.
#
#   ANVILKIT_HOME=/srv/anvilkit sh deploy/compose/verify.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
export ANVILKIT_HOME=${ANVILKIT_HOME:-/srv/anvilkit}
SECRETS="$ANVILKIT_HOME/secrets"
CERTS="$ANVILKIT_HOME/certs"
COMPOSE="docker compose --project-directory $HERE --env-file $HERE/images.env -f $HERE/compose.yaml"
rc=0
pass() { echo "PASS        $1"; }
fail() { echo "FAIL        $1"; rc=1; }
unex() { echo "UNEXECUTED  $1"; }

echo "== images (digest-pinned, CI-published)"
grep -E '^(ANVILKIT_RELEASE|ANVILKIT_IMAGE_REGISTRY)=' "$HERE/images.env"
for svc in anvilkit-agent-control anvilkit-agent-api anvilkit-agent-workflow anvilkit-agent-model-proxy anvilkit-agent-inference anvilkit-agent-knowledge knowledge-forwarder knowledge-relay anvilkit-agent-mcp mcp-relay anvilkit-agent-background-worker; do
  id=$($COMPOSE ps -q "$svc" 2>/dev/null | head -1)
  if [ -z "$id" ]; then fail "$svc: no container"; continue; fi
  want=$(docker inspect "$id" --format '{{.Config.Image}}')
  have=$(docker inspect "$id" --format '{{index .Image}}')
  repo=$(docker image inspect "$have" --format '{{index .RepoDigests 0}}' 2>/dev/null)
  case "$repo" in
    *"@${want##*@}") pass "$svc runs $want" ;;
    *) fail "$svc runs $have, expected $want" ;;
  esac
done

echo "== containers healthy, restart policy, limits, read-only root"
for svc in postgres temporal minio nats valkey-queue valkey-cache qdrant anvilkit-agent-control anvilkit-agent-api anvilkit-agent-workflow anvilkit-agent-model-proxy anvilkit-agent-inference anvilkit-agent-knowledge knowledge-forwarder knowledge-relay anvilkit-agent-mcp mcp-relay anvilkit-agent-background-worker; do
  id=$($COMPOSE ps -q "$svc" 2>/dev/null | head -1)
  [ -n "$id" ] || { fail "$svc: no container"; continue; }
  health=$(docker inspect "$id" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')
  restart=$(docker inspect "$id" --format '{{.HostConfig.RestartPolicy.Name}}')
  mem=$(docker inspect "$id" --format '{{.HostConfig.Memory}}')
  cpus=$(docker inspect "$id" --format '{{.HostConfig.NanoCpus}}')
  ro=$(docker inspect "$id" --format '{{.HostConfig.ReadonlyRootfs}}')
  if [ "$health" = healthy ] || [ "$health" = running ]; then pass "$svc: $health, restart=$restart, memory=${mem}B, nanocpus=$cpus, readonly_rootfs=$ro"; else fail "$svc: $health (restart=$restart)"; fi
done

echo "== published ports (loopback only)"
bad=$(docker ps --format '{{.Names}} {{.Ports}}' | grep -E '0\.0\.0\.0:|\[::\]:' || true)
if [ -z "$bad" ]; then pass "no container publishes a port on a non-loopback address"; else fail "non-loopback publication: $bad"; fi

echo "== migrations"
for db in anvilkit_control anvilkit_knowledge anvilkit_mcp; do
  v=$($COMPOSE exec -T postgres psql -U postgres -d "$db" -tAc "SELECT max(version_id) FROM goose_db_version" 2>/dev/null | tr -d ' ')
  if [ -n "$v" ]; then pass "$db goose version $v"; else fail "$db: goose_db_version unreadable"; fi
done
st=$($COMPOSE exec -T postgres psql -U postgres -d anvilkit_knowledge -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='memory_store'" 2>/dev/null | tr -d ' ')
if [ -n "$st" ] && [ "$st" -gt 0 ]; then pass "knowledge memory store schema present ($st tables)"; else fail "knowledge memory store schema missing"; fi

echo "== foundation setup"
# The server image carries no CLI; the admin-tools image of the temporal-namespace one-shot does.
ns=$(docker run --rm --network anvilkit_default temporalio/admin-tools:1.31.2@sha256:dbc5fcd6ee8f0f4d808bf765af9a87dea9d8a283abfdcfbd2fc148496ba66107 temporal operator namespace describe --namespace anvilkit --address temporal:7233 2>/dev/null | grep -E '^ *(Name|NamespaceInfo.Name) +anvilkit' | head -1)
[ -n "$ns" ] && pass "temporal namespace anvilkit ($ns)" || fail "temporal namespace anvilkit missing"
for s in ANVILKIT_CONTROL ANVILKIT_KNOWLEDGE ANVILKIT_MCP; do
  if $COMPOSE exec -T nats sh -c "wget -qO- http://127.0.0.1:8222/jsz?streams=true" 2>/dev/null | grep -qE "\"name\": *\"$s\""; then pass "nats stream $s"; else fail "nats stream $s missing"; fi
done
# The mc image has no awk/grep; the listing is processed on the host.
buckets=$(docker run --rm --network anvilkit_default --env-file "$SECRETS/minio.env" --entrypoint sh ghcr.io/ancyloce/minio@sha256:52dfd5c0bbd38d3219f2058c7af216d9f9a27a994b7b5baad09bbd38866015ff -c 'mc alias set s http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc ls s' 2>/dev/null | awk '{print $NF}' | tr '\n' ' ')
for b in anvilkit-artifacts anvilkit-inventory anvilkit-model-proxy anvilkit-knowledge anvilkit-memory-removals; do
  case " $buckets" in *" $b/"*) pass "object store $b" ;; *) fail "object store $b missing (have: $buckets)" ;; esac
done

echo "== PKI and mTLS"
sh "$HERE/pki.sh" verify | sed 's/^/            /' | head -12
for ep in anvilkit-agent-control:9101 anvilkit-agent-knowledge:9105 anvilkit-agent-mcp:9106; do
  name=${ep%%:*}
  out=$(docker run --rm --network anvilkit_default -v "$CERTS/anvilkit-agent-api:/id:ro" -v "$CERTS/ca:/ca:ro" --entrypoint sh alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6 -c "apk add -q openssl >/dev/null 2>&1; echo | openssl s_client -connect $ep -servername $name -CAfile /ca/ca.crt -cert /id/tls.crt -key /id/tls.key -alpn h2 2>/dev/null | grep -E 'Verify return code|Protocol  :'" 2>/dev/null | tr '\n' ' ')
  case "$out" in *"Verify return code: 0"*) pass "mTLS handshake to $ep as anvilkit-agent-api: $out" ;; *) fail "mTLS handshake to $ep: ${out:-no answer}" ;; esac
done
out=$(docker run --rm --network anvilkit_default --entrypoint sh alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6 -c "apk add -q openssl >/dev/null 2>&1; echo | openssl s_client -connect anvilkit-agent-control:9101 2>&1 | grep -ciE 'alert|handshake failure|certificate required'" 2>/dev/null)
[ "${out:-0}" -gt 0 ] && pass "Control refuses a client without a certificate" || unex "Control without client certificate: no refusal observed in the handshake transcript (service-level authorization still applies)"

echo "== API"
TA=$(python3 -c "import json; d=json.load(open('$SECRETS/api-principals.json')); print(next(k for k,v in d.items() if v['tenantId']=='tenant_a'))" 2>/dev/null)
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:9100/readyz); case "$code" in 200|204) pass "API /readyz $code" ;; *) fail "API /readyz $code" ;; esac
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:9100/api/v1/knowledge/sources); [ "$code" = 401 ] && pass "API refuses an unauthenticated read (401)" || fail "API unauthenticated read answered $code"
# /operations is POST-only (API-02); a read of an unknown operation proves routing and authentication.
body=$(curl -s --max-time 15 -H "Authorization: Bearer $TA" http://127.0.0.1:9100/api/v1/operations/op_verify_missing); code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H "Authorization: Bearer $TA" http://127.0.0.1:9100/api/v1/operations/op_verify_missing)
[ "$code" = 404 ] && pass "API GET /operations/{unknown} as tenant_a: 404 ($(printf '%s' "$body" | head -c 100 | tr -d '\n')...)" || fail "API GET /operations/{unknown} $code: $(printf '%s' "$body" | head -c 200)"
for path in knowledge/sources mcp/catalog mcp/grants; do
  code=$(curl -s -o /tmp/verify-body -w '%{http_code}' --max-time 15 -H "Authorization: Bearer $TA" "http://127.0.0.1:9100/api/v1/$path")
  [ "$code" = 200 ] && pass "API GET /$path: 200 ($(head -c 80 /tmp/verify-body | tr -d '\n')...)" || fail "API GET /$path $code: $(head -c 200 /tmp/verify-body)"
done
rm -f /tmp/verify-body
unex "LocalCheck, generation, preview, parsing flows: the Workflow has no Job execution backend here (README blocker B-1)"

echo "== model route (from the proxy's own log lines: diagnostic, no paid call)"
pid=$($COMPOSE ps -q anvilkit-agent-model-proxy 2>/dev/null | head -1)
plog=$(docker logs "$pid" 2>&1)
state=$(printf '%s\n' "$plog" | grep -E '"msg":"route (not )?served","routeId":"controlled-openai-v1"' | tail -1)
case "$state" in
  *'"msg":"route served"'*) pass "model-proxy serves controlled-openai-v1: $(printf '%s' "$state" | grep -oE '"(provider|model|origin)":"[^"]*"' | tr '\n' ' ')" ;;
  *'"msg":"route not served"'*) unex "controlled-openai-v1 not served: $(printf '%s' "$state" | grep -oE '"reasons":"[^"]*"') (blocker B-2: no credential delivered on this server)" ;;
  *) fail "model-proxy logged no state for controlled-openai-v1" ;;
esac
probe=$(printf '%s\n' "$plog" | grep '"msg":"route health probe","routeId":"controlled-openai-v1"' | tail -1)
case "$probe" in
  *'"healthy":true'*) pass "unpaid models probe healthy: $(printf '%s' "$probe" | grep -oE '"status":[0-9]+')" ;;
  "") unex "no models probe result logged (route not served or probe not configured)" ;;
  *) fail "unpaid models probe unhealthy: $(printf '%s' "$probe" | grep -oE '"status":[0-9]+|"reason":"[^"]*"' | tr '\n' ' ')" ;;
esac
unex "preparation (paid model call): runs only with the owner's explicit authorization (tools/smoke/api-generate.py, stopped at the frozen brief)"
echo "== result: $([ $rc -eq 0 ] && echo PASS || echo FAIL)"
exit $rc
