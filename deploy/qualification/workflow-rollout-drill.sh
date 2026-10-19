#!/bin/sh
# P1.7 Workflow rollout drill (DEVELOPMENT_ONLY): deploy/qualification/workflow-rollout.py
# against the development foundation's kind cluster and Temporal, with the Workflow image
# built from services/agent/workflow and real LocalCheck runs. Isolated: its own namespace
# (anvilkit-rollout-drill), task queues and worker deployment (anvilkit-rollout-drill-<tag>),
# no RBAC and no certificate outside that namespace, so the host-side workers of the
# integration scenarios never see its queues. Control is deliberately unreachable: each
# LocalCheck run stays open on the build it started on while its first Control call
# (OpenAttempt) retries - execution.control_retry_max_attempts 12 in the drill's values,
# about 4 minutes - then closes failed without reaching Control's database.
#   1. build A, the first versioned install: rollout with no predecessor, A becomes current
#   2. five LocalCheck runs, pinned to A
#   3. rollout to build B with a 60 s drain bound: B current, A DRAINING with its runs
#      still open and kept (exit 3)
#   4. three LocalCheck runs, pinned to B
#   5. once A's runs closed: retire removes A only after Temporal reports it DRAINED (exit 0)
#   6. rollout to build C whose image is not on the node: ABORTED, B stays current (exit 1)
#   7. retire removes C (never registered) and keeps B (exit 0)
# Afterwards every run's build and close status, its history and the worker logs searched
# for non-determinism. The record is written under outputs/.
#
#   sh deploy/qualification/workflow-rollout-drill.sh [OUT_DIR]
#
# Needs the foundation up (deploy/dev/up.sh), docker, kind, helm and kubectl. Builds
# anvilkit-agent-workflow:rollout-drill, loads it into kind and deletes its namespace and
# terminates its leftover runs at the end.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${1:-"$ROOT/outputs/workflow-rollout-drill-$(date -u +%Y%m%dT%H%M%SZ)"}
mkdir -p "$OUT"
# shellcheck disable=SC1091
. "$ROOT/.local/dev/env.sh"
export PATH="$ROOT/.local/bin:$PATH:$(go env GOPATH)/bin"
# The cluster administrator's kubeconfig, not the launcher identity env.sh exports.
export KUBECONFIG=${ANVILKIT_DEV_ADMIN_KUBECONFIG:-$HOME/.kube/config}
CTX=kind-anvilkit-dev
NS=anvilkit-rollout-drill
TAG=$(date -u +%m%d%H%M%S)
QUEUE=anvilkit-rollout-drill-$TAG
DEPLOYMENT=anvilkit-rollout-drill-$TAG
IMAGE=anvilkit-agent-workflow:rollout-drill
PY="$ROOT/.local/verification-venv/bin/python"
TOOL="$ROOT/deploy/qualification/workflow-rollout.py"
T="docker run --rm --network host --entrypoint temporal temporalio/admin-tools:1.31.2@sha256:dbc5fcd6ee8f0f4d808bf765af9a87dea9d8a283abfdcfbd2fc148496ba66107"
TA="--address $ANVILKIT_DEV_TEMPORAL_ADDRESS --namespace anvilkit"
A=drill-a-$TAG B=drill-b-$TAG C=drill-c-$TAG
key() { printf %s "$1" | sha256sum | cut -c1-10; }
count() { $T workflow count $TA --query "$1" -o json | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("count", 0))'; }
pinned() { echo "TemporalWorkerDeploymentVersion = \"$DEPLOYMENT:$1\" AND ExecutionStatus = \"Running\""; }
wait_count() { # query want seconds
  n=0
  while [ "$(count "$1")" != "$2" ]; do
    n=$((n + 10)); [ "$n" -le "$3" ] || return 1; sleep 10
  done
}
cleanup() {
  $T workflow terminate $TA --query "TaskQueue = \"$QUEUE\" AND ExecutionStatus = \"Running\"" --reason "rollout drill cleanup" --yes >/dev/null 2>&1 || true
  helm --kube-context "$CTX" uninstall anvilkit-agent-workflow -n "$NS" >/dev/null 2>&1 || true
  kubectl --context "$CTX" delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build --network=host \
  ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} \
  ${ANVILKIT_DEV_GONOSUMDB:+--build-arg GONOSUMDB="$ANVILKIT_DEV_GONOSUMDB"} \
  -t "$IMAGE" "$ROOT/services/agent/workflow" >"$OUT/image-build.log" 2>&1
