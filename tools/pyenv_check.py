"""Interpreter and dependency preflight shared by the verification tools.

The tools run under the pinned CPython 3.12 environment that
tools/verification-env.sh creates from tools/requirements.txt; a tool that
imports a third-party module calls `require(...)` first so a wrong
interpreter or a missing pin fails with the installation command instead of
a traceback. The check is read-only and imports nothing it is not asked for.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

INTERPRETER = (3, 12)
ROOT = pathlib.Path(__file__).resolve().parent.parent
HINT = "run `sh tools/verification-env.sh` (creates .local/verification-venv from tools/requirements.txt) and use its interpreter"


def problems(modules: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    if sys.version_info[:2] != INTERPRETER:
        out.append(f"interpreter {sys.executable} is Python {sys.version_info.major}.{sys.version_info.minor}; the pinned toolchain is CPython {INTERPRETER[0]}.{INTERPRETER[1]}")
    for name in modules:
        try:
            importlib.import_module(name)
        except ImportError:
            out.append(f"module {name} is not installed for {sys.executable}")
    return out


def require(*modules: str) -> None:
    """Exit 2 with the installation hint when the environment is not the pinned one."""
    found = problems(modules)
    if found:
        for p in found:
            print("UNEXECUTED", p, file=sys.stderr)
        print("UNEXECUTED", HINT, file=sys.stderr)
        sys.exit(2)
