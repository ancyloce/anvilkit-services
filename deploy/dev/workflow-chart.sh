#!/bin/sh
# Installs (or removes) the Workflow worker into the kind cluster of the
# development foundation from the Workflow repository alone: the image is
# built with services/agent/workflow as its sole build context, loaded into
# the kind node and the Workflow repository's chart is installed with the
# parent-owned development values (deploy/dev/values/anvilkit-agent-workflow.yaml).
# The chart creates the launcher identity (ServiceAccount, Role and
# RoleBinding in anvilkit-components); no kubeconfig or token is supplied.
#
#   sh deploy/dev/workflow-chart.sh install     # build, load, helm upgrade --install, wait
#   sh deploy/dev/workflow-chart.sh uninstall
#
# The worker talks to the Control release of deploy/dev/control-chart.sh
# (install it first) and to the foundation's Temporal by its kind-network
# address. The in-cluster replicas poll the same Temporal namespace as the
# host-side workers the integration scenarios start: uninstall this release
# before running those. ANVILKIT_DEV_GOPROXY / ANVILKIT_DEV_GONOSUMDB, when
# set, are passed to the image build. Requires docker, kind, kubectl, helm
# and a sourced .local/dev/env.sh.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SVC="$ROOT/services/agent/workflow"
CTX=kind-anvilkit-dev
NS=anvilkit-apps
IMAGE=anvilkit-agent-workflow:dev
ACTION=${1:-install}
export PATH="$PATH:$(go env GOPATH)/bin:$ROOT/.local/bin"
# The cluster administrator's kubeconfig (kind's context), not the launcher
# identity that .local/dev/env.sh exports as KUBECONFIG for the services.
export KUBECONFIG=${ANVILKIT_DEV_ADMIN_KUBECONFIG:-$HOME/.kube/config}

case "$ACTION" in
  install)
    [ -n "${ANVILKIT_DEV_TEMPORAL_ADDRESS_CLUSTER:-}" ] || { echo "ANVILKIT_DEV_TEMPORAL_ADDRESS_CLUSTER is not set: run deploy/dev/up.sh and source .local/dev/env.sh" >&2; exit 2; }
    docker build --network=host \
      ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} \
      ${ANVILKIT_DEV_GONOSUMDB:+--build-arg GONOSUMDB="$ANVILKIT_DEV_GONOSUMDB"} \
      -t "$IMAGE" "$SVC"
    kind load docker-image "$IMAGE" --name anvilkit-dev
    IMAGE_ID=$(docker image inspect "$IMAGE" --format '{{.Id}}')
    kubectl --context "$CTX" get namespace "$NS" >/dev/null 2>&1 || kubectl --context "$CTX" create namespace "$NS" >/dev/null
    helm --kube-context "$CTX" upgrade --install anvilkit-agent-workflow "$SVC/deploy/chart" -n "$NS" \
      -f "$ROOT/deploy/dev/values/anvilkit-agent-workflow.yaml" \
      --set "temporal.address=${ANVILKIT_DEV_TEMPORAL_ADDRESS_CLUSTER}" \
      --set "buildId=dev-${IMAGE_ID#sha256:}" \
      --set-string "podAnnotations.anvilkit\.io/image-id=${IMAGE_ID}" --wait --timeout 180s
    kubectl --context "$CTX" -n "$NS" get deploy,pod -l app.kubernetes.io/name=anvilkit-agent-workflow
    kubectl --context "$CTX" -n anvilkit-components get role,rolebinding -l app.kubernetes.io/name=anvilkit-agent-workflow
    ;;
  uninstall)
    helm --kube-context "$CTX" uninstall anvilkit-agent-workflow -n "$NS" || true
    ;;
  *)
    echo "usage: $0 [install|uninstall]" >&2
    exit 2
    ;;
esac
