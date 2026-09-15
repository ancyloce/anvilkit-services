#!/bin/sh
# Tears down only the anvilkit-dev resources this profile created. Pass
# --volumes to also delete the PostgreSQL data volume; .local/dev/ is kept.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
export PATH="$PATH:$(go env GOPATH)/bin"
if [ "${1:-}" = "--volumes" ]; then
  docker compose -f "$ROOT/deploy/dev/compose.yaml" down --remove-orphans --volumes
else
  docker compose -f "$ROOT/deploy/dev/compose.yaml" down --remove-orphans
fi
if kind get clusters 2>/dev/null | grep -qx anvilkit-dev; then
  kind delete cluster --name anvilkit-dev
fi
