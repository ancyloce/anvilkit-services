#!/usr/bin/env python3
"""One entry point for the replacement's verification chain (delivery.md "Verification
contract and evidence recording").

  sh tools/verification-env.sh [--static | --only STEP]   # creates the pinned Python 3.12
                                                          # environment, then runs this file
  .local/verification-venv/bin/python tools/run-verification.py --static

Steps, in order:
  docs        tools/check-docs.py                      current-baseline Markdown, missing inputs fail, generated status drift (docs-status.py)
  generate    pnpm install --frozen-lockfile in        the contracts repository's pinned generators (ts-proto and
              contracts/, then                         openapi-typescript come from that locked install) reproduce
              tools/generate-contracts.py --check      every checked-in binding (contracts/{go,ts,python});
                                                       sqlc reproduces Control's data-access code
  contracts   tools/check-contracts.py                 contracts/tools/check.py: buf lint, OpenAPI, JSON Schema
                                                       fixtures, API/RPC vectors
  evals       tools/run-evaluation.py --check and       the P22 registry (tests/evals/registry.json) maps every
              --self-test                              M1/CAP/SEC requirement to existing executable cases or
                                                       explicit NOT_RUN entries naming the missing ENV inputs;
                                                       the evaluator's skip/BLOCKED/roll-up negatives hold
  deployment  tools/check-deployment-lock.py           the P23 deployment lock (deploy/gitops/lock.yaml): the eight
              --self-test; the self-tests of           charts rendered with the qualification values carry its
                                                       replica/HPA/PDB/spread/probe/grace/pool limits, the
                                                       connection budgets fit, production fields stay REQUIRED;
              tools/release-artifacts.py,              its negative probes must each be detected; the release
              deploy/gitops/publish.py and             tools refuse BLOCKED, dirty or unsigned inputs and
              deploy/qualification/record.py           evidence of another release (P3.2)
  handover    deploy/handover/rehearse.py self-test    the P24 rehearsal's decisions on synthetic inputs: each
                                                       single violation of a preservation rule requires a
                                                       transfer; legacy-name patterns and route classifiers
  export      tools/check-source-export.py             the committed trees a clean recursive clone checks out
              --self-test, then the check              (the parent's HEAD and each submodule at its gitlink)
                                                       hold every declared input and no archive/secret/
                                                       legacy-submodule content; an untracked input fails;
                                                       a disposable extraction passes docs/contract checks
                                                       and builds on its own
  ts          pnpm install --frozen-lockfile in        the TypeScript consumer type-checks (tsc), lints (Biome)
              contracts/, then check-types/lint/test   and agrees with the fixtures (Vitest)
              in contracts/ts
  validator   pnpm install --frozen-lockfile, then     the validator Job package (P10) type-checks, lints,
              check-types/lint/profiles:check/build/   verifies its profiles, compiles and certifies its fixed
              test in jobs/validator                   component (Vitest; real build, SSR and Chromium steps)
  team        sync-protocol.sh --check in both         the codegen process protocol copies of
              jobs/codegen repositories, then pnpm     jobs/codegen/{supervisor,team} are contracts/jobs/codegen;
              install --frozen-lockfile, check-types,  the codegen team package (P12) type-checks, lints,
              lint, build, tools:check, test in        builds, checks its reviewed tools document and tests
              jobs/codegen/team                        (Vitest; a countable sidecar double, the real
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
  parser      unittest of jobs/parser in the built     the parser Job (P15): this checkout's sources and tests
              anvilkit-parser:dev image, no network    with the image's locked dependencies and pinned weights
                                                       (real Docling conversions, every guard); --static and a
                                                       missing image byte-compile the sources only
  inference   unittest of services/agent/inference     the Inference service (P15): the compute contract and,
              in the built anvilkit-agent-inference    with the image's locked weights, real BGE-M3 and
              image, no network                        reranker computations; --static byte-compiles only
  go          go build/vet/test per module in go.work  unit tests; Docker-backed tests (Testcontainers) run
                                                       unless ANVILKIT_SKIP_DOCKER_TESTS=1
  integration go test -tags integration ...           real PostgreSQL/Temporal/Kubernetes proofs; needs the
                                                       deploy/dev foundation (deploy/dev/README.md); for the
                                                       Workflow repository's cluster scenario the parent
                                                       starts a Control from the Control repository first

Each step prints PASS, PARTIAL, FAIL or UNEXECUTED (P3.3, plan 0004). PASS means every
part of the step ran and passed. PARTIAL means what ran passed but work was omitted: a
step that only byte-compiles or builds under --static (knowledge, background, parser,
inference), or any test that skipped itself. The Go and Vitest suites run with their
JSON reporters (`go test -json`, `vitest --reporter=json`), and every skipped package,
test or subtest is listed as an UNEXECUTED unit with its skip reason; the summary counts
those units apart from the steps. UNEXECUTED means the step's environment is missing.
Neither PARTIAL nor UNEXECUTED is ever counted as a pass. Running as root sets
ANVILKIT_REQUIRE_ROOT_TESTS=1, which turns a root-only skip into a failure, and
ANVILKIT_REQUIRE_VALIDATOR=1 once jobs/validator is built; their absence is reported.
Every command has a timeout (ANVILKIT_VERIFY_TIMEOUT seconds, default 2400). An unknown
--only name is refused. Exit code 1 if anything failed, 2 if any step is not a full PASS,
0 otherwise. This runner never regenerates, deletes or publishes anything; --check mode
generation writes only into a temporary directory. The runner itself refuses to start
under an interpreter other than the pinned one (tools/requirements.txt), because the Python
steps would then report failures of the machine rather than of the checkout.
"""
from __future__ import annotations