kind load docker-image "$IMAGE" --name anvilkit-dev >/dev/null
kubectl --context "$CTX" get namespace "$NS" >/dev/null 2>&1 || kubectl --context "$CTX" create namespace "$NS" >/dev/null
cat >"$OUT/values.yaml" <<EOF
# The drill's environment values: one replica per build, development transports, no
# RBAC or certificate outside $NS, a Control address with no Service behind it.
replicaCount: 1
image: {repository: anvilkit-agent-workflow, tag: rollout-drill, pullPolicy: Never}
temporal: {address: "$ANVILKIT_DEV_TEMPORAL_ADDRESS_CLUSTER", tls: {mode: development}}
control: {address: "anvilkit-agent-control.$NS.svc.cluster.local:9101"}
launcher: {namespace: anvilkit-components, backend: kind-anvilkit-dev, imageRegistry: "localhost:5001", sidecarControlAddress: "anvilkit-agent-control.$NS.svc.cluster.local:9101"}
rbac: {create: false}
identity: {mode: development}
sidecar: {identityMode: development}
development: {enabled: true}
podDisruptionBudget: {enabled: false}
resources: {requests: {cpu: 50m, memory: 64Mi}, limits: {cpu: 1000m, memory: 256Mi}}
config:
  temporal:
    task_queue: $QUEUE
    control_task_queue: $QUEUE-control
    versioning: {enabled: true, deployment_name: $DEPLOYMENT}
  execution:
    control_retry_max_attempts: 12
EOF
results=""
step() { # label expected-exit command...
  label=$1 want=$2; shift 2
  set +e
  "$PY" "$TOOL" "$@" --namespace "$NS" --context "$CTX" --temporal-address "$ANVILKIT_DEV_TEMPORAL_ADDRESS" --temporal-cli "$T" \
    --poll 10 --out "$OUT/$label" >"$OUT/$label.log" 2>&1
  got=$?
  set -e
  results="$results
$label: exit $got (expected $want) $([ "$got" = "$want" ] && echo PASS || echo FAIL)"
  echo "$label: exit $got (expected $want)"
}
start() { $T workflow start $TA --type LocalCheckWorkflow --task-queue "$QUEUE" --workflow-id "$1" \
  --input "{\"operationId\":\"$1\",\"tenantId\":\"tenant-rollout-drill\"}" >/dev/null; }

