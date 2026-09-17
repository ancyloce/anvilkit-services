#!/bin/sh
# Installs (or removes) the Model Proxy into the kind cluster of the
# development foundation from the Model Proxy repository alone: the image is
# built with services/agent/model-proxy as its build context (the contract
# document it validates against comes from contracts/ as a named build
# context, as the image build names it), loaded into the kind node, the
# Secrets (the store credentials, the DEVELOPMENT_ONLY bearer principals)
# are created from .local/dev/ (never in Git) and the Model Proxy
# repository's chart is installed with the parent-owned development values
# (deploy/dev/values/anvilkit-agent-model-proxy.yaml).
#
#   sh deploy/dev/model-proxy-chart.sh install     # build, load, secrets, helm upgrade --install, wait
#   sh deploy/dev/model-proxy-chart.sh uninstall
#
# Inside kind the Proxy reaches the Control release of the same namespace
# (install Control first) and the foundation's MinIO by its kind-network
# address (up.sh attaches it; .local/dev/env.sh exports it as
# ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER) with the bucket and user of
# anvilkit-model-proxy. No route credential is created: the controlled route
# of the verification scenarios stays declared and unserved here. Requires
# docker, kind, kubectl, helm and a sourced .local/dev/env.sh.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SVC="$ROOT/services/agent/model-proxy"
CTX=kind-anvilkit-dev
NS=anvilkit-apps
IMAGE=anvilkit-agent-model-proxy:dev
ACTION=${1:-install}
export PATH="$PATH:$(go env GOPATH)/bin:$ROOT/.local/bin"
export KUBECONFIG=${ANVILKIT_DEV_ADMIN_KUBECONFIG:-$HOME/.kube/config}

case "$ACTION" in
  install)
    for v in ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER ANVILKIT_DEV_MODEL_PROXY_BUCKET ANVILKIT_DEV_MODEL_PROXY_ACCESS_KEY_ID \
             ANVILKIT_DEV_MODEL_PROXY_SECRET_ACCESS_KEY ANVILKIT_MODEL_PROXY_PRINCIPALS_FILE; do
      eval "val=\${$v:-}"
      [ -n "$val" ] || { echo "$v is not set: run deploy/dev/up.sh and source .local/dev/env.sh" >&2; exit 2; }
    done
    docker build --network=host --build-context "contracts=$ROOT/contracts" -t "$IMAGE" "$SVC"
    kind load docker-image "$IMAGE" --name anvilkit-dev
    IMAGE_ID=$(docker image inspect "$IMAGE" --format '{{.Id}}')
    kubectl --context "$CTX" get namespace "$NS" >/dev/null 2>&1 || kubectl --context "$CTX" create namespace "$NS" >/dev/null
    kubectl --context "$CTX" -n "$NS" create secret generic anvilkit-agent-model-proxy-store \
      --from-literal=accessKeyId="$ANVILKIT_DEV_MODEL_PROXY_ACCESS_KEY_ID" --from-literal=secretAccessKey="$ANVILKIT_DEV_MODEL_PROXY_SECRET_ACCESS_KEY" \
      --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
    kubectl --context "$CTX" -n "$NS" create secret generic anvilkit-agent-model-proxy-principals \
      --from-file=principals.json="$ANVILKIT_MODEL_PROXY_PRINCIPALS_FILE" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
    helm --kube-context "$CTX" upgrade --install anvilkit-agent-model-proxy "$SVC/deploy/chart" -n "$NS" \
      -f "$ROOT/deploy/dev/values/anvilkit-agent-model-proxy.yaml" \
      --set "store.s3.endpoint=${ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER}" \
      --set-string "podAnnotations.anvilkit\.io/image-id=${IMAGE_ID}" --wait --timeout 180s
    kubectl --context "$CTX" -n "$NS" get deploy,pod,svc -l app.kubernetes.io/name=anvilkit-agent-model-proxy
    echo "Model Proxy installed; reach it with: kubectl --context $CTX -n $NS port-forward svc/anvilkit-agent-model-proxy 9103:9103"
    ;;
  uninstall)
    helm --kube-context "$CTX" uninstall anvilkit-agent-model-proxy -n "$NS" || true
    for s in store principals; do
      kubectl --context "$CTX" -n "$NS" delete secret "anvilkit-agent-model-proxy-$s" --ignore-not-found
    done
    ;;
  *)
    echo "usage: $0 [install|uninstall]" >&2
    exit 2
    ;;
esac
