#!/usr/bin/env python3
"""P22 evaluator (delivery.md P22, "Evaluation evidence"): requirement traceability,
execution with raw evidence, repeated-trial statistics and the release-candidate
inventory handed to P23.

  .local/verification-venv/bin/python tools/run-evaluation.py --check
  . .local/dev/env.sh && .local/verification-venv/bin/python tools/run-evaluation.py --run
      [--case ID]... [--suite NAME]...
  .local/verification-venv/bin/python tools/run-evaluation.py --inventory
  .local/verification-venv/bin/python tools/run-evaluation.py --self-test
  .local/verification-venv/bin/python tools/run-evaluation.py --reclassify outputs/evaluations/<run>

The registry is tests/evals/registry.json. --check fails when a M1, CAP or SEC
requirement has neither an executable case nor an explicit NOT_RUN entry, when a
case names a suite, test or gate that does not exist, or when an entry is
malformed. --run executes the selected cases one by one and writes, under
outputs/evaluations/<run>/ (ignored by Git): raw/<case>.log (every byte the
suite printed), report.json and report.md, and inventory.json. Every raw log
and trial report is bound by its sha256; the report binds the registry digest
and the inventory (repository revisions and working-tree digests, contract,
profile, lockfile and image digests, toolchains).

Statuses: a case is PASS, FAIL or NOT_RUN (its environment is missing, or a test or
subtest it ran skipped itself; never a pass). Since P3.3 (plan 0004) every runner
reports through a machine-readable result: go suites through `go test -json`, where
any skipped test or subtest of the selection makes the case NOT_RUN with the skip
reasons and the MISSING_INPUT tokens the test printed; pnpm suites, and command
suites that declare `"report": "vitest-json"`, through Vitest's JSON reporter (a
skipped, pending or todo test, or no test at all, is NOT_RUN); command suites that
declare `"report": "exit-code"` are the project's own checkers with the 0/1/2
contract (2 = could not run), and a skip marker in their output still makes them
NOT_RUN. A requirement's local status is FAIL when any of its registered cases
failed, NOT_RUN when any did not run — including every registered case this run
did not select — and PASS only when every registered case mapped to it ran and
passed; its qualification status stays NOT_RUN while any NOT_RUN entry names it. A
gate closes only when no NOT_RUN entry names it and every registered case that
contributes to it ran in this run and passed; synthetic fixtures and local
placements never close one by themselves. The release candidate is qualifiable
only when no reason against it exists, a BLOCKED or otherwise unsigned release
included. Trials are aggregated from the evaluators' own reports: pass@k (a case
passed in at least one of its k trials) and pass^k (in all of them), failure
classes and latency percentiles over every trial, none selected or dropped.
Monetary cost is reported only where a priced route exists (none does: ENV-06).

--self-test proves those rules on synthetic inputs; --reclassify RUN_DIR applies
them to the raw logs of an earlier run and lists every status that changes.

Exit code 1 when a check or a case failed, 2 when something could not run
(NOT_RUN cases), 0 otherwise. Nothing is regenerated, deleted or published.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pyenv_check  # noqa: E402

pyenv_check.require("yaml")

ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "tests" / "evals" / "registry.json"
GATES = {f"G-{i:02d}" for i in range(1, 15)}
ENVS = {f"ENV-{i:02d}" for i in range(1, 11)}
RUNNERS = {"go", "pnpm", "command"}
REPORTS = {"vitest-json", "exit-code"}
# Environments a suite may require; a missing one makes its cases NOT_RUN with the reason.
REQUIRES = {"foundation", "network-cluster", "qualification-run"}
MISSING = re.compile(r"MISSING_INPUT (\S+?):")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
BUDGET = re.compile(r"^([1-9][0-9]*)(m|h)$")
ID = re.compile(r"^[A-Z][A-Z0-9-]{1,63}$")


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def studio_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("ANVILKIT_STUDIO_DIR") or ROOT.parent / "anvilkit-studio" / "apps" / "studio")


def suite_dir(suite: dict) -> pathlib.Path:
    d = suite["dir"]
    if d == "${ANVILKIT_STUDIO_DIR}":
        return studio_dir()
    return (ROOT / d).resolve()


def load_registry(path: pathlib.Path = REGISTRY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def mapped(entry: dict) -> set[str]:
    return set(entry.get("requirements", [])) | set(entry.get("caps", [])) | set(entry.get("sec", []))


# ---- check ----------------------------------------------------------------


def go_test_exists(directory: pathlib.Path, package: str, name: str) -> bool:
    base = directory / package.removesuffix("...").strip("./") if package not in ("", "./...") else directory
    pattern = re.compile(r"^func " + re.escape(name) + r"\(t \*testing\.T\)", re.M)
    for f in base.rglob("*_test.go"):
        if "node_modules" in f.parts:
            continue
        if pattern.search(f.read_text(encoding="utf-8", errors="replace")):
            return True
    return False


def check(reg: dict) -> list[str]:
    errs: list[str] = []
    if reg.get("schemaVersion") != 1 or not re.fullmatch(r"[1-9][0-9]*", str(reg.get("registryRevision", ""))):
        errs.append("registry: schemaVersion 1 and a registryRevision are required")
    required = {r for group in reg.get("requirements", {}).values() for r in group}
    expected = {f"M1-{i:02d}" for i in range(1, 18)} | {f"CAP-{i:02d}" for i in range(1, 8)} | {f"SEC-{i:02d}" for i in range(1, 13)}
    if required != expected:
        errs.append(f"registry: the requirement list must be exactly M1-01..17, CAP-01..07, SEC-01..12 (missing {sorted(expected - required)}, extra {sorted(required - expected)})")
    suites = reg.get("suites", {})
    for name, s in suites.items():
        if s.get("runner") not in RUNNERS:
            errs.append(f"suite {name}: runner must be one of {sorted(RUNNERS)}")
        if s.get("runner") == "command" and not s.get("argv"):
            errs.append(f"suite {name}: a command suite names its argv")
        if s.get("runner") == "command" and s.get("report") not in REPORTS:
            errs.append(f"suite {name}: a command suite declares its machine-readable result ({' or '.join(sorted(REPORTS))})")
        if unknown := set(s.get("requires", [])) - REQUIRES:
            errs.append(f"suite {name}: unknown requirement(s) {sorted(unknown)} (known: {sorted(REQUIRES)})")
    ids: set[str] = set()
    covered_exec: set[str] = set()
    covered_nr: set[str] = set()
    for c in reg.get("cases", []):
        cid = c.get("id", "?")
        if not ID.match(cid) or cid in ids:
            errs.append(f"case {cid}: id missing, malformed or repeated")
        ids.add(cid)
        s = suites.get(c.get("suite", ""))
        if s is None:
            errs.append(f"case {cid}: unknown suite {c.get('suite')}")
            continue
        m = mapped(c)
        if not m and not c.get("unmapped"):
            errs.append(f"case {cid}: maps no requirement (a supporting case states why in `unmapped`)")
        if m and c.get("unmapped"):
            errs.append(f"case {cid}: maps requirements and is declared unmapped")
        if not m <= expected:
            errs.append(f"case {cid}: unknown requirements {sorted(m - expected)}")
        covered_exec |= m
        if not set(c.get("gates", [])) <= GATES:
            errs.append(f"case {cid}: unknown gates {sorted(set(c.get('gates', [])) - GATES)}")
        if not BUDGET.match(c.get("budget", "")):
            errs.append(f"case {cid}: budget must be minutes or hours (e.g. 20m)")
        d = suite_dir(s)
        if s["runner"] == "go":
            for name in (c.get("run") or "").split("|"):
                if name and not go_test_exists(d, c.get("package", "./..."), name):
                    errs.append(f"case {cid}: no test {name} in {d.relative_to(ROOT.parent)}")
        if "trials" in c and not (isinstance(c["trials"].get("k"), int) and c["trials"]["k"] >= 1 and c["trials"].get("reports")):
            errs.append(f"case {cid}: trials name their report directory and k >= 1")
    for n in reg.get("notRun", []):
        nid = n.get("id", "?")
        if not ID.match(nid) or nid in ids:
            errs.append(f"notRun {nid}: id missing, malformed or repeated")
        ids.add(nid)
        m = mapped(n)
        if not m <= expected or not (m or n.get("gates")):
            errs.append(f"notRun {nid}: maps no known requirement or gate")
        if not set(n.get("gates", [])) <= GATES:
            errs.append(f"notRun {nid}: unknown gates")
        if not n.get("missing") or not set(n["missing"]) <= ENVS:
            errs.append(f"notRun {nid}: names the missing ENV inputs")
        if not n.get("reason"):
            errs.append(f"notRun {nid}: states its reason")
        covered_nr |= m
    for r in sorted(expected - covered_exec - covered_nr):
        errs.append(f"requirement {r}: neither an executable case nor a NOT_RUN entry")
    errs += check_assertions(reg, expected, covered_nr)
    trace = reg.get("traceability", {})
    want = {r for r in expected if not r.startswith("SEC-")}
    if set(trace) != want:
        errs.append(f"traceability: must map exactly the M1 and CAP requirements to gates (missing {sorted(want - set(trace))})")
    for r, gs in trace.items():
        if not gs or not set(gs) <= GATES:
            errs.append(f"traceability {r}: names no or unknown gates {gs}")
    return errs


def go_name(name: str) -> str:
    """A subtest name as `go test -json` reports it (spaces become underscores)."""
    return "/".join(part.replace(" ", "_") for part in name.split("/"))


def check_assertions(reg: dict, expected: set[str], covered_nr: set[str]) -> list[str]:
    """P3.4: every requirement maps to named assertions (a test plus its subtest path, a
    Vitest full name, or a checker case) or to a NOT_RUN entry; each assertion names an
    existing case and a file:line whose line defines it."""
    errs: list[str] = []
    assertions = reg.get("assertions")
    if assertions is None:
        return errs  # registries before revision 13 map whole cases
    cases = {c["id"]: c for c in reg.get("cases", [])}
    for r in sorted(expected):
        named = assertions.get(r) or []
        if not named and r not in covered_nr:
            errs.append(f"requirement {r}: no named assertion and no NOT_RUN entry")
        for a in named:
            c = cases.get(a.get("case", ""))
            if c is None:
                errs.append(f"requirement {r}: assertion {a.get('name')} names unknown case {a.get('case')}")
                continue
            if r not in mapped(c):
                errs.append(f"requirement {r}: assertion {a.get('name')} sits in case {c['id']}, which does not map {r}")
            names = [n for n in (c.get("run") or "").split("|") if n]
            top = (a.get("name") or "").split("/")[0]
            # A Playwright test runs inside its Go case (TestStudioWorkbench); its
            # name is the spec's, not a Go test of the selection.
            if reg["suites"][c["suite"]]["runner"] == "go" and a.get("runner") != "playwright" and names and top not in names:
                errs.append(f"requirement {r}: assertion {a['name']} is outside case {c['id']}'s selection {names}")
            loc = a.get("file", "")
            path, _, line = loc.rpartition(":")
            if path.startswith("${ANVILKIT_STUDIO_DIR}/"):
                # The Studio repository is outside this checkout: checked when present.
                target = studio_dir() / path.split("/", 1)[1]
                if not target.is_file():
                    continue
            elif path.startswith("${"):
                continue
            else:
                target = ROOT / path
            if not path or not line.isdigit() or not target.is_file():
                errs.append(f"requirement {r}: assertion {a.get('name')} cites {loc}, which does not exist")
                continue
            text = target.read_text(encoding="utf-8", errors="replace").splitlines()
            # A Vitest/Playwright full name carries its describe titles; `title` is
            # the test's own title as the cited line writes it.
            leaf = a.get("title") or (a.get("name") or "").split("/")[-1]
            here = text[int(line) - 1] if int(line) <= len(text) else ""
            if leaf.replace("_", " ") not in here.replace("_", " ") and leaf not in here:
                errs.append(f"requirement {r}: {loc} does not define {leaf!r}")
    return errs


def trace_table(reg: dict) -> int:
    """The reviewer listing (P3.4 AC1): per requirement its named assertions with file:line."""
    assertions = reg.get("assertions") or {}
    nrs = reg.get("notRun", [])
    for group in reg["requirements"].values():
        for r in group:
            print(f"{r}: {reg.get('scenarios', {}).get(r, '')}")
            for a in assertions.get(r, []):
                print(f"    {a['case']:<22} {a['name']}  ({a.get('file', '')})")
            for n in nrs:
                if r in mapped(n):
                    print(f"    NOT_RUN {n['id']}: missing {', '.join(n['missing'])}")
            if gap := reg.get("gaps", {}).get(r):
                print(f"    GAP {gap}")
    for c in reg.get("cases", []):
        if c.get("unmapped"):
            print(f"supporting {c['id']}: {c['unmapped']}")
    return 0


def assertion_status(a: dict, by_case: dict[str, dict], runner: str) -> str:
    """One named assertion's status in this run: the case's own status when it did not
    pass as a whole, else the reported outcome of exactly that test or subtest."""
    res = by_case.get(a["case"])
    if not res or res.get("status") == "NOT_RUN":
        return "NOT_RUN"
    if runner == "command" and "assertionResults" not in res:
        return res["status"]
    reported = (res.get("assertionResults") or {}).get(go_name(a["name"]) if runner == "go" else a["name"])
    if reported is None:
        return "FAIL" if res.get("status") == "FAIL" else "NOT_RUN"
    return {"pass": "PASS", "fail": "FAIL"}.get(reported, "NOT_RUN")


# ---- inventory ------------------------------------------------------------


def repos() -> list[str]:
    """The parent and every submodule it registers (P3.3: the release-candidate
    dirt check covers all of them, not a hand-kept list)."""
    p = subprocess.run(["git", "config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"], cwd=ROOT, capture_output=True, text=True)
    return [""] + sorted(line.split(None, 1)[1].strip() for line in p.stdout.splitlines() if line.strip())


def git(args: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True)


def repo_state(path: pathlib.Path) -> dict:
    head = git(["rev-parse", "HEAD"], path)
    if head.returncode != 0:
        return {"path": str(path.relative_to(ROOT.parent)), "repository": False}
    status = git(["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"], path).stdout
    diff = git(["diff", "HEAD", "--binary", "--ignore-submodules=all"], path).stdout
    untracked = [ln[3:] for ln in status.decode(errors="replace").splitlines() if ln.startswith("?? ")]
    h = hashlib.sha256(diff)
    for rel in sorted(untracked):
        f = path / rel
        if f.is_file():
            h.update(rel.encode() + b"\0")
            h.update(f.read_bytes())
    return {
        "path": str(path.relative_to(ROOT.parent)),
        "head": head.stdout.decode().strip(),
        "branch": git(["rev-parse", "--abbrev-ref", "HEAD"], path).stdout.decode().strip(),
        "clean": not status.strip(),
        "changedPaths": len(status.decode(errors="replace").splitlines()),
        "untrackedFiles": len(untracked),
        "workingTreeDigest": "sha256:" + h.hexdigest(),
    }


def tree_digest(base: pathlib.Path, patterns: list[str]) -> str:
    h = hashlib.sha256()
    files = sorted({p for pat in patterns for p in base.glob(pat) if p.is_file() and "node_modules" not in p.parts})
    for f in files:
        h.update(str(f.relative_to(base)).encode() + b"\0")
        h.update(f.read_bytes())
    return "sha256:" + h.hexdigest()


def tool_version(argv: list[str]) -> str:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return (p.stdout or p.stderr).strip().splitlines()[0] if p.returncode == 0 else "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def release_reasons(manifests: list[pathlib.Path] | None = None) -> list[str]:
    """What the newest P23 release manifest (tools/release-artifacts.py) says about the
    candidate's signed images, SBOMs and charts. Any status other than a signed one is a
    reason against qualification; a BLOCKED manifest also lists its findings (P3.3)."""
    if manifests is None:
        manifests = sorted((ROOT / "outputs" / "qualification").glob("*/release/release-manifest.json"))
    if not manifests:
        return ["no signed image, SBOM or published chart exists for any service (G-13/P23)"]
    m = json.loads(manifests[-1].read_text())
    rel = manifests[-1].relative_to(ROOT) if manifests[-1].is_relative_to(ROOT) else manifests[-1]
    status = m.get("status")
    if status == "BLOCKED":
        return [f"the newest release manifest {rel} is BLOCKED: {len(m.get('findings', []))} findings"]
    if status in ("SIGNED", "SIGNED_DEVELOPMENT_ONLY"):
        return [] if status == "SIGNED" else [f"the newest release manifest {rel} is SIGNED_DEVELOPMENT_ONLY (development signing key)"]
    return [f"the newest release manifest {rel} has status {status}"]


def inventory() -> dict:
    repos_ = [repo_state(ROOT / r) if r else repo_state(ROOT) for r in repos()]
    if (studio_dir() / "package.json").exists():
        top = git(["rev-parse", "--show-toplevel"], studio_dir()).stdout.decode().strip()
        if top:
            repos_.append(repo_state(pathlib.Path(top)))
    profiles = json.loads((ROOT / "contracts" / "jobs" / "profiles.json").read_text(encoding="utf-8"))["profiles"]
    job_profiles = [{"profileId": p["profileId"], "revision": p.get("revision"), "jobKind": p.get("jobKind"), "candidateCode": p.get("candidateCode"),
                     "runtimeClass": p.get("runtimeClass"), "image": p.get("image"), "sidecarImage": p.get("sidecarImage")} for p in profiles]
    component_profiles = {}
    for f in sorted((ROOT / "jobs" / "validator" / "profiles").glob("*.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        component_profiles[f.stem] = doc.get("profileDigest")
    go_pins = {}
    for mod in sorted(ROOT.glob("services/agent/*/go.mod")) + sorted(ROOT.glob("jobs/**/go.mod")) + sorted(ROOT.glob("tests/*/go.mod")):
        if "node_modules" in mod.parts:
            continue
        m = re.search(r"anvilkit-agent-contracts/go (\S+)", mod.read_text(encoding="utf-8"))
        go_pins[str(mod.parent.relative_to(ROOT))] = m.group(1) if m else None
    locks = {}
    for pat in ["**/pnpm-lock.yaml", "**/go.sum", "go.work.sum", "tools/requirements.txt"]:
        for f in sorted(ROOT.glob(pat)):
            if "node_modules" in f.parts or ".local" in f.parts or "docs" in f.parts:
                continue
            locks[str(f.relative_to(ROOT))] = sha256_file(f)
    migrations = {}
    for label, d in [("control", ROOT / "services/agent/control/internal/migrate/sql"),
                     ("knowledge", ROOT / "jobs/migration/internal/migrate/sql/knowledge"),
                     ("mcp", ROOT / "jobs/migration/internal/migrate/sql/mcp")]:
        files = sorted(p.name for p in d.glob("*.sql")) if d.exists() else []
        migrations[label] = {"latest": files[-1] if files else None, "count": len(files), "digest": tree_digest(d, ["*.sql"]) if files else None}
    dirty = [r["path"] for r in repos_ if r.get("repository", True) is not False and not r.get("clean", False)]
    unpublished = sorted({v for v in go_pins.values() if v and not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", v)})
    described = git(["describe", "--tags", "--match", "go/v*"], ROOT / "contracts").stdout.decode().strip()
    tag = re.match(r"go/(v[0-9]+\.[0-9]+\.[0-9]+)(?:-([0-9]+)-g[0-9a-f]+)?$", described)
    contracts_release = {"describe": described, "latestGoTag": tag.group(1) if tag else None, "commitsAfterTag": int(tag.group(2) or 0) if tag else None}
    lagging = sorted(k for k, v in go_pins.items() if v and tag and (v != tag.group(1) or int(tag.group(2) or 0) > 0))
    return {
        "schemaVersion": 1,
        "recordedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "repositories": repos_,
        "contracts": {
            "sourceDigest": tree_digest(ROOT / "contracts", ["proto/**/*.proto", "openapi/*.yaml", "jobs/*.json", "components/*.json", "events/*.json"]),
            "generatedDigest": tree_digest(ROOT / "contracts", ["go/**/*.go", "ts/src/**/*.ts", "python/anvilkit_generated_clients/*.py"]),
            "goModulePins": go_pins,
            "release": contracts_release,
        },
        "migrations": migrations,
        "jobProfiles": job_profiles,
        "componentProfiles": component_profiles,
        "lockfiles": locks,
        "toolchains": {"go": tool_version(["go", "version"]), "node": tool_version(["node", "--version"]),
                       "pnpm": tool_version(["pnpm", "--version"]), "python": sys.version.split()[0],
                       "docker": tool_version(["docker", "version", "--format", "{{.Server.Version}}"])},
        "releaseCandidate": candidate(
            ([f"uncommitted working trees: {', '.join(dirty)}"] if dirty else [])
            + ([f"consumers pin unreleased contracts versions: {', '.join(unpublished)}"] if unpublished else [])
            + ([f"contracts HEAD is {described}: the consumers' pins ({', '.join(lagging)}) do not name the contracts they are built and tested with in the workspace"] if lagging else [])
            + release_reasons()),
    }


def candidate(reasons: list[str]) -> dict:
    """Qualifiable only when no reason against the candidate exists (P3.3: a BLOCKED
    release is one)."""
    return {"qualifiable": not reasons, "reasons": reasons}


# ---- run ------------------------------------------------------------------


def verification_chain():
    """tools/run-verification.py as a module (its prepared_control helper)."""
    spec = importlib.util.spec_from_file_location("run_verification", ROOT / "tools" / "run-verification.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def budget_seconds(b: str) -> int:
    n, unit = BUDGET.match(b).groups()
    return int(n) * (60 if unit == "m" else 3600)


def foundation_ready() -> str:
    missing = [v for v in ("ANVILKIT_DEV_CONTROL_DSN", "ANVILKIT_DEV_TEMPORAL_ADDRESS", "KUBECONFIG") if not os.environ.get(v)]
    return f"{', '.join(missing)} not set: source .local/dev/env.sh after deploy/dev/up.sh" if missing else ""


def classify_go(stdout: bytes, rc: int, names: list[str]) -> dict:
    """A `go test -json` stream: every test and subtest of the selection; any failure
    FAILs, any skip (a test or a subtest) is NOT_RUN with its printed reasons and the
    MISSING_INPUT tokens (P3.3)."""
    tests: dict[str, dict] = {}
    notes: dict[str, list[str]] = {}
    pkg_fail = False
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        action, test = ev.get("Action"), ev.get("Test")
        if not test:
            pkg_fail = pkg_fail or action == "fail"
            continue
        key = f"{ev.get('Package', '')}::{test}"
        if action == "output" and "_test.go:" in (ev.get("Output") or ""):
            notes.setdefault(key, []).append(ev["Output"].strip()[:300])
        if action in ("pass", "fail", "skip"):
            tests[key] = {"test": test, "status": action, "elapsed": ev.get("Elapsed")}
    selected = {k: v for k, v in tests.items() if not names or v["test"].split("/")[0] in names}
    out: dict = {"tests": {v["test"]: {"status": v["status"], "elapsed": v["elapsed"]} for v in selected.values() if "/" not in v["test"]},
                 # P3.4: every test and subtest outcome, the named assertions' evidence.
                 "assertionResults": {v["test"]: v["status"] for v in selected.values()}}
    skipped = {k: v for k, v in selected.items() if v["status"] == "skip"}
    if skipped:
        out["skipped"] = [{"test": v["test"], "reason": (notes.get(k) or ["no reason printed"])[-1]} for k, v in sorted(skipped.items())]
        out["missingInputs"] = sorted({m for k in skipped for n in notes.get(k, []) for m in MISSING.findall(n)})
    if any(v["status"] == "fail" for v in selected.values()):
        return {**out, "status": "FAIL", "reason": "a test failed"}
    if names:
        found = {v["test"] for v in selected.values()}
        if any(n not in found for n in names):
            return {**out, "status": "FAIL", "reason": "a named test did not run (build failure or wrong selector)" if rc != 0 else "a named test was not reported"}
    elif rc != 0 or pkg_fail:
        return {**out, "status": "FAIL", "reason": f"exit {rc}"}
    if not selected:
        return {**out, "status": "NOT_RUN", "reason": "no test ran"}
    if skipped:
        first = out["skipped"][0]
        more = f" (+{len(skipped) - 1} more)" if len(skipped) > 1 else ""
        return {**out, "status": "NOT_RUN", "reason": f"{first['test']} skipped: {first['reason']}{more}"}
    return {**out, "status": "PASS"}


def classify_vitest(report: dict | None, rc: int) -> dict:
    """Vitest's JSON reporter: failures FAIL; a skipped, pending or todo test, or no test
    at all, is NOT_RUN (P3.3)."""
    if report is None:
        return {"status": "FAIL" if rc != 0 else "NOT_RUN", "reason": f"exit {rc}" if rc != 0 else "no JSON report was written"}
    results = [a for f in report.get("testResults", []) for a in f.get("assertionResults", [])]
    failed = [a.get("fullName") for a in results if a.get("status") == "failed"]
    skipped = [a.get("fullName") for a in results if a.get("status") in ("skipped", "pending", "todo", "disabled")]
    counts = {"tests": len(results), "failed": len(failed), "skipped": len(skipped)}
    named = {a.get("fullName"): {"passed": "pass", "failed": "fail"}.get(a.get("status"), "skip") for a in results}
    if failed or rc != 0 or any(f.get("status") == "failed" for f in report.get("testResults", [])):
        return {"status": "FAIL", "reason": f"{len(failed)} failed: {failed[0] if failed else f'exit {rc}'}", "counts": counts, "assertionResults": named}
    if not results:
        return {"status": "NOT_RUN", "reason": "no test ran", "counts": counts}
    if skipped:
        return {"status": "NOT_RUN", "reason": f"{len(skipped)} skipped: {skipped[0]}", "counts": counts, "skipped": [{"test": t} for t in skipped],
                "missingInputs": sorted({m for t in skipped for m in MISSING.findall(t or "")}), "assertionResults": named}
    return {"status": "PASS", "counts": counts, "assertionResults": named}


SKIP_MARKERS = (re.compile(r"^\s*Tests\s.*\b(\d+) (skipped|todo)\b", re.M), re.compile(r"^--- SKIP: ", re.M),
                re.compile(r"\b(\d+) skipped\b"), re.compile(r"^UNEXECUTED\b", re.M))


def classify_text(raw: bytes, rc: int) -> dict:
    """A command checker with the 0/1/2 contract (0 all ran and passed, 1 failed, 2
    could not run); a skip marker in its output (Vitest/pytest summaries, go's
    --- SKIP, an UNEXECUTED line) still makes it NOT_RUN (P3.3)."""
    text = ANSI.sub("", raw.decode("utf-8", errors="replace"))
    if rc == 2:
        return {"status": "NOT_RUN", "reason": "the checker could not run (exit 2)"}
    if rc != 0:
        return {"status": "FAIL", "reason": f"exit {rc}"}
    for pat in SKIP_MARKERS:
        m = pat.search(text)
        if m and not (m.groups() and m.group(1) == "0"):
            line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end()) if text.find("\n", m.end()) != -1 else len(text)].strip()
            return {"status": "NOT_RUN", "reason": f"skip marker in the output: {line[:200]}", "missingInputs": sorted(set(MISSING.findall(text)))}
    return {"status": "PASS"}


def run_case(c: dict, s: dict, raw_dir: pathlib.Path) -> dict:
    d = suite_dir(s)
    out = {"id": c["id"], "suite": c["suite"], "requirements": c.get("requirements", []), "caps": c.get("caps", []), "sec": c.get("sec", []),
           "gates": c.get("gates", []), "profiles": c.get("profiles", []), "budget": c["budget"]}
    if "foundation" in s.get("requires", []) and (why := foundation_ready()):
        return {**out, "status": "NOT_RUN", "reason": why}
    if "network-cluster" in s.get("requires", []) and not os.environ.get("ANVILKIT_NETWORK_KUBECONFIG"):
        return {**out, "status": "NOT_RUN", "reason": "ANVILKIT_NETWORK_KUBECONFIG not set: a cluster whose CNI enforces NetworkPolicy (Cilium) with the anvilkit-network chart installed"}
    if "qualification-run" in s.get("requires", []) and not os.environ.get("ANVILKIT_QUALIFICATION_RUN"):
        return {**out, "status": "NOT_RUN", "reason": "ANVILKIT_QUALIFICATION_RUN not set: an operator-authorized run of the qualification environment "
                                                      "(it changes workloads and injects faults; the run id it records under)"}
    if not d.exists():
        return {**out, "status": "NOT_RUN", "reason": f"{d} does not exist"}
    timeout = budget_seconds(c["budget"])
    vitest_report = raw_dir / f"{c['id']}.vitest.json"
    names: list[str] = []
    if s["runner"] == "go":
        names = [n for n in (c.get("run") or "").split("|") if n]
        argv = ["go", "test", "-json", "-count=1", f"-timeout={timeout}s"]
        if s.get("tags"):
            argv.append(f"-tags={s['tags']}")
        if names:
            argv.append("-run=^(" + "|".join(names) + ")$")
        argv.append(c.get("package", "./..."))
    elif s["runner"] == "pnpm":
        if not shutil.which("pnpm"):
            return {**out, "status": "NOT_RUN", "reason": "pnpm is not on PATH"}
        # The packages' test script is `vitest run`; the JSON reporter is the result.
        argv = ["pnpm", "exec", "vitest", "run", "--reporter=default", "--reporter=json", f"--outputFile.json={vitest_report}"]
    else:
        # ${VAR} in an argv names an input of the environment (the qualification run id).
        argv = [os.path.expandvars(a) for a in s["argv"]]
        if s.get("report") == "vitest-json":
            argv += ["--reporter=default", "--reporter=json", f"--outputFile.json={vitest_report}"]
    t0 = time.time()
    env = dict(os.environ)
    prep = contextlib.nullcontext((env, ""))
    if s.get("prepare") == "control":
        # The cross-service dependency the verification chain prepares for
        # the Workflow repository's cluster scenario (the same helper).
        prep = verification_chain().prepared_control(env)
    stdout = b""
    try:
        with prep as (env, note):
            p = subprocess.run(argv, cwd=d, capture_output=True, timeout=timeout + 120, env=env)
        stdout = p.stdout
        raw, rc = (note.encode() + b"\n" if note else b"") + p.stdout + b"\n--- stderr ---\n" + p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        raw, rc = (e.stdout or b"") + b"\n--- timeout ---\n", -1
    except RuntimeError as e:
        return {**out, "status": "NOT_RUN", "reason": f"the prepared dependency could not start: {e}"}
    duration = round(time.time() - t0, 1)
    log = raw_dir / f"{c['id']}.log"
    log.write_bytes(raw)
    out.update({"argv": argv, "dir": str(d.relative_to(ROOT.parent)), "durationSeconds": duration, "rawLog": str(log.relative_to(ROOT)), "rawDigest": sha256_bytes(raw)})
    if rc == -1:
        return {**out, "status": "FAIL", "reason": f"exceeded the budget {c['budget']}"}
    if s["runner"] == "go":
        return {**out, **classify_go(stdout, rc, names)}
    if s["runner"] == "pnpm" or s.get("report") == "vitest-json":
        report = json.loads(vitest_report.read_text(encoding="utf-8")) if vitest_report.exists() else None
        if report is not None:
            out.update({"report": str(vitest_report.relative_to(ROOT)), "reportDigest": sha256_file(vitest_report)})
        return {**out, **classify_vitest(report, rc)}
    return {**out, **classify_text(raw, rc)}


def percentile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    i = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return round(xs[i], 1)


def trials_of(c: dict, started: float) -> dict | None:
    t = c.get("trials")
    if not t:
        return None
    d = ROOT / t["reports"]
    reports = sorted((f for f in d.glob("report-*.json") if f.stat().st_mtime >= started), key=lambda f: f.stat().st_mtime) if d.exists() else []
    if not reports:
        return {"status": "NOT_RUN", "reason": f"no report written to {t['reports']} by this run"}
    f = reports[-1]
    doc = json.loads(f.read_text(encoding="utf-8"))
    cases, classes, latencies, strata = [], {}, [], {}
    for case in doc.get("cases", []):
        trials = case.get("trials", [])
        passed = [not (tr.get("failureClasses") or []) for tr in trials]
        for tr in trials:
            for cl in tr.get("failureClasses") or []:
                classes[cl] = classes.get(cl, 0) + 1
            if isinstance(tr.get("latencyMs"), (int, float)):
                latencies.append(float(tr["latencyMs"]))
        k = len(trials)
        row = {"id": case.get("id"), "stratum": case.get("stratum"), "k": k, "passedTrials": sum(passed),
               "passAtK": any(passed), "passPowK": bool(passed) and all(passed)}
        cases.append(row)
        st = strata.setdefault(case.get("stratum") or "-", {"cases": 0, "passAtK": 0, "passPowK": 0})
        st["cases"] += 1
        st["passAtK"] += row["passAtK"]
        st["passPowK"] += row["passPowK"]
    n = len(cases)
    short = [r["id"] for r in cases if r["k"] != t["k"]]
    return {
        "report": str(f.relative_to(ROOT)), "reportDigest": sha256_file(f), "expectedK": t["k"], "cases": n,
        "trialsTotal": sum(r["k"] for r in cases), "casesWithOtherK": short,
        "passAtK": round(sum(r["passAtK"] for r in cases) / n, 4) if n else None,
        "passPowK": round(sum(r["passPowK"] for r in cases) / n, 4) if n else None,
        "failureClasses": dict(sorted(classes.items())), "latencyMs": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95)},
        "strata": strata, "perCase": cases,
        "monetaryCost": doc.get("monetaryCost", "not measured: no priced route (ENV-04/ENV-06)"),
    }


def effective_cases(reg: dict, results: list[dict]) -> list[dict]:
    """Every registered case with this run's result, or NOT_RUN when this run did not
    select it (P3.3: requirements and gates are judged over the full registry)."""
    by_id = {r["id"]: r for r in results}
    out = []
    for c in reg["cases"]:
        out.append(by_id.get(c["id"]) or {"id": c["id"], "suite": c["suite"], "requirements": c.get("requirements", []), "caps": c.get("caps", []),
                                           "sec": c.get("sec", []), "gates": c.get("gates", []), "status": "NOT_RUN", "reason": "not selected in this run"})
    return out


def roll_up(reg: dict, results: list[dict]) -> tuple[dict, dict]:
    cases_all = effective_cases(reg, results)
    by_req: dict[str, dict] = {}
    all_reqs = [r for group in reg["requirements"].values() for r in group]
    by_case = {x["id"]: x for x in cases_all}
    runners = {c["id"]: reg["suites"][c["suite"]]["runner"] for c in reg["cases"] if c["suite"] in reg.get("suites", {})}
    named = reg.get("assertions")
    for r in all_reqs:
        cases = [x for x in cases_all if r in set(x["requirements"]) | set(x["caps"]) | set(x["sec"])]
        nrs = [n for n in reg["notRun"] if r in mapped(n)]
        statuses = [x["status"] for x in cases]
        if named is not None:
            # P3.4: the requirement is judged by its named assertions, not by whole cases.
            statuses = [assertion_status(a, by_case, runners.get(a["case"], "go")) for a in named.get(r, [])]
        if "FAIL" in statuses:
            local = "FAIL"
        elif not statuses or "NOT_RUN" in statuses:
            local = "NOT_RUN"
        else:
            local = "PASS"
        by_req[r] = {"local": local, "cases": {x["id"]: x["status"] for x in cases},
                     **({"assertions": {a["name"]: assertion_status(a, by_case, runners.get(a["case"], "go")) for a in named.get(r, [])}} if named is not None else {}),
                     "gates": sorted(set(reg.get("traceability", {}).get(r, [])) | {g for x in cases for g in x["gates"]} | {g for n in nrs for g in n.get("gates", [])}),
                     "missing": sorted({e for n in nrs for e in n["missing"]}), "notRun": [n["id"] for n in nrs]}
    gates = {}
    for g in sorted(GATES):
        cases = [x for x in cases_all if g in x["gates"]]
        nrs = [n for n in reg["notRun"] if g in n.get("gates", [])]
        closable = bool(cases) and not nrs and all(x["status"] == "PASS" for x in cases)
        blockers = [n["id"] for n in nrs] + [f"{x['id']} {x['status']}" for x in cases if x["status"] != "PASS"]
        if not cases:
            blockers.append("no contributing case in the registry")
        gates[g] = {"status": "NOT_RUN" if not closable else "CLOSABLE_LOCALLY",
                    "contributingCases": {x["id"]: x["status"] for x in cases}, "blockedBy": blockers}
    # Qualification follows the runtime gates: a requirement is qualified only
    # when its local evidence passed and every gate it maps to is closable.
    for r, v in by_req.items():
        if v["local"] == "FAIL":
            v["qualification"] = "FAIL"
        elif v["local"] == "PASS" and v["gates"] and all(gates[g]["status"] == "CLOSABLE_LOCALLY" for g in v["gates"]):
            v["qualification"] = "PASS"
        else:
            v["qualification"] = "NOT_RUN"
    return by_req, gates


def markdown(report: dict) -> str:
    lines = [f"# Evaluation {report['run']}", "", f"Registry revision {report['registryRevision']} ({report['registryDigest']}); recorded {report['finishedAt']}.", "",
             "| Case | Suite | Status | Duration (s) | Raw evidence |", "|---|---|---|---|---|"]
    for c in report["cases"]:
        lines.append(f"| {c['id']} | {c['suite']} | {c['status']}{' — ' + c['reason'] if c.get('reason') else ''} | {c.get('durationSeconds', '')} | {c.get('rawDigest', '')} |")
    lines += ["", "| Requirement | Local | Qualification | Missing |", "|---|---|---|---|"]
    for r, v in report["requirements"].items():
        lines.append(f"| {r} | {v['local']} | {v['qualification']} | {', '.join(v['missing'])} |")
    lines += ["", "| Gate | Status | Blocked by |", "|---|---|---|"]
    for g, v in report["gates"].items():
        lines.append(f"| {g} | {v['status']} | {', '.join(v['blockedBy'])} |")
    for c in report["cases"]:
        if c.get("trials"):
            t = c["trials"]
            lines += ["", f"## Trials of {c['id']}", "", json.dumps({k: v for k, v in t.items() if k != "perCase"}, indent=2)]
    return "\n".join(lines) + "\n"


def self_test() -> int:
    """P3.3 AC3: the evaluator's negatives on synthetic inputs."""
    import tempfile

    def go_stream(*events: tuple) -> bytes:
        return b"\n".join(json.dumps({"Package": "example.com/p", **({"Test": t} if t else {}), "Action": a, **({"Output": o} if o else {})}).encode()
                           for t, a, o in events)

    sub_skip = go_stream(("TestA", "run", None), ("TestA/one", "run", None), ("TestA/one", "pass", None),
                         ("TestA/two", "output", "    x_test.go:10: MISSING_INPUT ANVILKIT_X: not set\n"), ("TestA/two", "skip", None),
                         ("TestA", "pass", None), (None, "pass", None))
    all_pass = go_stream(("TestA", "pass", None), ("TestA/one", "pass", None), (None, "pass", None))
    failing = go_stream(("TestA/one", "fail", None), ("TestA", "fail", None), (None, "fail", None))
    top_skip = go_stream(("TestB", "output", "    y_test.go:3: MISSING_INPUT root: needs root\n"), ("TestB", "skip", None), (None, "pass", None))

    def vitest(*statuses: str) -> dict:
        return {"testResults": [{"status": "passed", "assertionResults": [{"fullName": f"t{i}", "status": st} for i, st in enumerate(statuses)]}]}

    cases: list[tuple[str, bool]] = []
    r = classify_go(sub_skip, 0, ["TestA"])
    cases.append(("go: a passing test with one skipped subtest is NOT_RUN and names the missing input", r["status"] == "NOT_RUN" and r.get("missingInputs") == ["ANVILKIT_X"]))
    cases.append(("go: the whole-package run with a skipped test is NOT_RUN", classify_go(top_skip, 0, [])["status"] == "NOT_RUN"))
    cases.append(("go: every test and subtest passed is PASS", classify_go(all_pass, 0, ["TestA"])["status"] == "PASS"))
    cases.append(("go: a failed subtest is FAIL", classify_go(failing, 1, ["TestA"])["status"] == "FAIL"))
    cases.append(("go: a named test that was not reported is FAIL", classify_go(all_pass, 0, ["TestMissing"])["status"] == "FAIL"))
    cases.append(("vitest: one skipped test is NOT_RUN", classify_vitest(vitest("passed", "skipped"), 0)["status"] == "NOT_RUN"))
    cases.append(("vitest: a todo test is NOT_RUN", classify_vitest(vitest("passed", "todo"), 0)["status"] == "NOT_RUN"))
    cases.append(("vitest: no test at all is NOT_RUN", classify_vitest({"testResults": []}, 0)["status"] == "NOT_RUN"))
    cases.append(("vitest: a failed test is FAIL", classify_vitest(vitest("passed", "failed"), 1)["status"] == "FAIL"))
    cases.append(("vitest: every test passed is PASS", classify_vitest(vitest("passed", "passed"), 0)["status"] == "PASS"))
    cases.append(("vitest: exit 0 without a JSON report is NOT_RUN", classify_vitest(None, 0)["status"] == "NOT_RUN"))
    cases.append(("command: a skip summary with exit 0 is NOT_RUN", classify_text(b"      Tests  5 passed | 1 skipped (6)\n", 0)["status"] == "NOT_RUN"))
    cases.append(("command: exit 2 is NOT_RUN", classify_text(b"UNEXECUTED kyverno missing", 2)["status"] == "NOT_RUN"))
    cases.append(("command: exit 0 and no skip marker is PASS", classify_text(b"      Tests  5 passed (5)\npytest: 3 passed, 0 skipped\n", 0)["status"] == "PASS"))
    cases.append(("command: exit 1 is FAIL", classify_text(b"", 1)["status"] == "FAIL"))
    with tempfile.TemporaryDirectory(prefix="evaluation-selftest-") as tmp:
        blocked = pathlib.Path(tmp) / "release-manifest.json"
        blocked.write_text(json.dumps({"status": "BLOCKED", "findings": [{"id": "CVE-0000-0001"}]}))
        signed = pathlib.Path(tmp) / "signed.json"
        signed.write_text(json.dumps({"status": "SIGNED", "findings": []}))
        cases.append(("a BLOCKED release makes the candidate unqualifiable", candidate(release_reasons([blocked]))["qualifiable"] is False))
        cases.append(("a SIGNED release with no other reason is qualifiable", candidate(release_reasons([signed]))["qualifiable"] is True))
    reg = {"requirements": {"m1": ["M1-01"], "cap": [], "sec": []}, "traceability": {"M1-01": ["G-01"]}, "notRun": [],
           "cases": [{"id": "C-A", "suite": "x", "requirements": ["M1-01"], "gates": ["G-01"]},
                     {"id": "C-B", "suite": "x", "requirements": ["M1-01"], "gates": ["G-01"]}]}
    passed_a = {"id": "C-A", "suite": "x", "requirements": ["M1-01"], "caps": [], "sec": [], "gates": ["G-01"], "status": "PASS"}
    reqs, gates = roll_up(reg, [passed_a, {**passed_a, "id": "C-B", "status": "NOT_RUN"}])
    cases.append(("a requirement with a NOT_RUN case is not PASS", reqs["M1-01"]["local"] == "NOT_RUN"))
    reqs, gates = roll_up(reg, [passed_a])
    cases.append(("a gate is not closable from a selection that left one of its cases out", gates["G-01"]["status"] == "NOT_RUN" and "C-B NOT_RUN" in gates["G-01"]["blockedBy"]))
    cases.append(("a requirement is not PASS when one of its registered cases was not selected", reqs["M1-01"]["local"] == "NOT_RUN"))
    reqs, gates = roll_up(reg, [passed_a, {**passed_a, "id": "C-B"}])
    cases.append(("every registered case passed: the gate is closable locally", gates["G-01"]["status"] == "CLOSABLE_LOCALLY" and reqs["M1-01"]["local"] == "PASS"))
    # P3.4: requirements judged by named assertions.
    areg = {**reg, "suites": {"x": {"runner": "go", "dir": "."}},
            "assertions": {"M1-01": [{"case": "C-A", "name": "TestA/the scenario", "file": "tools/run-evaluation.py:1"}]}}
    ran = {**passed_a, "assertionResults": {"TestA": "pass", "TestA/the_scenario": "skip"}}
    reqs, _ = roll_up(areg, [ran, {**passed_a, "id": "C-B"}])
    cases.append(("a passing case whose named subtest skipped leaves the requirement NOT_RUN", reqs["M1-01"]["local"] == "NOT_RUN"))
    reqs, _ = roll_up(areg, [{**ran, "assertionResults": {"TestA": "fail", "TestA/the_scenario": "fail"}, "status": "FAIL"}, {**passed_a, "id": "C-B"}])
    cases.append(("a failed named subtest fails the requirement", reqs["M1-01"]["local"] == "FAIL"))
    reqs, _ = roll_up(areg, [{**ran, "assertionResults": {"TestA": "pass", "TestA/the_scenario": "pass"}}, {**passed_a, "id": "C-B"}])
    cases.append(("the named subtest passed: the requirement passes", reqs["M1-01"]["local"] == "PASS"))
    creg = {**areg, "cases": [{"id": "C-A", "suite": "x", "requirements": ["M1-01"], "gates": ["G-01"], "run": "TestA"}]}
    errs = check_assertions(creg, {"M1-01"}, set())
    cases.append(("an assertion whose file:line does not define it is a registry error", any("does not define" in e for e in errs)))
    creg["assertions"]["M1-01"][0]["name"] = "TestOther/x"
    cases.append(("an assertion outside its case's selection is a registry error", any("outside case" in e for e in check_assertions(creg, {"M1-01"}, set()))))
    bad = load_registry()
    bad["suites"] = {**bad["suites"], "bare": {"runner": "command", "dir": ".", "argv": ["true"]}}
    cases.append(("a command suite without a machine-readable result is a registry error", any("suite bare" in e for e in check(bad))))
    bad["suites"]["bare"] = {"runner": "command", "dir": ".", "argv": ["true"], "report": "exit-code", "requires": ["a-cluster-nobody-knows"]}
    cases.append(("an unknown suite requirement is a registry error", any("unknown requirement" in e for e in check(bad))))
    bad = load_registry()
    bad["cases"] = [*bad["cases"], {"id": "BARE-1", "suite": "contracts", "gates": ["G-01"], "budget": "1m"}]
    cases.append(("a case mapping nothing without an `unmapped` reason is a registry error", any("case BARE-1: maps no requirement" in e for e in check(bad))))
    bad["cases"][-1]["unmapped"] = "supporting evidence"
    cases.append(("a supporting case with its `unmapped` reason is accepted", not any("BARE-1" in e for e in check(bad))))
    treg = load_registry()
    first = next(a for named in treg["assertions"].values() for a in named if a.get("title") and not a["file"].startswith("${"))
    first["title"] = "a title the cited line does not hold"
    cases.append(("an assertion whose title is not on its cited line is a registry error", any("does not define 'a title the cited line" in e for e in check(treg))))
    saved = os.environ.pop("ANVILKIT_QUALIFICATION_RUN", None)
    try:
        gated = run_case({"id": "Q-1", "suite": "q", "budget": "1m"}, {"runner": "command", "dir": ".", "argv": ["true"], "report": "exit-code",
                                                                          "requires": ["qualification-run"]}, ROOT / "outputs")
    finally:
        if saved is not None:
            os.environ["ANVILKIT_QUALIFICATION_RUN"] = saved
    cases.append(("a qualification-run suite without an authorized run is NOT_RUN, never run", gated["status"] == "NOT_RUN" and "ANVILKIT_QUALIFICATION_RUN" in gated["reason"]))
    failures = [name for name, ok in cases if not ok]
    for name, ok in cases:
        print(f"{'PASS' if ok else 'FAIL':<5} self-test: {name}")
    print(f"evaluator self-test: {len(cases) - len(failures)}/{len(cases)} PASS")
    return 1 if failures else 0