step 1-rollout-a 0 rollout -f "$OUT/values.yaml" --set "buildId=$A"
for i in 1 2 3 4 5; do start "rollout-drill-$TAG-a$i"; done
wait_count "$(pinned "$A")" 5 120 || echo "the five runs did not pin to build A" >>"$OUT/errors.txt"
step 3-rollout-b 3 rollout -f "$OUT/values.yaml" --set "buildId=$B" --drain-timeout 60
# At the bound runs pinned to A were still open: the refusal is not only Temporal's grace period.
open_at_bound=$("$PY" -c 'import json,sys; r=json.load(open(sys.argv[1])); print(min([k["polls"][-1]["openPinnedRuns"] or 0 for k in r["kept"]] or [0]))' "$OUT/3-rollout-b/rollout.json")
results="$results
3-rollout-b: kept A with $open_at_bound open pinned runs at the bound $([ "$open_at_bound" -gt 0 ] && echo PASS || echo FAIL)"
for i in 1 2 3; do start "rollout-drill-$TAG-b$i"; done
wait_count "$(pinned "$B")" 3 120 || echo "the three runs did not pin to build B" >>"$OUT/errors.txt"
wait_count "$(pinned "$A")" 0 600 || echo "build A's runs did not close" >>"$OUT/errors.txt"
kubectl --context "$CTX" -n "$NS" logs -l "anvilkit.io/worker-version=$(key "$A")" --tail=-1 >"$OUT/worker-a.log" 2>&1 || true
step 5-retire-a 0 retire --drain-timeout 900
step 6-rollout-c 1 rollout -f "$OUT/values.yaml" --set "buildId=$C" --set image.tag=rollout-drill-absent --ready-timeout 60
step 7-retire-c 0 retire --drain-timeout 60
wait_count "$(pinned "$B")" 0 600 || echo "build B's runs did not close" >>"$OUT/errors.txt"
kubectl --context "$CTX" -n "$NS" logs -l "anvilkit.io/worker-version=$(key "$B")" --tail=-1 >"$OUT/worker-b.log" 2>&1 || true
kubectl --context "$CTX" -n "$NS" get deployments -o name >"$OUT/deployments-after.txt"

: >"$OUT/runs.txt"
nondeterministic=0
for id in $(for i in 1 2 3 4 5; do echo "rollout-drill-$TAG-a$i"; done; for i in 1 2 3; do echo "rollout-drill-$TAG-b$i"; done); do
  $T workflow describe $TA -w "$id" -o json | "$PY" -c 'import json,sys
d = json.load(sys.stdin)["workflowExecutionInfo"]
v = (d.get("versioningInfo") or {}).get("deploymentVersion") or {}
print(sys.argv[1], d.get("status"), v.get("buildId"))' "$id" >>"$OUT/runs.txt"
  n=$($T workflow show $TA -w "$id" -o json | grep -ci nondetermin || true)
  nondeterministic=$((nondeterministic + n))
done
logs=$(cat "$OUT/worker-a.log" "$OUT/worker-b.log" | grep -ci nondetermin || true)
expected_runs=$(grep -c "rollout-drill-$TAG-a. WORKFLOW_EXECUTION_STATUS_FAILED $A\$" "$OUT/runs.txt" || true)
expected_runs=$((expected_runs + $(grep -c "rollout-drill-$TAG-b. WORKFLOW_EXECUTION_STATUS_FAILED $B\$" "$OUT/runs.txt" || true)))
status=PASS
case "$results" in *FAIL*) status=FAIL ;; esac
[ -s "$OUT/errors.txt" ] 2>/dev/null && status=FAIL
[ "$expected_runs" = 8 ] && [ "$nondeterministic" = 0 ] && [ "$logs" = 0 ] || status=FAIL
grep -qx "deployment.apps/anvilkit-agent-workflow-$(key "$B")" "$OUT/deployments-after.txt" && [ "$(wc -l <"$OUT/deployments-after.txt")" = 1 ] || status=FAIL
{
  echo "workflow rollout drill: $status"
  echo "worker deployment: $DEPLOYMENT (builds A=$A B=$B C=$C), task queue $QUEUE, namespace $NS"
  echo "temporal: $ANVILKIT_DEV_TEMPORAL_ADDRESS (cluster $ANVILKIT_DEV_TEMPORAL_ADDRESS_CLUSTER)"
  echo "steps:$results"
  echo "runs closed on the build they started on: $expected_runs/8 (LocalCheck fails on the unreachable Control by design)"
  echo "non-determinism: $nondeterministic in run histories, $logs in worker logs"
  echo "deployments left: $(tr '\n' ' ' <"$OUT/deployments-after.txt")"
  [ -s "$OUT/errors.txt" ] 2>/dev/null && cat "$OUT/errors.txt"
  cat "$OUT/runs.txt"
} >"$OUT/record.txt"
cat "$OUT/record.txt"
if [ "$status" = PASS ]; then exit 0; fi
exit 1
