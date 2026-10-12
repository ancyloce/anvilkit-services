#!/bin/sh
# P1.7 upgrade drill (DEVELOPMENT_ONLY): Worker Deployment Versioning on the
# development foundation's Temporal. Build A of a fresh worker deployment
# holds ten in-flight runs pinned to it while build B, whose Workflow code
# is incompatible with A's histories, becomes the current version; the ten
# complete on A, new runs land on B, and no worker reports a
# non-determinism error. The record is written under outputs/.
#
#   sh deploy/qualification/upgrade-drill.sh [OUT_DIR]
#
# Needs the foundation up (deploy/dev/up.sh) and Go; touches nothing but a
# deployment and task queue of its own on the foundation's Temporal.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${1:-"$ROOT/outputs/upgrade-drill-$(date -u +%Y%m%dT%H%M%SZ)"}
mkdir -p "$OUT"
# shellcheck disable=SC1091
. "$ROOT/.local/dev/env.sh"
cd "$ROOT/services/agent/workflow"
set +e
GOWORK=off go test -tags integration -count=1 -v -run TestUpgradeDrill ./internal/bootstrap/ >"$OUT/drill.log" 2>&1
status=$?
set -e
if [ "$status" -eq 0 ]; then result=PASS; else result=FAIL; fi
{
  echo "upgrade drill: $result"
  echo "temporal: ${ANVILKIT_DEV_TEMPORAL_ADDRESS:-unset}"
  grep -E "P1.7 AC2|ran on" "$OUT/drill.log" || true
} >"$OUT/record.txt"
cat "$OUT/record.txt"
exit "$status"
