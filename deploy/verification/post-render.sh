#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
exec "$ROOT/.local/verification-venv/bin/python" "$ROOT/deploy/verification/post-render.py"