def reclassify(run_dir: pathlib.Path, out_root: pathlib.Path) -> int:
    """P3.3 AC4: the classification rules applied to an earlier run's raw logs; every
    changed case, requirement and gate is listed (nothing in the earlier run changes)."""
    old = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    current = load_registry()
    reg = {"requirements": current["requirements"], "traceability": current.get("traceability", {}), "notRun": old.get("notRun", []),
           "cases": old["cases"]}
    new_cases = []
    for c in old["cases"]:
        if not c.get("rawLog"):
            new_cases.append({**c})
            continue
        raw = (ROOT / c["rawLog"]).read_bytes()
        reason = c.get("reason") or ""
        m = re.search(r"exit (-?\d+)", reason)
        rc = 0 if c["status"] == "PASS" else (int(m.group(1)) if m else 1)
        argv = c.get("argv") or []
        if argv[:2] == ["go", "test"]:
            sel = next((a for a in argv if a.startswith("-run=")), "")
            names = re.findall(r"[A-Za-z0-9_]+", sel[len("-run=^("):-len(")$")]) if sel else []
            stdout = raw.split(b"\n--- stderr ---\n")[0]
            verdict = classify_go(stdout, rc, names)
        else:
            verdict = classify_text(raw, rc)
        new_cases.append({**c, **{k: v for k, v in verdict.items() if k != "tests"}})
    old_req, old_gates = old["requirements"], old["gates"]
    new_req, new_gates = roll_up(reg, new_cases)
    changed = [{"id": n["id"], "before": o["status"], "after": n["status"], "reason": n.get("reason", "")}
               for o, n in zip(old["cases"], new_cases) if o["status"] != n["status"]]
    req_changed = [{"requirement": r, "before": old_req[r]["local"], "after": new_req[r]["local"]} for r in new_req if r in old_req and old_req[r]["local"] != new_req[r]["local"]]
    gate_changed = [{"gate": g, "before": old_gates[g]["status"], "after": new_gates[g]["status"]} for g in new_gates if g in old_gates and old_gates[g]["status"] != new_gates[g]["status"]]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = out_root / f"{stamp}-reclassify-{run_dir.name}"
    out.mkdir(parents=True, exist_ok=True)
    doc = {"schemaVersion": 1, "run": old["run"], "registryRevision": old["registryRevision"], "reclassifiedAt": stamp,
           "rules": "tools/run-evaluation.py P3.3 classification (subtest skips, skip markers, full-registry roll-up)",
           "cases": changed, "requirements": req_changed, "gates": gate_changed,
           "counts": {"before": {s: sum(1 for c in old["cases"] if c["status"] == s) for s in ("PASS", "FAIL", "NOT_RUN")},
                      "after": {s: sum(1 for c in new_cases if c["status"] == s) for s in ("PASS", "FAIL", "NOT_RUN")}}}
    (out / "reclassification.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Reclassification of evaluation {old['run']} (registry revision {old['registryRevision']})", "",
             f"Before: {doc['counts']['before']}; after: {doc['counts']['after']}.", "", "| Case | Before | After | Reason |", "|---|---|---|---|"]
    lines += [f"| {c['id']} | {c['before']} | {c['after']} | {c['reason']} |" for c in changed] or ["| (none) | | | |"]
    lines += ["", "| Requirement | Before | After |", "|---|---|---|"] + ([f"| {r['requirement']} | {r['before']} | {r['after']} |" for r in req_changed] or ["| (none) | | |"])
    lines += ["", "| Gate | Before | After |", "|---|---|---|"] + ([f"| {g['gate']} | {g['before']} | {g['after']} |" for g in gate_changed] or ["| (none) | | |"])
    (out / "reclassification.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwritten: {out}")
    silent = [c for c in changed if c["before"] == "PASS" and c["after"] == "FAIL"]
    if silent:
        print("FAIL a former PASS became FAIL from the same raw log: " + ", ".join(c["id"] for c in silent))
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--inventory", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--trace", action="store_true", help="list every requirement's named assertions with file:line (the reviewer's view)")
    mode.add_argument("--reclassify", metavar="RUN_DIR", help="apply the classification rules to an earlier run's raw logs")
    ap.add_argument("--case", action="append")
    ap.add_argument("--suite", action="append")
    ap.add_argument("--out", default=str(ROOT / "outputs" / "evaluations"))
    ap.add_argument("--registry", default=str(REGISTRY), help="an alternative registry (checker probes)")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.trace:
        return trace_table(load_registry(pathlib.Path(a.registry)))
    if a.reclassify:
        return reclassify(pathlib.Path(a.reclassify).resolve(), pathlib.Path(a.out))
    reg_path = pathlib.Path(a.registry)
    reg_digest = sha256_file(reg_path)  # bound at load: what this run actually executed
    reg = load_registry(reg_path)
    errs = check(reg)
    if a.check:
        for e in errs:
            print(f"FAIL {e}")
        print(f"registry: {len(reg['cases'])} cases, {len(reg['notRun'])} NOT_RUN entries, {len(errs)} failures")
        return 1 if errs else 0
    if errs:
        for e in errs:
            print(f"FAIL {e}")
        return 1
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = pathlib.Path(a.out) / run_id
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    inv = inventory()
    (out / "inventory.json").write_text(json.dumps(inv, indent=2) + "\n", encoding="utf-8")
    if a.inventory:
        print(f"inventory: {out / 'inventory.json'} ({sha256_file(out / 'inventory.json')})")
        print(f"release candidate qualifiable: {inv['releaseCandidate']['qualifiable']}")
        for r in inv["releaseCandidate"]["reasons"]:
            print(f"  - {r}")
        return 0
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    results = []
    for c in reg["cases"]:
        if a.case and c["id"] not in a.case:
            continue
        if a.suite and c["suite"] not in a.suite:
            continue
        t0 = time.time()
        r = run_case(c, reg["suites"][c["suite"]], raw_dir)
        if r["status"] != "NOT_RUN" and c.get("trials"):
            r["trials"] = trials_of(c, t0)
        results.append(r)
        print(f"{r['status']:<8} {c['id']:<22} {r.get('durationSeconds', ''):>8}  {r.get('reason', '')}", flush=True)
    by_req, gates = roll_up(reg, results)
    report = {"schemaVersion": 1, "run": run_id, "startedAt": started, "finishedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "registryRevision": reg["registryRevision"], "registryDigest": reg_digest, "inventoryDigest": sha256_file(out / "inventory.json"),
              "selection": {"cases": a.case or [], "suites": a.suite or []}, "cases": results, "requirements": by_req, "gates": gates,
              "notRun": reg["notRun"], "monetaryCost": "not measured: no priced provider or tool route exists (ENV-06)"}
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "report.md").write_text(markdown(report), encoding="utf-8")
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "FAIL", "NOT_RUN")}
    print(f"\n{len(results)} cases: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['NOT_RUN']} NOT_RUN; report {out / 'report.json'}")
    print("Gates: " + ", ".join(f"{g} {v['status']}" for g, v in gates.items()))
    return 1 if counts["FAIL"] else (2 if counts["NOT_RUN"] else 0)


if __name__ == "__main__":
    sys.exit(main())
