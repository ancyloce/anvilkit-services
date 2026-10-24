#!/bin/sh
# Stops the Compose placement. Containers are removed; the data volumes
# (postgres, minio, nats, valkey-queue, qdrant) stay unless --volumes is given,
# and the secrets and certificates under ${ANVILKIT_HOME} always stay.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
export ANVILKIT_HOME=${ANVILKIT_HOME:-/srv/anvilkit}
case "${1:-}" in
  "") docker compose --project-directory "$HERE" --env-file "$HERE/images.env" -f "$HERE/compose.yaml" down --remove-orphans ;;
  --volumes) docker compose --project-directory "$HERE" --env-file "$HERE/images.env" -f "$HERE/compose.yaml" down --remove-orphans --volumes ;;
  *) echo "usage: $0 [--volumes]" >&2; exit 2 ;;
esac
