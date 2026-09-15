#!/usr/bin/env python3
"""Read-only check of the contract baseline, delegated to the contracts repository.

Usage: python3 tools/check-contracts.py [--against GIT_REF]

The sources, fixtures and the checker itself live in the anvilkit-agent-contracts submodule
(contracts/): this entry point only runs contracts/tools/check.py under the same pinned
interpreter so the parent's verification chain keeps one command name. --against names a ref
of the contracts repository (its own Git history), never of the parent.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "contracts" / "tools" / "check.py"


def main() -> int:
    if not TOOL.exists():
        print("FAIL contracts/tools/check.py is missing (the contracts submodule is not checked out)")
        return 1
    return subprocess.run([sys.executable, str(TOOL), *sys.argv[1:]], cwd=ROOT / "contracts").returncode


if __name__ == "__main__":
    sys.exit(main())