import argparse
import contextlib
import json
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


TIMEOUT = int(os.environ.get("ANVILKIT_VERIFY_TIMEOUT", "2400"))
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def run(cmd: list[str], cwd: pathlib.Path, env: dict | None = None, timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    """One command with a bound; a timeout is a failure with what it printed so far."""
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return subprocess.CompletedProcess(cmd, 124, out, err + f"\ntimed out after {timeout}s: {' '.join(cmd)}")


def test_env(env: dict | None = None) -> dict:
    """The environment of every test command: as root, root-only tests must run
    (ANVILKIT_REQUIRE_ROOT_TESTS=1 turns their skip into a failure); with the
    validator built, the team's validation cases must run (ANVILKIT_REQUIRE_VALIDATOR=1)."""
    env = dict(os.environ if env is None else env)
    if IS_ROOT:
        env.setdefault("ANVILKIT_REQUIRE_ROOT_TESTS", "1")
    if (ROOT / "jobs" / "validator" / "dist" / "job.js").exists():
        env.setdefault("ANVILKIT_REQUIRE_VALIDATOR", "1")
    return env


class Units:
    """Skipped tests of a step: each is an UNEXECUTED unit with its reason."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, unit: str, reason: str) -> None:
        self.items.append(f"{unit}: {reason}")


def go_test(mod: pathlib.Path, env: dict, units: Units) -> tuple[bool, str]:
    """`go test -json -count=1 ./...` in one module: every skipped test or subtest
    becomes an UNEXECUTED unit with its printed reason; a failure keeps the
    failing tests' output and, for a test that never finished, where it hung."""
    p = run(["go", "test", "-json", "-count=1", "./..."], mod, env)
    notes: dict[str, list[str]] = {}
    failed: list[str] = []
    running: set[str] = set()
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        pkg, test, action = ev.get("Package", ""), ev.get("Test"), ev.get("Action")
        key = f"{pkg} {test or ''}".strip()
        if action == "output":
            notes.setdefault(key, []).append(ev.get("Output") or "")
        elif action == "run" and test:
            running.add(key)
        elif action == "skip" and test:
            running.discard(key)
            said = [n.strip() for n in notes.get(key, []) if "_test.go:" in n]
            units.add(f"{mod.relative_to(ROOT)} {pkg.rsplit('/', 1)[-1]}.{test}", (said[-1] if said else "no reason printed")[:200])
        elif action in ("pass", "fail"):
            running.discard(key)
            if action == "fail":
                failed.append(key)
    if p.returncode == 0 and not failed:
        return True, ""
    tail = "".join(n for k in failed for n in notes.get(k, []))[-3000:]
    stuck = "".join(unfinished(k, "".join(notes.get(k, []))) for k in sorted(running))
    return False, (stuck + tail) or (p.stdout[-2000:] + p.stderr[-2000:])


def unfinished(key: str, out: str) -> str:
    """A test still running when its package failed (a `go test` timeout: Go
    attributes the panic naming the running tests, and the goroutine dump, to
    it and never reports it failed): the panic and the goroutines inside test
    code, which show where it hung."""
    head, *blocks = out.split("\n\ngoroutine ")
    held = ["goroutine " + b[:1500] for b in blocks if "_test.go:" in b]
    return (f"{key}: did not finish\n{head[:800]}\n" + "\n".join(held))[:12000] + "\n"


def vitest(pkg: pathlib.Path, env: dict, units: Units) -> tuple[bool, str]:
    """The package's tests (its test script is `vitest run`) with the JSON reporter:
    skipped, pending and todo tests become UNEXECUTED units."""
    with tempfile.TemporaryDirectory(prefix="anvilkit-vitest-") as tmp:
        report = pathlib.Path(tmp) / "report.json"
        p = run([shutil.which("pnpm") or "pnpm", "exec", "vitest", "run", "--reporter=default", "--reporter=json", f"--outputFile.json={report}"], pkg, env)
        if not report.exists():
            return False, "no Vitest JSON report was written\n" + p.stdout[-2000:] + p.stderr[-2000:]
        doc = json.loads(report.read_text(encoding="utf-8"))
    for f in doc.get("testResults", []):
        for a in f.get("assertionResults", []):
            if a.get("status") in ("skipped", "pending", "todo", "disabled"):
                units.add(f"{pkg.relative_to(ROOT)} {pathlib.Path(f.get('name', '')).name} > {a.get('fullName')}", f"vitest {a.get('status')}")
    if p.returncode != 0 or doc.get("numFailedTests") or doc.get("numFailedTestSuites"):
        return False, p.stdout[-3000:] + p.stderr[-1500:]
    if not doc.get("numTotalTests"):
        units.add(str(pkg.relative_to(ROOT)), "no test ran")
    return True, ""


def step_go(static: bool, units: Units) -> tuple[str, str]:
    env = test_env()
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
        # -o os.devnull: a module whose ./... is a single main package (tests/load, the
        # integration fixture) would otherwise leave its executable in the source tree,
        # which tools/check-source-export.py rightly refuses under an input directory.
        for cmd in (["go", "build", "-o", os.devnull, "./..."], ["go", "vet", "./..."]):
            p = run(cmd, mod, env)
            out.append(f"{mod.relative_to(ROOT)}: {' '.join(cmd[:2])} -> {'ok' if p.returncode == 0 else 'FAIL'}")
            if p.returncode != 0:
                return "FAIL", "\n".join(out + [p.stdout[-2000:], p.stderr[-2000:]])
        before = len(units.items)
        ok, detail = go_test(mod, env, units)
        out.append(f"{mod.relative_to(ROOT)}: go test -> {'ok' if ok else 'FAIL'}" + (f" ({len(units.items) - before} skipped)" if len(units.items) > before else ""))
        if not ok:
            return "FAIL", "\n".join(out + [detail])
    if static:
        out.append("--static: ANVILKIT_SKIP_DOCKER_TESTS=1, the Docker-backed tests are omitted")
    return ("PARTIAL" if units.items or static else "PASS"), "\n".join(out)


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


def step_ts(units: Units) -> tuple[str, str]:
    if outcome := locked_install():
        return outcome
    pnpm = shutil.which("pnpm")
    out = ["contracts: pnpm install --frozen-lockfile -> ok"]
    return scripts_then_vitest(TS_PKG, [("check-types", [pnpm, "run", "check-types"]), ("lint", [pnpm, "run", "lint"])], test_env(), units, out)


def scripts_then_vitest(pkg: pathlib.Path, scripts: list[tuple[str, list[str]]], env: dict, units: Units, out: list[str] | None = None) -> tuple[str, str]:
    """The package's checks in order, then its Vitest suite with the JSON reporter;
    PARTIAL when a test skipped."""
    out = out or []
    for name, cmd in scripts:
        p = run(cmd, pkg, env)
        out.append(f"{pkg.relative_to(ROOT)}: pnpm {name} -> {'ok' if p.returncode == 0 else 'FAIL'}")
        if p.returncode != 0:
            return "FAIL", "\n".join(out + [p.stdout[-2000:], p.stderr[-2000:]])
    before = len(units.items)
    ok, detail = vitest(pkg, env, units)
    out.append(f"{pkg.relative_to(ROOT)}: vitest -> {'ok' if ok else 'FAIL'}" + (f" ({len(units.items) - before} skipped)" if len(units.items) > before else ""))
    if not ok:
        return "FAIL", "\n".join(out + [detail])
    return ("PARTIAL" if len(units.items) > before else "PASS"), "\n".join(out)


VALIDATOR_PKG = ROOT / "jobs" / "validator"


def step_validator(units: Units) -> tuple[str, str]:
    """The validator Job package (P10): its locked install (no lifecycle script),
    type check, lint, profile digests and toolchain, the compiled dist the
    integration step's host-process scenario runs, and the tests (the build,
    SSR and Chromium steps run real processes)."""
    pnpm = shutil.which("pnpm")
    if not pnpm or not shutil.which("node"):
        return "UNEXECUTED", "pnpm/node are not on PATH (Node.js 24 LTS and the packageManager of jobs/validator/package.json are required)"
    return scripts_then_vitest(VALIDATOR_PKG, [("install", [pnpm, "install", "--frozen-lockfile"]), ("check-types", [pnpm, "run", "check-types"]),
                                               ("lint", [pnpm, "run", "lint"]), ("profiles:check", [pnpm, "run", "profiles:check"]),
                                               ("build", [pnpm, "run", "build"])], test_env(), units)


MODEL_PROXY_PKG = ROOT / "services" / "agent" / "model-proxy"


def step_model_proxy(units: Units) -> tuple[str, str]:
    """The Model Proxy (P11): its locked install (no lifecycle script), type
    check, lint, build and tests; the contract document comes from the
    contracts checkout of this repository."""
    pnpm = shutil.which("pnpm")
    if not pnpm or not shutil.which("node"):
        return "UNEXECUTED", "pnpm/node are not on PATH (Node.js 24 LTS and the packageManager of services/agent/model-proxy/package.json are required)"
    env = test_env(dict(os.environ, ANVILKIT_MODEL_PROXY_CONTRACTS_DIR=str(CONTRACTS)))
    return scripts_then_vitest(MODEL_PROXY_PKG, [("install", [pnpm, "install", "--frozen-lockfile", "--ignore-scripts"]), ("check-types", [pnpm, "run", "check-types"]),
                                                 ("lint", [pnpm, "run", "lint"]), ("build", [pnpm, "run", "build"])], env, units)


TEAM_PKG = ROOT / "jobs" / "codegen" / "team"
SUPERVISOR_REPO = ROOT / "jobs" / "codegen" / "supervisor"


def step_team(units: Units) -> tuple[str, str]:
    """The codegen team package (P12, repository anvilkit-job-codegen-team):
    its locked install (better-sqlite3's prebuilt binary is the one lifecycle
    script its workspace file allows), type check, lint, build (the coder and
    coordinator the tests and the integration scenario spawn), the reviewed
    tools document checked against the build, the tests (a countable sidecar
    double; the coordinator over the process protocol; the validation test
    runs the real validator chain from jobs/validator when it is built, and
    must then, ANVILKIT_REQUIRE_VALIDATOR), and the process protocol copies of
    both codegen repositories against contracts/jobs/codegen (the
    supervisor's Go tests run in the go step)."""
    pnpm = shutil.which("pnpm")
    if not pnpm or not shutil.which("node"):
        return "UNEXECUTED", "pnpm/node are not on PATH (Node.js 24 LTS and the packageManager of jobs/codegen/team/package.json are required)"
    env = test_env(dict(os.environ, ANVILKIT_CODEGEN_TEAM_CONTRACTS_DIR=str(CONTRACTS)))
    out = []
    for repo in (SUPERVISOR_REPO, TEAM_PKG):
        p = run(["sh", "tools/sync-protocol.sh", "--check", str(CONTRACTS)], repo, env)
        out.append(f"{repo.relative_to(ROOT)}: protocol copy -> {'ok' if p.returncode == 0 else 'FAIL'}")
        if p.returncode != 0:
            return "FAIL", "\n".join(out + [p.stdout[-2000:], p.stderr[-2000:]])
    if "ANVILKIT_REQUIRE_VALIDATOR" not in env:
        out.append("jobs/validator is not built: the team's validation cases may skip (ANVILKIT_REQUIRE_VALIDATOR unset)")
    return scripts_then_vitest(TEAM_PKG, [("install", [pnpm, "install", "--frozen-lockfile"]), ("check-types", [pnpm, "run", "check-types"]),
                                          ("lint", [pnpm, "run", "lint"]), ("build", [pnpm, "run", "build"]), ("tools:check", [pnpm, "run", "tools:check"])],
                               env, units, out)


def step_python(units: Units) -> tuple[str, str]:
    p = run([PY, "-m", "pytest", "-q", "-rs"], PY_PKG, test_env())
    for m in re.finditer(r"^SKIPPED \[\d+\] (.+?): (.*)$", p.stdout, flags=re.M):
        units.add(f"{PY_PKG.relative_to(ROOT)} {m.group(1)}", m.group(2)[:200])
    detail = f"{PY_PKG.relative_to(ROOT)}: pytest -> {'ok' if p.returncode == 0 else 'FAIL'}\n{p.stdout[-1500:]}"
    if p.returncode != 0:
        return "FAIL", detail
    return ("PARTIAL" if re.search(r"\b[1-9][0-9]* skipped\b", p.stdout) else "PASS"), detail


KNOWLEDGE_PKG = ROOT / "services" / "agent" / "knowledge"
BACKGROUND_WORKER_PKG = ROOT / "services" / "agent" / "background-worker"
PROFILE_SCHEMAS = ROOT / "packages" / "profile-schemas"


def pnpm_package(rel: pathlib.Path, env: dict, units: Units, static: bool) -> tuple[str, str]:
    """The locked install (no lifecycle script), type check, lint and build of one
    TypeScript package outside the workspace, then its tests unless --static (Docker-
    backed Testcontainers): under --static the step is PARTIAL, naming the omitted tests."""
    pnpm = shutil.which("pnpm")
    if not pnpm or not shutil.which("node"):
        return "UNEXECUTED", f"pnpm/node are not on PATH (Node.js 24 LTS and the packageManager of {rel}/package.json are required)"
    scripts = [("install", [pnpm, "install", "--frozen-lockfile", "--ignore-scripts"])] + [(n, [pnpm, "run", n]) for n in ("check-types", "lint", "build")]
    if static:
        out = []
        for name, cmd in scripts:
            p = run(cmd, ROOT / rel, env)
            out.append(f"{rel}: pnpm {name} -> {'ok' if p.returncode == 0 else 'FAIL'}")
            if p.returncode != 0:
                return "FAIL", "\n".join(out + [p.stdout[-2000:], p.stderr[-2000:]])
        out.append(f"omitted under --static: the {rel} test suite (Testcontainers)")
        units.add(str(rel), "test suite omitted under --static (Docker-backed Testcontainers)")
        return "PARTIAL", "\n".join(out)
    if not shutil.which("docker"):
        return "UNEXECUTED", f"docker is not on PATH; the {rel} tests need Testcontainers (use --static for the build alone)"
    return scripts_then_vitest(ROOT / rel, scripts, test_env(env), units)


def step_knowledge(static: bool, units: Units) -> tuple[str, str]:
    """The Knowledge owner (P14-P17): type check, lint, build (the entries the
    forwarder test and the integration scenarios spawn, since P17 also the
    Store's vendor migration and memoryctl) and tests (Vitest; Docker-backed
    PostgreSQL with the Store's vendor schema and, since P16, Qdrant through
    Testcontainers, so --static leaves the tests out and is PARTIAL). The Go
    forwarder sidecar module is covered by the go step."""
    return pnpm_package(pathlib.Path("services/agent/knowledge"), dict(os.environ), units, static)


def step_background_worker(static: bool, units: Units) -> tuple[str, str]:
    """The Background Worker and owner queue relay (P14): type check, lint,
    build and tests (Vitest; Docker-backed PostgreSQL, Valkey and NATS
    through Testcontainers, so --static leaves the tests out and is PARTIAL)."""
    env = dict(os.environ, ANVILKIT_BACKGROUND_WORKER_CONTRACTS_DIR=str(CONTRACTS))
    return pnpm_package(pathlib.Path("services/agent/background-worker"), env, units, static)


def step_profile_schemas() -> tuple[str, str]:
    """The shared configuration schemas (P14-06): the Python consumer example
    runs the cross-language fixture cases (the Go and TypeScript consumers
    run the same file in their own tests)."""
    p = run([PY, str(PROFILE_SCHEMAS / "python" / "config_generation.py"), "--fixtures", str(PROFILE_SCHEMAS / "fixtures.json")], PROFILE_SCHEMAS, dict(os.environ))
    detail = f"packages/profile-schemas: python fixtures -> {'ok' if p.returncode == 0 else 'FAIL'}\n{p.stdout[-1500:]}{p.stderr[-800:]}"
    return ("PASS" if p.returncode == 0 else "FAIL"), detail


def python_image_step(name: str, image: str, package: pathlib.Path, mounts: list[tuple[pathlib.Path, str]], env: dict[str, str], static: bool, units: Units) -> tuple[str, str]:
    """A Python package whose dependencies and weights are locked into its
    image: the checkout's sources and tests run inside that image with no
    network. Without Docker or the image (or under --static) the sources are
    byte-compiled only and the tests are reported as not run."""
    compiled = run([PY, "-m", "compileall", "-q", str(package / "src"), str(package / "tests")], ROOT)
    if compiled.returncode != 0:
        return "FAIL", f"{name}: compileall failed\n{compiled.stdout[-1500:]}{compiled.stderr[-800:]}"
    if static:
        units.add(name, f"unittest omitted under --static (only byte-compiled; the tests run in {image})")
        return "PARTIAL", f"{name}: sources byte-compile; omitted under --static: the unittest suite in {image}"
    docker = shutil.which("docker")
    if not docker or run([docker, "image", "inspect", image], ROOT).returncode != 0:
        units.add(name, f"unittest not run: the image {image} is not built")
        return "PARTIAL", f"{name}: sources byte-compile; the image {image} is not built, so its unittest suite did not run"
    cmd = [docker, "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,size=512m", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges", "--entrypoint", "/opt/venv/bin/python", "-w", "/opt/anvilkit"]
    for src, dst in mounts:
        cmd += ["-v", f"{src}:{dst}:ro"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    p = run(cmd + [image, "-m", "unittest", "discover", "-s", "tests"], ROOT)
    tail = (p.stdout + p.stderr)[-1500:]
    if p.returncode != 0:
        return "FAIL", f"{name}: unittest in {image} -> FAIL\n{tail}"
    skipped = re.search(r"OK \(skipped=(\d+)\)", p.stdout + p.stderr)
    if skipped:
        units.add(name, f"{skipped.group(1)} unittest case(s) skipped in {image}")
        return "PARTIAL", f"{name}: unittest in {image} -> ok, {skipped.group(1)} skipped\n{tail}"
    return "PASS", f"{name}: unittest in {image} -> ok\n{tail}"


def step_parser(static: bool, units: Units) -> tuple[str, str]:
    pkg = ROOT / "jobs" / "parser"
    return python_image_step("jobs/parser", "anvilkit-parser:dev", pkg,
                             [(pkg / "src" / "anvilkit_parser", "/opt/anvilkit/src/anvilkit_parser"), (pkg / "tests", "/opt/anvilkit/tests"),
                              (CONTRACTS / "jobs" / "job.schema.json", "/opt/anvilkit/job.schema.json")], {}, static, units)


def step_inference(static: bool, units: Units) -> tuple[str, str]:
    pkg = ROOT / "services" / "agent" / "inference"
    return python_image_step("services/agent/inference", "anvilkit-agent-inference:dev", pkg,
                             [(pkg / "src" / "anvilkit_inference", "/opt/anvilkit/src/anvilkit_inference"), (pkg / "tests", "/opt/anvilkit/tests"),
                              (PY_PKG / "anvilkit_generated_clients", "/opt/anvilkit/src/anvilkit_generated_clients"),
                              (pkg / "models.lock", "/opt/models/models.lock")],
                             {"ANVILKIT_INFERENCE_MODELS_DIR": "/opt/models"}, static, units)


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


def step_integration(units: Units) -> tuple[str, str]:
    missing = [v for v in ("ANVILKIT_DEV_CONTROL_DSN", "ANVILKIT_DEV_TEMPORAL_ADDRESS", "KUBECONFIG") if not os.environ.get(v)]
    if missing:
        return "UNEXECUTED", "deploy/dev foundation not configured; missing " + ", ".join(missing)
    out = []
    before = len(units.items)
    for mod in workspace_modules():
        if not list(mod.rglob("*_integration_test.go")):
            continue
        env = test_env()
        if mod == ROOT / "services/agent/workflow":
            # The parent prepares the cross-service dependency of that repository's scenario.
            try:
                with prepared_control(env) as (env, note):
                    out.append(f"{mod.relative_to(ROOT)}: {note}")
                    ok, detail = go_test_tagged(mod, env, units)
            except RuntimeError as exc:
                return "FAIL", "\n".join(out + [str(exc)])
        else:
            ok, detail = go_test_tagged(mod, env, units)
        out.append(f"{mod.relative_to(ROOT)}: integration -> {'ok' if ok else 'FAIL'}")
        if not ok:
            return "FAIL", "\n".join(out + [detail])
    if not out:
        return "UNEXECUTED", "no integration tests present"
    return ("PARTIAL" if len(units.items) > before else "PASS"), "\n".join(out)


def go_test_tagged(mod: pathlib.Path, env: dict, units: Units) -> tuple[bool, str]:
    """go_test with the integration build tag."""
    return go_test(mod, dict(env, GOFLAGS=(env.get("GOFLAGS", "") + " -tags=integration").strip()), units)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", action="store_true")
    ap.add_argument("--only", action="append")
    a = ap.parse_args()

    units: dict[str, Units] = {}

    def u(name: str) -> Units:
        return units.setdefault(name, Units())

    steps = [
        ("docs", lambda: scripts([PY, "tools/docs-status.py", "--self-test"], [PY, "tools/check-docs.py"])),
        ("generate", step_generate),
        ("contracts", lambda: script([PY, "tools/check-contracts.py"])),
        ("evals", lambda: scripts([PY, "tools/run-evaluation.py", "--check"], [PY, "tools/run-evaluation.py", "--self-test"])),
        ("deployment", lambda: scripts([PY, "tools/check-deployment-lock.py", "--self-test"], [PY, "tools/release-artifacts.py", "--self-test"],
                                       [PY, "deploy/gitops/publish.py", "--self-test"], [PY, "deploy/qualification/record.py", "--self-test"],
                                       [PY, "deploy/qualification/check-dr.py", "--self-test"], [PY, "deploy/qualification/loadrun.py", "--self-test"],
                                       [PY, "deploy/qualification/soak.py", "--self-test"], [PY, "deploy/qualification/chaos.py", "--self-test"],
                                       [PY, "deploy/qualification/workflow-rollout.py", "--self-test"],
                                       [PY, "tools/check-model-routes.py", "--self-test"], [PY, "tools/check-model-routes.py"])),
        ("handover", lambda: scripts([PY, "deploy/handover/rehearse.py", "self-test"], [PY, "deploy/handover/handover.py", "--self-test"])),
        ("export", lambda: scripts([PY, "tools/check-source-export.py", "--self-test"], [PY, "tools/check-source-export.py"])),
        ("ts", lambda: step_ts(u("ts"))),
        ("validator", lambda: step_validator(u("validator"))),
        ("model-proxy", lambda: step_model_proxy(u("model-proxy"))),
        ("team", lambda: step_team(u("team"))),
        ("python", lambda: step_python(u("python"))),
        ("knowledge", lambda: step_knowledge(a.static, u("knowledge"))),
        ("background", lambda: step_background_worker(a.static, u("background"))),
        ("profiles", step_profile_schemas),
        ("parser", lambda: step_parser(a.static, u("parser"))),
        ("inference", lambda: step_inference(a.static, u("inference"))),
        ("go", lambda: step_go(a.static, u("go"))),
        ("integration", lambda: step_integration(u("integration"))),
    ]
    names = [n for n, _ in steps]
    if a.only:
        unknown = sorted(set(a.only) - set(names))
        if unknown:
            ap.error(f"unknown step(s): {', '.join(unknown)} (steps: {', '.join(names)})")
        if a.static and a.only == ["integration"]:
            ap.error("--static excludes the integration step; nothing would run")
    print(("root: ANVILKIT_REQUIRE_ROOT_TESTS=1 (root-only tests must run)" if IS_ROOT
           else "not root: root-only tests may skip; each skip is reported as an UNEXECUTED unit"))
    outcomes = []
    for name, fn in steps:
        if a.only and name not in a.only:
            continue
        if a.static and name == "integration":
            continue
        t0 = time.time()
        status, detail = fn()
        outcomes.append((name, status))
        skipped = units.get(name, Units()).items
        print(f"{status:<11} {name:<12} ({time.time() - t0:.1f}s)" + (f"  {len(skipped)} UNEXECUTED units" if skipped else ""))
        if status != "PASS":
            for line in detail.strip().splitlines()[-15:]:
                print(f"            | {line}")
        for item in skipped[:60]:
            print(f"            UNEXECUTED {item}")
        if len(skipped) > 60:
            print(f"            ... {len(skipped) - 60} more UNEXECUTED units")
    count = {s: [n for n, st in outcomes if st == s] for s in ("PASS", "PARTIAL", "FAIL", "UNEXECUTED")}
    n_units = sum(len(v.items) for v in units.values())
    print(f"\n{len(outcomes)} steps: {len(count['PASS'])} PASS, {len(count['PARTIAL'])} PARTIAL, {len(count['FAIL'])} FAIL, "
          f"{len(count['UNEXECUTED'])} UNEXECUTED; {n_units} UNEXECUTED units (skipped or omitted tests, never a pass)")
    for s in ("PARTIAL", "UNEXECUTED", "FAIL"):
        if count[s]:
            print(f"  {s.lower()}: " + ", ".join(count[s]))
    print("\nThese checks establish contract agreement and local behavior only; G-01..G-14 stay NOT_RUN "
          "until their complete real-environment evidence exists.")
    return 1 if count["FAIL"] else (2 if count["PARTIAL"] or count["UNEXECUTED"] else 0)


def script(cmd: list[str]) -> tuple[str, str]:
    """A checker with the 0/1/2 contract: 2 (UNEXECUTED, e.g. a wrong interpreter or a
    missing tool) is not a failure of the checkout and never a pass."""
    p = run(cmd, ROOT)
    status = "PASS" if p.returncode == 0 else ("UNEXECUTED" if p.returncode == 2 else "FAIL")
    return status, p.stdout + p.stderr


def scripts(*cmds: list[str]) -> tuple[str, str]:
    """Several checkers of one step, in order; the worst status wins."""
    order = {"PASS": 0, "UNEXECUTED": 1, "FAIL": 2}
    worst, details = "PASS", []
    for cmd in cmds:
        status, detail = script(cmd)
        details.append(f"{' '.join(cmd[1:])} -> {status}\n{detail[-1500:] if status != 'PASS' else ''}".rstrip())
        worst = max(worst, status, key=order.get)
    return worst, "\n".join(details)


if __name__ == "__main__":
    sys.exit(main())
