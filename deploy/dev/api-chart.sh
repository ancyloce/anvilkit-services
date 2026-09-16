#!/bin/sh
# Installs (or removes) the API into the kind cluster of the development
# foundation from the API repository alone: the image is built with
# services/agent/api as its sole build context, loaded into the kind node,
# the principals Secret is created from .local/dev/api-principals.json (never
# in Git) and the API repository's chart is installed with the parent-owned
# development values (deploy/dev/values/anvilkit-agent-api.yaml).
#
#   sh deploy/dev/api-chart.sh install     # build, load, secret, helm upgrade --install, wait
#   sh deploy/dev/api-chart.sh uninstall
#
# Inside kind the API reaches the host-side Control through the Docker network
# gateway (control.address=<gateway>:9101), so Control must listen on a
# non-loopback address for this topology: ANVILKIT_CONTROL_LISTEN=0.0.0.0:9101.
# With the Control release of deploy/dev/control-chart.sh installed, set
# ANVILKIT_DEV_API_CONTROL_ADDRESS=anvilkit-agent-control.anvilkit-apps.svc.cluster.local:9101
# to point the API at it instead.
# ANVILKIT_DEV_GOPROXY / ANVILKIT_DEV_GONOSUMDB, when set, are passed to the
# image build for a private module proxy; by default the Dockerfile resolves the
# published contracts module through the public proxy. Requires docker, kind,
# kubectl, helm.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
API="$ROOT/services/agent/api"
CTX=kind-anvilkit-dev
NS=anvilkit-apps
IMAGE=anvilkit-agent-api:dev
ACTION=${1:-install}
export PATH="$PATH:$(go env GOPATH)/bin:$ROOT/.local/bin"
# The cluster administrator's kubeconfig (kind's context), not the launcher
# identity that .local/dev/env.sh exports as KUBECONFIG for the services.
export KUBECONFIG=${ANVILKIT_DEV_ADMIN_KUBECONFIG:-$HOME/.kube/config}

case "$ACTION" in
  install)
    test -f "$ROOT/.local/dev/api-principals.json" || { echo "run deploy/dev/up.sh first (.local/dev/api-principals.json missing)" >&2; exit 2; }
    docker build --network=host \
      ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} \
      ${ANVILKIT_DEV_GONOSUMDB:+--build-arg GONOSUMDB="$ANVILKIT_DEV_GONOSUMDB"} \
      -t "$IMAGE" "$API"
    kind load docker-image "$IMAGE" --name anvilkit-dev
    # The image is addressed by tag inside kind, so a rebuilt image alone changes
    # nothing in the Deployment's template; its ID as a Pod annotation rolls the
    # Pods exactly when the image content changed and records what runs.
    IMAGE_ID=$(docker image inspect "$IMAGE" --format '{{.Id}}')
    GATEWAY=$(docker network inspect kind --format '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}} {{end}}{{end}}' | tr ' ' '\n' | grep -m1 '^[0-9]')
    CONTROL_ADDRESS=${ANVILKIT_DEV_API_CONTROL_ADDRESS:-${GATEWAY}:9101}
    kubectl --context "$CTX" get namespace "$NS" >/dev/null 2>&1 || kubectl --context "$CTX" create namespace "$NS" >/dev/null
    kubectl --context "$CTX" -n "$NS" create secret generic anvilkit-agent-api-principals \
      --from-file=principals.json="$ROOT/.local/dev/api-principals.json" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
    helm --kube-context "$CTX" upgrade --install anvilkit-agent-api "$API/deploy/chart" -n "$NS" \
      -f "$ROOT/deploy/dev/values/anvilkit-agent-api.yaml" --set "control.address=${CONTROL_ADDRESS}" \
      --set-string "podAnnotations.anvilkit\.io/image-id=${IMAGE_ID}" --wait --timeout 120s
    kubectl --context "$CTX" -n "$NS" get deploy,pod,svc -l app.kubernetes.io/name=anvilkit-agent-api
    echo "API installed; reach it with: kubectl --context $CTX -n $NS port-forward svc/anvilkit-agent-api 9100:80"
    ;;
  uninstall)
    helm --kube-context "$CTX" uninstall anvilkit-agent-api -n "$NS" || true
    kubectl --context "$CTX" -n "$NS" delete secret anvilkit-agent-api-principals --ignore-not-found
    ;;
  *)
    echo "usage: $0 [install|uninstall]" >&2
    exit 2
    ;;
esac
