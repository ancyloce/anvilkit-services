#!/bin/sh
# One-command development smoke: a real model generates one HelloCard
# component through the controlled send path (Control admission and budget,
# Model Proxy as the only credential holder, the team's Pi coder as the one
# source writer), the trusted validator builds it, and Chromium renders it
# with a screenshot. DEVELOPMENT_ONLY; it certifies nothing and changes no
# profile, gate or release status.
#
# Prerequisites: the development foundation (sh deploy/dev/up.sh), Docker
# (a disposable Control database), built jobs/codegen/team, jobs/validator
# and services/agent/model-proxy, and for the real run a 0600 file holding
# the provider API key, named by ANVILKIT_SMOKE_CREDENTIAL_FILE (read by the
# test process and handed to the Model Proxy process only).
#
#   sh tools/smoke/hello-card.sh                      # real route (default deepseek / deepseek-flash), key file from ANVILKIT_SMOKE_CREDENTIAL_FILE
#   sh tools/smoke/hello-card.sh --from-openbao        # same, the key fetched from the unsealed dev OpenBao for this run only
#   ANVILKIT_SMOKE_UPSTREAM=fixture sh tools/smoke/hello-card.sh   # dry run on the countable double: harness only, no generation claim
#
# Inputs (environment): ANVILKIT_SMOKE_CREDENTIAL_FILE, ANVILKIT_SMOKE_PROVIDER
# (deepseek), ANVILKIT_SMOKE_MODEL (deepseek-flash), ANVILKIT_SMOKE_BASE_URL
# (https://api.deepseek.com), ANVILKIT_SMOKE_TITLE, ANVILKIT_SMOKE_EVIDENCE_DIR
# (outputs/smoke-hello-card/<timestamp>).
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
if [ ! -f .local/dev/env.sh ]; then
  echo "UNEXECUTED: .local/dev/env.sh is missing; run sh deploy/dev/up.sh first" >&2
  exit 2
fi
# shellcheck disable=SC1091
. "$root/.local/dev/env.sh"
export PATH="$root/.local/bin:$PATH"
# --from-openbao: fetch the owner's DeepSeek key from the unsealed dev OpenBao
# into a temporary 0600 file that exists only for this run.
if [ "${1:-}" = "--from-openbao" ]; then
  if [ -n "${ANVILKIT_SMOKE_CREDENTIAL_FILE:-}" ]; then
    echo "ANVILKIT_SMOKE_CREDENTIAL_FILE is already set; drop --from-openbao or the variable" >&2
    exit 2
  fi
  keyfile=$(mktemp "${TMPDIR:-/tmp}/anvilkit-smoke-key.XXXXXX")
  trap 'rm -f "$keyfile"' EXIT INT TERM
  .local/verification-venv/bin/python tools/smoke/fetch-deepseek-key.py "$keyfile" || exit $?
  ANVILKIT_SMOKE_CREDENTIAL_FILE=$keyfile
  export ANVILKIT_SMOKE_CREDENTIAL_FILE
fi
cd tests/integration
# Not exec: the EXIT trap above must still remove the temporary key file.
go test -tags 'integration smoke' -count=1 -timeout 40m -v -run '^TestSmokeHelloCard$' ./...
status=$?
cd "$root"
exit $status
