#!/bin/sh
# Builds the P09 harness images and the P10 validator image and publishes
# them to the development registry of the kind cluster (deploy/dev/up.sh
# starts it), by digest:
#
#   sh deploy/dev/images.sh          # build all, push, print the digests
#   sh deploy/dev/images.sh --harness # P09 supervisor and sidecar only
#
# anvilkit-codegen builds from jobs/codegen alone (its own Dockerfile);
# anvilkit-job-access-sidecar builds from its own Dockerfile against its
# published, versioned contracts dependency; anvilkit-validator builds from
# jobs/validator with this checkout's contracts sources as the named build
# context "contracts" (the schemas the Job validates its outputs against;
# DEVELOPMENT_ONLY until a contracts release is consumed). The digests
# printed here are what contracts/jobs/profiles.json pins for
# codegen-fixed-v1, harness-wiring-dev-v1 and validator-fixed-dev-v1 and what
# deploy/policies/kyverno admits; a rebuilt image is a new digest and needs a
# profile revision.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
REGISTRY=${ANVILKIT_DEV_REGISTRY:-localhost:5001}
case "${1:-}" in
  ""|--harness) ;;
  *) echo "usage: $0 [--harness]" >&2; exit 2 ;;
esac
IMAGES="anvilkit-codegen anvilkit-job-access-sidecar"
docker build --network=host ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} -t "$REGISTRY/anvilkit-codegen:dev" "$ROOT/jobs/codegen"
docker build --network=host ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} -t "$REGISTRY/anvilkit-job-access-sidecar:dev" "$ROOT/jobs/shared/access-sidecar"
if [ "${1:-}" != --harness ]; then
  docker build --network=host --build-context contracts="$ROOT/contracts" -t "$REGISTRY/anvilkit-validator:dev" "$ROOT/jobs/validator"
  IMAGES="$IMAGES anvilkit-validator"
fi
for image in $IMAGES; do
  docker push "$REGISTRY/$image:dev" >/dev/null
  printf '%s %s\n' "$image" "$(docker image inspect "$REGISTRY/$image:dev" --format '{{index .RepoDigests 0}}' | sed 's/.*@//')"
done
