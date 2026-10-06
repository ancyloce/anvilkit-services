#!/bin/sh
# Builds the P09 harness images, the P10 validator image and the P12 team
# image and publishes them to the development registry of the kind cluster
# (deploy/dev/up.sh starts it), by digest:
#
#   sh deploy/dev/images.sh          # build all, push, print the digests
#   sh deploy/dev/images.sh --harness # P09 supervisor and sidecar only
#   sh deploy/dev/images.sh --parser  # P15 parser Job image only
#
# anvilkit-parser (P15, jobs/parser) is the fixed Docling parser Job image:
# the hash-locked Python dependencies, the layout weights of its
# models.lock and the contracts' job schema (named build context
# "contracts"); parser-docling-dev-v1 pins its digest.
#
# anvilkit-codegen-team (P12) is built by the team repository
# (jobs/codegen/team, anvilkit-job-codegen-team, its Dockerfile): the
# validator image at the digest that Dockerfile names as its base (pulled
# here from this registry), plus the supervisor of jobs/codegen/supervisor
# (the named build context "supervisor") and the team package; it is pinned
# by codegen-team-dev-v1 (DISABLED: it runs model-written code and needs the
# gVisor qualification).
# anvilkit-codegen builds from jobs/codegen/supervisor alone
# (anvilkit-job-codegen-supervisor, its own Dockerfile);
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
  ""|--harness|--parser) ;;
  *) echo "usage: $0 [--harness|--parser]" >&2; exit 2 ;;
esac
if [ "${1:-}" = --parser ]; then
  docker build --network=host --build-context contracts="$ROOT/contracts" -t "$REGISTRY/anvilkit-parser:dev" "$ROOT/jobs/parser"
  docker push "$REGISTRY/anvilkit-parser:dev" >/dev/null
  printf '%s %s\n' anvilkit-parser "$(docker image inspect "$REGISTRY/anvilkit-parser:dev" --format '{{index .RepoDigests 0}}' | sed 's/.*@//')"
  exit 0
fi
IMAGES="anvilkit-codegen anvilkit-job-access-sidecar"
docker build --network=host ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} -t "$REGISTRY/anvilkit-codegen:dev" "$ROOT/jobs/codegen/supervisor"
docker build --network=host ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} -t "$REGISTRY/anvilkit-job-access-sidecar:dev" "$ROOT/jobs/shared/access-sidecar"
if [ "${1:-}" != --harness ]; then
  docker build --network=host --build-context contracts="$ROOT/contracts" -t "$REGISTRY/anvilkit-validator:dev" "$ROOT/jobs/validator"
  docker build --network=host ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} --build-arg VALIDATOR_REPOSITORY="$REGISTRY/anvilkit-validator" \
    --build-context supervisor="$ROOT/jobs/codegen/supervisor" -t "$REGISTRY/anvilkit-codegen-team:dev" "$ROOT/jobs/codegen/team"
  docker build --network=host --build-context contracts="$ROOT/contracts" -t "$REGISTRY/anvilkit-parser:dev" "$ROOT/jobs/parser"
  IMAGES="$IMAGES anvilkit-validator anvilkit-codegen-team anvilkit-parser"
fi
for image in $IMAGES; do
  docker push "$REGISTRY/$image:dev" >/dev/null
  printf '%s %s\n' "$image" "$(docker image inspect "$REGISTRY/$image:dev" --format '{{index .RepoDigests 0}}' | sed 's/.*@//')"
done
