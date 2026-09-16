#!/bin/sh
# Installs (or removes) Control into the kind cluster of the development
# foundation from the Control repository alone: the image is built with
# services/agent/control as its sole build context, loaded into the kind node,
# the Secrets (application and migrator database URLs, the inventory and
# artifact store credentials) are created from .local/dev/ (never in Git) and
# the Control repository's chart is installed with the parent-owned
# development values (deploy/dev/values/anvilkit-agent-control.yaml). The
# chart's migration Job runs before the Pods roll.
#
#   sh deploy/dev/control-chart.sh install     # build, load, secrets, helm upgrade --install, wait
#   sh deploy/dev/control-chart.sh uninstall
#
# Inside kind Control reaches the foundation's PostgreSQL, Temporal and MinIO
# by their kind-network addresses (up.sh attaches them; .local/dev/env.sh
# exports them as ANVILKIT_DEV_*_CLUSTER). The two replicas share the
# anvilkit-inventory bucket (the S3 inventory backend), never a local
# directory. ANVILKIT_DEV_GOPROXY / ANVILKIT_DEV_GONOSUMDB, when set, are
# passed to the image build for a private module proxy. Requires docker,
# kind, kubectl, helm and a sourced .local/dev/env.sh.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SVC="$ROOT/services/agent/control"
CTX=kind-anvilkit-dev
NS=anvilkit-apps
IMAGE=anvilkit-agent-control:dev
ACTION=${1:-install}
export PATH="$PATH:$(go env GOPATH)/bin:$ROOT/.local/bin"
# The cluster administrator's kubeconfig (kind's context), not the launcher
# identity that .local/dev/env.sh exports as KUBECONFIG for the services.
export KUBECONFIG=${ANVILKIT_DEV_ADMIN_KUBECONFIG:-$HOME/.kube/config}

case "$ACTION" in
  install)
    for v in ANVILKIT_DEV_CONTROL_DSN_CLUSTER ANVILKIT_DEV_CONTROL_MIGRATOR_DSN_CLUSTER ANVILKIT_DEV_TEMPORAL_ADDRESS_CLUSTER \
             ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER ANVILKIT_CONTROL_ARTIFACTS_S3_ACCESS_KEY_ID ANVILKIT_CONTROL_ARTIFACTS_S3_SECRET_ACCESS_KEY \
             ANVILKIT_DEV_INVENTORY_BUCKET ANVILKIT_DEV_INVENTORY_ACCESS_KEY_ID ANVILKIT_DEV_INVENTORY_SECRET_ACCESS_KEY; do
      eval "val=\${$v:-}"
      [ -n "$val" ] || { echo "$v is not set: run deploy/dev/up.sh and source .local/dev/env.sh" >&2; exit 2; }
    done
    docker build --network=host \
      ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} \
      ${ANVILKIT_DEV_GONOSUMDB:+--build-arg GONOSUMDB="$ANVILKIT_DEV_GONOSUMDB"} \
      -t "$IMAGE" "$SVC"
    kind load docker-image "$IMAGE" --name anvilkit-dev
    # The image is addressed by tag inside kind, so a rebuilt image alone changes
    # nothing in the Deployment's template; its ID as a Pod annotation rolls the
    # Pods exactly when the image content changed and records what runs.
    IMAGE_ID=$(docker image inspect "$IMAGE" --format '{{.Id}}')
    kubectl --context "$CTX" get namespace "$NS" >/dev/null 2>&1 || kubectl --context "$CTX" create namespace "$NS" >/dev/null
    kubectl --context "$CTX" -n "$NS" create secret generic anvilkit-agent-control-database \
      --from-literal=url="$ANVILKIT_DEV_CONTROL_DSN_CLUSTER" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
    kubectl --context "$CTX" -n "$NS" create secret generic anvilkit-agent-control-migrator \
      --from-literal=url="$ANVILKIT_DEV_CONTROL_MIGRATOR_DSN_CLUSTER" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
    kubectl --context "$CTX" -n "$NS" create secret generic anvilkit-agent-control-inventory \
      --from-literal=accessKeyId="$ANVILKIT_DEV_INVENTORY_ACCESS_KEY_ID" --from-literal=secretAccessKey="$ANVILKIT_DEV_INVENTORY_SECRET_ACCESS_KEY" \
      --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
    kubectl --context "$CTX" -n "$NS" create secret generic anvilkit-agent-control-artifacts \
      --from-literal=accessKeyId="$ANVILKIT_CONTROL_ARTIFACTS_S3_ACCESS_KEY_ID" --from-literal=secretAccessKey="$ANVILKIT_CONTROL_ARTIFACTS_S3_SECRET_ACCESS_KEY" \
      --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
    helm --kube-context "$CTX" upgrade --install anvilkit-agent-control "$SVC/deploy/chart" -n "$NS" \
      -f "$ROOT/deploy/dev/values/anvilkit-agent-control.yaml" \
      --set "temporal.address=${ANVILKIT_DEV_TEMPORAL_ADDRESS_CLUSTER}" \
      --set "inventory.s3.endpoint=${ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER}" \
      --set "artifacts.s3.endpoint=${ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER}" \
      --set-string "podAnnotations.anvilkit\.io/image-id=${IMAGE_ID}" --wait --timeout 180s
    kubectl --context "$CTX" -n "$NS" get deploy,pod,svc,job -l app.kubernetes.io/name=anvilkit-agent-control
    echo "Control installed; reach it with: kubectl --context $CTX -n $NS port-forward svc/anvilkit-agent-control 9101:9101"
    ;;
  uninstall)
    helm --kube-context "$CTX" uninstall anvilkit-agent-control -n "$NS" || true
    kubectl --context "$CTX" -n "$NS" delete job -l app.kubernetes.io/name=anvilkit-agent-control --ignore-not-found >/dev/null
    for s in database migrator inventory artifacts; do
      kubectl --context "$CTX" -n "$NS" delete secret "anvilkit-agent-control-$s" --ignore-not-found
    done
    ;;
  *)
    echo "usage: $0 [install|uninstall]" >&2
    exit 2
    ;;
esac
