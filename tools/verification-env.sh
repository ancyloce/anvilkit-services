#!/bin/sh
# Reproducible entry point for the verification chain (delivery.md
# "Verification contract and evidence recording").
#
#   sh tools/verification-env.sh [run-verification.py arguments]
#
# Creates (or reuses) .local/verification-venv from CPython 3.12 with the
# pinned tools of tools/requirements.txt, installs the Python consumer package
# in place, then runs tools/run-verification.py under that interpreter with
# the given arguments (for example --static). The venv lives under the
# gitignored .local/ directory; nothing else on the machine is changed. The
# Go, Node/pnpm (packageManager in package.json), buf/oapi-codegen/sqlc pins
# and Docker/kind inputs are checked by the runner itself.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
VENV="$ROOT/.local/verification-venv"
PY=${ANVILKIT_PYTHON:-}
if [ -z "$PY" ]; then
  for candidate in python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
      PY=$(command -v "$candidate")
      break
    fi
  done
fi
if [ -z "$PY" ]; then
  echo "FAIL: CPython 3.12 not found (set ANVILKIT_PYTHON to a 3.12 interpreter)" >&2
  exit 2
fi
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --require-virtualenv -r "$ROOT/tools/requirements.txt"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --require-virtualenv --no-deps -e "$ROOT/contracts/python"
exec "$VENV/bin/python" "$ROOT/tools/run-verification.py" "$@"
