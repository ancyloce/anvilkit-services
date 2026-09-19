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
  validator   pnpm install --frozen-lockfile, then     the validator Job package (P10) type-checks, lints,
              check-types/lint/profiles:check/build/   verifies its profiles, compiles and certifies its fixed
              test in jobs/validator                   component (Vitest; real build, SSR and Chromium steps)
  team        pnpm install --frozen-lockfile, then     the codegen team package (P12) type-checks, lints,
              check-types, lint, build, tools:check,   builds, checks its reviewed tools document and tests
              test in jobs/codegen/team                (Vitest; a countable sidecar double, the real
                                                       validator chain when jobs/validator is built)
  model-proxy pnpm install --frozen-lockfile, then     the Model Proxy (P11) type-checks, lints, builds and
              check-types/lint/build/test in           proves its guards and call scenarios (Vitest: the real
              services/agent/model-proxy               pi-ai path against a countable upstream, a fake
                                                       Control, two instances on one store, mTLS; the S3
                                                       store against the foundation's MinIO when present)
  python      pytest in contracts/python               the pydantic consumer agrees with the inference fixtures
  knowledge   pnpm install --frozen-lockfile, then     the Knowledge owner (P14) type-checks, lints, builds and
              check-types/lint/build/test in           tests (Vitest; real PostgreSQL through Testcontainers;
              services/agent/knowledge                 --static leaves the tests out); its Go forwarder sidecar
                                                       module is a go.work module of the go step
  background  pnpm install --frozen-lockfile, then     the Background Worker and owner queue relay (P14)
              check-types/lint/build/test in           type-check, lint, build and test (Vitest; real
              services/agent/background-worker         PostgreSQL, Valkey and NATS through Testcontainers)
  profiles    python packages/profile-schemas/         the shared configuration schemas' cross-language
              python/config_generation.py --fixtures   fixtures pass the Python consumer example (P14-06)
  go          go build/vet/test per module in go.work  unit tests; Docker-backed tests (Testcontainers) run
                                                       unless ANVILKIT_SKIP_DOCKER_TESTS=1
  integration go test -tags integration ...           real PostgreSQL/Temporal/Kubernetes proofs; needs the
                                                       deploy/dev foundation (deploy/dev/README.md); for the
                                                       Workflow repository's cluster scenario the parent
                                                       starts a Control from the Control repository first

Each step prints PASS, FAIL or UNEXECUTED; an UNEXECUTED step is one whose environment is
missing and is never counted as a pass. Exit code 1 if anything failed, 2 if something could
not run, 0 otherwise. This runner never regenerates, deletes or publishes anything; --check
mode generation writes only into a temporary directory. The runner itself refuses to start
under an interpreter other than the pinned one (tools/requirements.txt), because the Python
steps would then report failures of the machine rather than of the checkout.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
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


VALIDATOR_PKG = ROOT / "jobs" / "validator"


def step_validator() -> tuple[str, str]:
    """The validator Job package (P10): its locked install (no lifecycle script),
    type check, lint, profile digests and toolchain, the compiled dist the
    integration step's host-process scenario runs, and the tests (the build,
    SSR and Chromium steps run real processes)."""
    pnpm = shutil.which("pnpm")
    if not pnpm or not shutil.which("node"):
        return "UNEXECUTED", "pnpm/node are not on PATH (Node.js 24 LTS and the packageManager of jobs/validator/package.json are required)"
    out = []
    for name, cmd in (
        ("install", [pnpm, "install", "--frozen-lockfile"]),
        ("check-types", [pnpm, "run", "check-types"]),
        ("lint", [pnpm, "run", "lint"]),
        ("profiles:check", [pnpm, "run", "profiles:check"]),
        ("build", [pnpm, "run", "build"]),
        ("test", [pnpm, "run", "test"]),
    ):
        p = run(cmd, VALIDATOR_PKG, dict(os.environ))
        out.append(f"jobs/validator: pnpm {name} -> {'ok' if p.returncode == 0 else 'FAIL'}")
        if p.returncode != 0:
            return "FAIL", "\n".join(out + [p.stdout[-2000:], p.stderr[-2000:]])
    return "PASS", "\n".join(out)


MODEL_PROXY_PKG = ROOT / "services" / "agent" / "model-proxy"


def step_model_proxy() -> tuple[str, str]:
    """The Model Proxy (P11): its locked install (no lifecycle script), type
    check, lint, build and tests; the contract document comes from the
    contracts checkout of this repository."""
    pnpm = shutil.which("pnpm")
    if not pnpm or not shutil.which("node"):
        return "UNEXECUTED", "pnpm/node are not on PATH (Node.js 24 LTS and the packageManager of services/agent/model-proxy/package.json are required)"
    env = dict(os.environ, ANVILKIT_MODEL_PROXY_CONTRACTS_DIR=str(CONTRACTS))
    out = []
    for name, cmd in (
        ("install", [pnpm, "install", "--frozen-lockfile", "--ignore-scripts"]),
        ("check-types", [pnpm, "run", "check-types"]),
        ("lint", [pnpm, "run", "lint"]),
        ("build", [pnpm, "run", "build"]),
        ("test", [pnpm, "run", "test"]),
    ):
        p = run(cmd, MODEL_PROXY_PKG, env)
        out.append(f"services/agent/model-proxy: pnpm {name} -> {'ok' if p.returncode == 0 else 'FAIL'}")
        if p.returncode != 0:
            return "FAIL", "\n".join(out + [p.stdout[-2000:], p.stderr[-2000:]])
    return "PASS", "\n".join(out)


TEAM_PKG = ROOT / "jobs" / "codegen" / "team"


def step_team() -> tuple[str, str]:
    """The codegen team package (P12): its locked install (better-sqlite3's
    prebuilt binary is the one lifecycle script its workspace file allows),
    type check, lint, build (the coder and coordinator the tests and the
    integration scenario spawn), the reviewed tools document checked against
    the build, and the tests (a countable sidecar double; the validation test
    runs the real validator chain from jobs/validator when it is built)."""
    pnpm = shutil.which("pnpm")
    if not pnpm or not shutil.which("node"):
        return "UNEXECUTED", "pnpm/node are not on PATH (Node.js 24 LTS and the packageManager of jobs/codegen/team/package.json are required)"
    env = dict(os.environ, ANVILKIT_CODEGEN_TEAM_CONTRACTS_DIR=str(CONTRACTS))
    out = []
    for name, cmd in (
        ("install", [pnpm, "install", "--frozen-lockfile"]),
        ("check-types", [pnpm, "run", "check-types"]),
        ("lint", [pnpm, "run", "lint"]),
        ("build", [pnpm, "run", "build"]),
        ("tools:check", [pnpm, "run", "tools:check"]),
        ("test", [pnpm, "run", "test"]),
    ):
        p = run(cmd, TEAM_PKG, env)
        out.append(f"jobs/codegen/team: pnpm {name} -> {'ok' if p.returncode == 0 else 'FAIL'}")
        if p.returncode != 0:
            return "FAIL", "\n".join(out + [p.stdout[-2000:], p.stderr[-2000:]])
    return "PASS", "\n".join(out)


def step_python() -> tuple[str, str]:
    p = run([PY, "-m", "pytest", "-q"], PY_PKG, dict(os.environ))
    detail = f"{PY_PKG.relative_to(ROOT)}: pytest -> {'ok' if p.returncode == 0 else 'FAIL'}\n{p.stdout[-1500:]}"
    return ("PASS" if p.returncode == 0 else "FAIL"), detail


KNOWLEDGE_PKG = ROOT / "services" / "agent" / "knowledge"
BACKGROUND_WORKER_PKG = ROOT / "services" / "agent" / "background-worker"
PROFILE_SCHEMAS = ROOT / "packages" / "profile-schemas"


def pnpm_package(rel: pathlib.Path, env: dict, scripts: tuple[str, ...] = ("check-types", "lint", "build", "test")) -> tuple[str, str]:
    """The locked install (no lifecycle script) and the named scripts of one
    TypeScript package outside the workspace."""
    pnpm = shutil.which("pnpm")
    if not pnpm or not shutil.which("node"):
        return "UNEXECUTED", f"pnpm/node are not on PATH (Node.js 24 LTS and the packageManager of {rel}/package.json are required)"
    out = []
    for name, cmd in (("install", [pnpm, "install", "--frozen-lockfile", "--ignore-scripts"]),) + tuple((s, [pnpm, "run", s]) for s in scripts):
        p = run(cmd, ROOT / rel, env)
        out.append(f"{rel}: pnpm {name} -> {'ok' if p.returncode == 0 else 'FAIL'}")
        if p.returncode != 0:
            return "FAIL", "\n".join(out + [p.stdout[-2000:], p.stderr[-2000:]])
    return "PASS", "\n".join(out)


def step_knowledge(static: bool) -> tuple[str, str]:
    """The Knowledge owner (P14): type check, lint, build (the entries the
    forwarder test and the integration scenario spawn) and tests (Vitest;
    Docker-backed PostgreSQL through Testcontainers, so --static leaves the
    tests out). The Go forwarder sidecar module is covered by the go step."""
    if static:
        return pnpm_package(pathlib.Path("services/agent/knowledge"), dict(os.environ), ("check-types", "lint", "build"))
    if not shutil.which("docker"):
        return "UNEXECUTED", "docker is not on PATH; the Knowledge tests need Testcontainers (use --static for the build alone)"
    return pnpm_package(pathlib.Path("services/agent/knowledge"), dict(os.environ))


def step_background_worker(static: bool) -> tuple[str, str]:
    """The Background Worker and owner queue relay (P14): type check, lint,
    build and tests (Vitest; Docker-backed PostgreSQL, Valkey and NATS
    through Testcontainers, so --static leaves the tests out)."""
    env = dict(os.environ, ANVILKIT_BACKGROUND_WORKER_CONTRACTS_DIR=str(CONTRACTS))
    if static:
        return pnpm_package(pathlib.Path("services/agent/background-worker"), env, ("check-types", "lint", "build"))
    if not shutil.which("docker"):
        return "UNEXECUTED", "docker is not on PATH; the Background Worker tests need Testcontainers (use --static for the build alone)"
    return pnpm_package(pathlib.Path("services/agent/background-worker"), env)


def step_profile_schemas() -> tuple[str, str]:
    """The shared configuration schemas (P14-06): the Python consumer example
    runs the cross-language fixture cases (the Go and TypeScript consumers
    run the same file in their own tests)."""
    p = run([PY, str(PROFILE_SCHEMAS / "python" / "config_generation.py"), "--fixtures", str(PROFILE_SCHEMAS / "fixtures.json")], PROFILE_SCHEMAS, dict(os.environ))
    detail = f"packages/profile-schemas: python fixtures -> {'ok' if p.returncode == 0 else 'FAIL'}\n{p.stdout[-1500:]}{p.stderr[-800:]}"
    return ("PASS" if p.returncode == 0 else "FAIL"), detail


@contextlib.contextmanager
def prepared_control(env: dict):
    """The cross-service dependency of the Workflow repository's cluster scenario
    (services/agent/workflow, internal/adapters/kubernetes, tag integration): a
    Control built from the Control repository, bound to every interface so a Job
    Pod's sidecar reaches it through the kind network gateway, with the artifact
    store as the cluster reaches it. The Workflow repository builds nothing but
    itself and reads the endpoints from ANVILKIT_INTEGRATION_CONTROL_ADDRESS (host
    side) and ANVILKIT_INTEGRATION_SIDECAR_CONTROL_ADDRESS (as the Pod reaches it);
    without a gateway or artifact endpoint the scenario skips on its own."""
    gateway, artifacts = env.get("ANVILKIT_DEV_KIND_GATEWAY"), env.get("ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER")
    if not gateway or not artifacts:
        yield env, "no kind gateway / cluster artifact endpoint in the environment; the Workflow cluster scenario skips"
        return
    control = ROOT / "services/agent/control"
    with tempfile.TemporaryDirectory(prefix="anvilkit-control-") as scratch:
        binary = pathlib.Path(scratch) / "anvilkit-agent-control"
        built = run(["go", "build", "-o", str(binary), "./cmd/anvilkit-agent-control"], control, env)
        if built.returncode != 0:
            raise RuntimeError("build of services/agent/control failed: " + (built.stdout + built.stderr)[-2000:])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        proc_env = dict(env, ANVILKIT_CONTROL_CONFIG=str(control / "config.yaml"), ANVILKIT_CONTROL_LISTEN=f"0.0.0.0:{port}",
                        ANVILKIT_CONTROL_DATABASE_URL=env["ANVILKIT_DEV_CONTROL_DSN"], ANVILKIT_CONTROL_INVENTORY_DIR=str(pathlib.Path(scratch) / "inventory"),
                        ANVILKIT_CONTROL_TEMPORAL_ADDRESS=env["ANVILKIT_DEV_TEMPORAL_ADDRESS"], ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT=artifacts)
        log = open(pathlib.Path(scratch) / "control.log", "w", encoding="utf-8")
        proc = subprocess.Popen([str(binary)], cwd=control, env=proc_env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 60
            while time.time() < deadline:
                with socket.socket() as s:
                    s.settimeout(0.5)
                    try:
                        s.connect(("127.0.0.1", port))
                        break
                    except OSError:
                        time.sleep(0.2)
            else:
                raise RuntimeError("the prepared Control did not open its listener")
            yield dict(env, ANVILKIT_INTEGRATION_CONTROL_ADDRESS=f"127.0.0.1:{port}", ANVILKIT_INTEGRATION_SIDECAR_CONTROL_ADDRESS=f"{gateway}:{port}"), f"Control prepared on 0.0.0.0:{port}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()


def step_integration() -> tuple[str, str]:
    missing = [v for v in ("ANVILKIT_DEV_CONTROL_DSN", "ANVILKIT_DEV_TEMPORAL_ADDRESS", "KUBECONFIG") if not os.environ.get(v)]
    if missing:
        return "UNEXECUTED", "deploy/dev foundation not configured; missing " + ", ".join(missing)
    out = []
    for mod in workspace_modules():
        if not list(mod.rglob("*_integration_test.go")):
            continue
        env = dict(os.environ)
        if mod == ROOT / "services/agent/workflow":
            # The parent prepares the cross-service dependency of that repository's scenario.
            try:
                with prepared_control(env) as (env, note):
                    out.append(f"{mod.relative_to(ROOT)}: {note}")
                    p = run(["go", "test", "-tags", "integration", "-count=1", "./..."], mod, env)
            except RuntimeError as exc:
                return "FAIL", "\n".join(out + [str(exc)])
        else:
            p = run(["go", "test", "-tags", "integration", "-count=1", "./..."], mod, env)
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
        ("validator", step_validator),
        ("model-proxy", step_model_proxy),
        ("team", step_team),
        ("python", step_python),
        ("knowledge", lambda: step_knowledge(a.static)),
        ("background", lambda: step_background_worker(a.static)),
        ("profiles", step_profile_schemas),
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
