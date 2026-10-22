#!/bin/sh
# One-command development smoke of the API-driven generation: the in-cluster
# API (kind-anvilkit-dev, namespace anvilkit-apps) is port-forwarded, the
# client tools/smoke/api-generate.py uploads the prompt, runs the preparation
# and the generation and follows both over SSE, downloads the delivered
# source, and the trusted validator then builds that source locally and
# Chromium renders it for one screenshot (tools/smoke/hello-card-screenshot.mjs).
# DEVELOPMENT_ONLY: it certifies nothing and changes no profile, gate or
# release status. The provider credential lives in the cluster's Model Proxy
# Secret; this script holds only the API's fixture bearer token.
#
# Prerequisites: the dev foundation (sh deploy/dev/up.sh), the Control, API,
# Model Proxy and Workflow releases installed with their smoke overlays
# (deploy/dev/values/*-smoke.yaml, *-deepseek.yaml), budget pools for tenant_a.
#
#   sh tools/smoke/api-generate.sh [--prompt "..."] [--brief-id <id>] [--answer "..."]
#
# Environment: ANVILKIT_SMOKE_EVIDENCE_DIR (outputs/smoke-api-generation/<ts>),
# ANVILKIT_SMOKE_API_PORT (9100).
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
[ -f .local/dev/env.sh ] || { echo "UNEXECUTED: .local/dev/env.sh is missing; run sh deploy/dev/up.sh first" >&2; exit 2; }
# shellcheck disable=SC1091
. "$root/.local/dev/env.sh"
export PATH="$root/.local/bin:$PATH"
export KUBECONFIG=${ANVILKIT_DEV_ADMIN_KUBECONFIG:-$HOME/.kube/config}
port=${ANVILKIT_SMOKE_API_PORT:-9100}
evidence=${ANVILKIT_SMOKE_EVIDENCE_DIR:-$root/outputs/smoke-api-generation/$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$evidence"
kubectl --context kind-anvilkit-dev -n anvilkit-apps port-forward svc/anvilkit-agent-api "$port:80" >"$evidence/port-forward.log" 2>&1 &
pf=$!
trap 'kill $pf 2>/dev/null || true' EXIT INT TERM
i=0
until curl -fsS -o /dev/null "http://127.0.0.1:$port/readyz" 2>/dev/null; do
  i=$((i+1)); [ $i -lt 30 ] || { echo "UNEXECUTED: the API port-forward did not become ready" >&2; exit 2; }; sleep 1
done
status=0
python3 tools/smoke/api-generate.py --api "http://127.0.0.1:$port" --evidence "$evidence" "$@" || status=$?
if [ $status -eq 0 ]; then
  # The delivered source through the trusted validator's build and one browser render, locally.
  src="$evidence/generated-source"
  rev=$(python3 -c "import json;print(json.load(open('$evidence/summary.json'))['source']['headers'].get('AnvilKit-Source-Revision','1'))")
  cid=$(python3 -c "import json;d=json.load(open('$src/component.json'));print(d['componentId'])")
  ptype=$(python3 -c "import json;d=json.load(open('$src/component.json'));print(d['puckType'])")
  pname=$(python3 -c "import json;d=json.load(open('$src/package.json'));print(d['name'])")
  (cd jobs/validator && NODE_ENV=production ANVILKIT_VALIDATOR_CONTRACTS_DIR="$root/contracts" node dist/cli.js "$src" --source-revision "$rev" \
    --component-id "$cid" --puck-type "$ptype" --package-name "$pname" --out "$evidence/validator" --validator-profile validator-dev-v1) >"$evidence/validator.log" 2>&1 || { echo "local validator build FAILED (see $evidence/validator.log)"; status=1; }
  if [ $status -eq 0 ]; then
    node tools/smoke/hello-card-screenshot.mjs --validator "$root/jobs/validator" --artifacts "$evidence/validator/artifacts" \
      --component-json "$src/component.json" --out "$evidence/screenshot.png" >"$evidence/screenshot.log" 2>&1 || { echo "browser render FAILED (see $evidence/screenshot.log)"; status=1; }
    tail -1 "$evidence/screenshot.log"
  fi
fi
echo "evidence: $evidence"
exit $status
