#!/bin/sh
# Builds the two P09 harness images and publishes them to the development
# registry of the kind cluster (deploy/dev/up.sh starts it), by digest:
#
#   sh deploy/dev/images.sh          # build, push, print the digests
#
# anvilkit-codegen builds from jobs/codegen alone (its own Dockerfile);
# anvilkit-job-access-sidecar builds from its repository's Dockerfile once a
# contracts release with ExecutionService.GetInstance is published, and until
# then from deploy/dev/docker/access-sidecar.dev.Dockerfile against this
# checkout's contracts (DEVELOPMENT_ONLY). The digests printed here are what
# contracts/jobs/profiles.json pins for codegen-fixed-v1 and
# harness-wiring-dev-v1 and what deploy/policies/kyverno admits; a rebuilt
# image is a new digest and needs a profile revision.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
REGISTRY=${ANVILKIT_DEV_REGISTRY:-localhost:5001}
docker build --network=host ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} -t "$REGISTRY/anvilkit-codegen:dev" "$ROOT/jobs/codegen"
cp "$ROOT/deploy/dev/docker/parent.dockerignore" "$ROOT/deploy/dev/docker/access-sidecar.dev.Dockerfile.dockerignore"
docker build --network=host ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} -f "$ROOT/deploy/dev/docker/access-sidecar.dev.Dockerfile" -t "$REGISTRY/anvilkit-job-access-sidecar:dev" "$ROOT"
docker push "$REGISTRY/anvilkit-codegen:dev" >/dev/null
docker push "$REGISTRY/anvilkit-job-access-sidecar:dev" >/dev/null
for image in anvilkit-codegen anvilkit-job-access-sidecar; do
  printf '%s %s\n' "$image" "$(docker image inspect "$REGISTRY/$image:dev" --format '{{index .RepoDigests 0}}' | sed 's/.*@//')"
done
