#!/usr/bin/env python3
"""One entry point for the replacement's verification chain (delivery.md "Verification
contract and evidence recording").

  sh tools/verification-env.sh [--static | --only STEP]   # creates the pinned Python 3.12
                                                          # environment, then runs this file
  .local/verification-venv/bin/python tools/run-verification.py --static

Steps, in order:
  docs        tools/check-docs.py                      current-baseline Markdown, missing inputs fail
  generate    pnpm install --frozen-lockfile in        the contracts repository's pinned generators (ts-proto and
              contracts/, then                         openapi-typescript come from that locked install) reproduce
              tools/generate-contracts.py --check      every checked-in binding (contracts/{go,ts,python});
                                                       sqlc reproduces Control's data-access code
  contracts   tools/check-contracts.py                 contracts/tools/check.py: buf lint, OpenAPI, JSON Schema
                                                       fixtures, API/RPC vectors
  export      tools/check-source-export.py             the Git export set (parent plus the contracts and API
                                                       repositories) holds every declared input and no
                                                       archive/secret/legacy-submodule content; a disposable
                                                       copy passes docs/contract checks and builds on its own
  ts          pnpm install --frozen-lockfile in        the TypeScript consumer type-checks (tsc), lints (Biome)
              contracts/, then check-types/lint/test   and agrees with the fixtures (Vitest)
              in contracts/ts
  python      pytest in contracts/python               the pydantic consumer agrees with the inference fixtures
  go          go build/vet/test per module in go.work  unit tests; Docker-backed tests (Testcontainers) run
                                                       unless ANVILKIT_SKIP_DOCKER_TESTS=1
  integration go test -tags integration ...           real PostgreSQL/Temporal/Kubernetes proofs; needs the
                                                       deploy/dev foundation (deploy/dev/README.md)

Each step prints PASS, FAIL or UNEXECUTED; an UNEXECUTED step is one whose environment is
missing and is never counted as a pass. Exit code 1 if anything failed, 2 if something could
not run, 0 otherwise. This runner never regenerates, deletes or publishes anything; --check
mode generation writes only into a temporary directory. The runner itself refuses to start
under an interpreter other than the pinned one (tools/requirements.txt), because the Python
steps would then report failures of the machine rather than of the checkout.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pyenv_check  # noqa: E402

pyenv_check.require("yaml", "jsonschema", "referencing", "openapi_spec_validator", "datamodel_code_generator", "pydantic", "pytest")

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
CONTRACTS = ROOT / "contracts"
TS_PKG = CONTRACTS / "ts"
PY_PKG = CONTRACTS / "python"


def workspace_modules() -> list[pathlib.Path]:
    work = (ROOT / "go.work").read_text(encoding="utf-8")
    return [ROOT / m for m in re.findall(r"^\s*\./(\S+)", work, flags=re.M)]


def run(cmd: list[str], cwd: pathlib.Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)


def step_go(static: bool) -> tuple[str, str]:
    env = dict(os.environ)
    if static:
        env["ANVILKIT_SKIP_DOCKER_TESTS"] = "1"
    elif not shutil.which("docker"):
        return "UNEXECUTED", "docker is not on PATH; Docker-backed tests cannot run (use --static for unit-only)"
    out = []
    for mod in workspace_modules():
        listed = run(["go", "list", "./..."], mod, env)
        if "matched no packages" in listed.stderr:
            # Entirely tag-gated (tests/integration): vet with the tag; the integration step runs it.
            p = run(["go", "vet", "-tags", "integration", "./..."], mod, env)
            out.append(f"{mod.relative_to(ROOT)}: go vet -tags integration -> {'ok' if p.returncode == 0 else 'FAIL'}")
            if p.returncode != 0:
                return "FAIL", "\n".join(out + [p.stdout[-2000:], p.stderr[-2000:]])
            continue
        for cmd in (["go", "build", "./..."], ["go", "vet", "./..."], ["go", "test", "-count=1", "./..."]):
            p = run(cmd, mod, env)
            out.append(f"{mod.relative_to(ROOT)}: {' '.join(cmd[:2])} -> {'ok' if p.returncode == 0 else 'FAIL'}")
            if p.returncode != 0:
                return "FAIL", "\n".join(out + [p.stdout[-2000:], p.stderr[-2000:]])
    return "PASS", "\n".join(out)


def locked_install() -> tuple[str, str] | None:
    """The locked install of the contracts workspace provides the Node generators
    (ts-proto, openapi-typescript) that the generate step runs and the
    TypeScript checks; a fresh clone has none of them. Idempotent; None when
    it succeeded."""
    pnpm = shutil.which("pnpm")
    if not pnpm or not shutil.which("node"):
        return "UNEXECUTED", "pnpm/node are not on PATH (Node.js 24 LTS and the packageManager of contracts/package.json are required)"
    p = run([pnpm, "install", "--frozen-lockfile"], CONTRACTS, dict(os.environ))
    if p.returncode != 0:
        return "FAIL", "contracts: pnpm install --frozen-lockfile -> FAIL\n" + p.stdout[-2000:] + p.stderr[-2000:]
    return None


def step_generate() -> tuple[str, str]:
    if outcome := locked_install():
        return outcome
    return script([PY, "tools/generate-contracts.py", "--check"])


def step_ts() -> tuple[str, str]:
    if outcome := locked_install():
        return outcome
    pnpm = shutil.which("pnpm")
    out = ["contracts: pnpm install --frozen-lockfile -> ok"]
    steps = [
        ("check-types", [pnpm, "run", "check-types"], TS_PKG),
        ("lint", [pnpm, "run", "lint"], TS_PKG),
        ("test", [pnpm, "run", "test"], TS_PKG),
    ]
    for name, cmd, cwd in steps:
        p = run(cmd, cwd, dict(os.environ))
        out.append(f"{TS_PKG.relative_to(ROOT)}: {name} -> {'ok' if p.returncode == 0 else 'FAIL'}")
        if p.returncode != 0:
            return "FAIL", "\n".join(out + [p.stdout[-2000:], p.stderr[-2000:]])
    return "PASS", "\n".join(out)


def step_python() -> tuple[str, str]:
    p = run([PY, "-m", "pytest", "-q"], PY_PKG, dict(os.environ))
    detail = f"{PY_PKG.relative_to(ROOT)}: pytest -> {'ok' if p.returncode == 0 else 'FAIL'}\n{p.stdout[-1500:]}"
    return ("PASS" if p.returncode == 0 else "FAIL"), detail


def step_integration() -> tuple[str, str]:
    missing = [v for v in ("ANVILKIT_DEV_CONTROL_DSN", "ANVILKIT_DEV_TEMPORAL_ADDRESS", "KUBECONFIG") if not os.environ.get(v)]
    if missing:
        return "UNEXECUTED", "deploy/dev foundation not configured; missing " + ", ".join(missing)
    out = []
    for mod in workspace_modules():
        if not list(mod.rglob("*_integration_test.go")):
            continue
        p = run(["go", "test", "-tags", "integration", "-count=1", "./..."], mod, dict(os.environ))
        out.append(f"{mod.relative_to(ROOT)}: integration -> {'ok' if p.returncode == 0 else 'FAIL'}")
        if p.returncode != 0:
            return "FAIL", "\n".join(out + [p.stdout[-3000:], p.stderr[-2000:]])
    return "PASS", "\n".join(out) if out else "no integration tests present"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", action="store_true")
    ap.add_argument("--only", action="append")
    a = ap.parse_args()

    steps = [
        ("docs", lambda: script([PY, "tools/check-docs.py"])),
        ("generate", step_generate),
        ("contracts", lambda: script([PY, "tools/check-contracts.py"])),
        ("export", lambda: script([PY, "tools/check-source-export.py"])),
        ("ts", step_ts),
        ("python", step_python),
        ("go", lambda: step_go(a.static)),
        ("integration", step_integration),
    ]
    outcomes = []
    for name, fn in steps:
        if a.only and name not in a.only:
            continue
        if a.static and name == "integration":
            continue
        t0 = time.time()
        status, detail = fn()
        outcomes.append((name, status))
        print(f"{status:<11} {name:<12} ({time.time() - t0:.1f}s)")
        if status != "PASS":
            for line in detail.strip().splitlines()[-15:]:
                print(f"            | {line}")
    failed = [n for n, s in outcomes if s == "FAIL"]
    unexec = [n for n, s in outcomes if s == "UNEXECUTED"]
    print(f"\n{len(outcomes)} steps, {len(failed)} failed, {len(unexec)} unexecuted")
    if unexec:
        print("  unexecuted: " + ", ".join(unexec) + "   (environment missing; NOT a pass)")
    print("\nThese checks establish contract agreement and local behavior only; G-01..G-14 stay NOT_RUN "
          "until their complete real-environment evidence exists.")
    return 1 if failed else (2 if unexec else 0)


def script(cmd: list[str]) -> tuple[str, str]:
    p = run(cmd, ROOT)
    return ("PASS" if p.returncode == 0 else "FAIL"), p.stdout + p.stderr


if __name__ == "__main__":
    sys.exit(main())
