#!/bin/sh
# Builds the owned Job images and publishes them to the development registry
# of the kind cluster (deploy/dev/up.sh starts it), by digest:
#
#   sh deploy/dev/images.sh           # build all, push, print the digests
#   sh deploy/dev/images.sh --harness # codegen supervisor and access sidecar only
#   sh deploy/dev/images.sh --parser  # parser Job image only
#
# Each image is built from its own repository's Dockerfile:
# - anvilkit-codegen: jobs/codegen/supervisor alone
#   (anvilkit-job-codegen-supervisor).
# - anvilkit-job-access-sidecar: jobs/shared/access-sidecar alone, against its
#   published, versioned contracts dependency.
# - anvilkit-job-validator: jobs/validator, with this checkout's contracts
#   sources as the named build context "contracts" (the schemas the Job
#   validates its outputs against).
# - anvilkit-codegen-team: jobs/codegen/team (anvilkit-job-codegen-team): the
#   validator image at the digest that Dockerfile names as its base (pulled
#   here from this registry), plus the supervisor of jobs/codegen/supervisor
#   (the named build context "supervisor") and the team package.
# - anvilkit-parser: jobs/parser, the fixed Docling parser: the hash-locked
#   Python dependencies, the layout weights of its models.lock and the
#   contracts' job schema (named build context "contracts").
#
# contracts/jobs/profiles.json pins each image by digest in the profiles that
# run it (the access sidecar as their sidecarImage), and deploy/policies/chart
# admits exactly those digests: a rebuilt image is a new digest and needs a
# profile revision. The published Job images are built by
# .github/workflows/job-images.yml (tools/release-artifacts.py --jobs).
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
  docker build --network=host --build-context contracts="$ROOT/contracts" -t "$REGISTRY/anvilkit-job-validator:dev" "$ROOT/jobs/validator"
  docker build --network=host ${ANVILKIT_DEV_GOPROXY:+--build-arg GOPROXY="$ANVILKIT_DEV_GOPROXY"} --build-arg VALIDATOR_REPOSITORY="$REGISTRY/anvilkit-job-validator" \
    --build-context supervisor="$ROOT/jobs/codegen/supervisor" -t "$REGISTRY/anvilkit-codegen-team:dev" "$ROOT/jobs/codegen/team"
  docker build --network=host --build-context contracts="$ROOT/contracts" -t "$REGISTRY/anvilkit-parser:dev" "$ROOT/jobs/parser"
  IMAGES="$IMAGES anvilkit-job-validator anvilkit-codegen-team anvilkit-parser"
fi
for image in $IMAGES; do
  docker push "$REGISTRY/$image:dev" >/dev/null
  printf '%s %s\n' "$image" "$(docker image inspect "$REGISTRY/$image:dev" --format '{{index .RepoDigests 0}}' | sed 's/.*@//')"
done
