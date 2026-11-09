#!/usr/bin/env python3
"""Read-only check of the contract baseline, delegated to the contracts repository.

Usage: python3 tools/check-contracts.py [--against GIT_REF]

The sources, fixtures and the checker itself live in the anvilkit-agent-contracts submodule
(contracts/): this entry point runs contracts/tools/check.py under the same pinned
interpreter so the parent's verification chain keeps one command name. --against names a ref
of the contracts repository (its own Git history), never of the parent.

One check is the parent's, because only this checkout holds both sides: the service
repositories that test against a verbatim copy of a contracts fixture hold the bytes of its
source (FIXTURE_COPIES). A missing or differing copy fails; nothing is skipped.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "contracts" / "tools" / "check.py"
# RB-A23: the google.rpc.ErrorInfo detail of a Control revision conflict. Control (the
# producer) and the API (the consumer) link no package that carries the vectors, so each
# tests against a verbatim copy.
FIXTURE_COPIES = {
    "contracts/proto/status-details.fixtures.json": (
        "services/agent/control/internal/transport/grpc/testdata/status-details.fixtures.json",
        "services/agent/api/internal/adapters/control/testdata/status-details.fixtures.json",
    ),
}


def check_fixture_copies() -> int:
    failures = 0
    for source, copies in FIXTURE_COPIES.items():
        if not (ROOT / source).is_file():
            print(f"FAIL {source} is missing: its copies cannot be compared")
            failures += 1
            continue
        want = (ROOT / source).read_bytes()
        for copy in copies:
            if not (ROOT / copy).is_file():
                print(f"FAIL {copy} is missing (a verbatim copy of {source})")
                failures += 1
            elif (ROOT / copy).read_bytes() != want:
                print(f"FAIL {copy} differs from {source}: copy the source over it")
                failures += 1
    print(f"fixture copies: {sum(len(c) for c in FIXTURE_COPIES.values())} compared, {failures} failures")
    return failures


def main() -> int:
    if not TOOL.exists():
        print("FAIL contracts/tools/check.py is missing (the contracts submodule is not checked out)")
        return 1
    rc = subprocess.run([sys.executable, str(TOOL), *sys.argv[1:]], cwd=ROOT / "contracts").returncode
    return 1 if check_fixture_copies() else rc


if __name__ == "__main__":
    sys.exit(main())
