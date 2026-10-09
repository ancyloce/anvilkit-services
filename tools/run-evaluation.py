#!/usr/bin/env python3
"""P22 evaluator (delivery.md P22, "Evaluation evidence"): requirement traceability,
execution with raw evidence, repeated-trial statistics and the release-candidate
inventory handed to P23.

  .local/verification-venv/bin/python tools/run-evaluation.py --check
  . .local/dev/env.sh && .local/verification-venv/bin/python tools/run-evaluation.py --run
      [--case ID]... [--suite NAME]...
  .local/verification-venv/bin/python tools/run-evaluation.py --inventory

The registry is tests/evals/registry.json. --check fails when a M1, CAP or SEC
requirement has neither an executable case nor an explicit NOT_RUN entry, when a
case names a suite, test or gate that does not exist, or when an entry is
malformed. --run executes the selected cases one by one and writes, under
outputs/evaluations/<run>/ (ignored by Git): raw/<case>.log (every byte the
suite printed), report.json and report.md, and inventory.json. Every raw log
and trial report is bound by its sha256; the report binds the registry digest
and the inventory (repository revisions and working-tree digests, contract,
profile, lockfile and image digests, toolchains).

Statuses: a case is PASS, FAIL or NOT_RUN (its environment is missing, or its
test skipped itself; never a pass). A requirement's local status is FAIL when
any of its cases failed, NOT_RUN when none ran or any did not run, PASS only
when every mapped case ran and passed; its qualification status stays NOT_RUN
while any NOT_RUN entry names it. A gate closes only when no NOT_RUN entry names
it and every contributing case passed; synthetic fixtures and local placements
never close one by themselves. Trials are aggregated from the evaluators' own
reports: pass@k (a case passed in at least one of its k trials) and pass^k (in
all of them), failure classes and latency percentiles over every trial, none
selected or dropped. Monetary cost is reported only where a priced route exists
(none does: ENV-06).

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
        if not m:
            errs.append(f"case {cid}: maps no requirement")
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
    trace = reg.get("traceability", {})
    want = {r for r in expected if not r.startswith("SEC-")}
    if set(trace) != want:
        errs.append(f"traceability: must map exactly the M1 and CAP requirements to gates (missing {sorted(want - set(trace))})")
    for r, gs in trace.items():
        if not gs or not set(gs) <= GATES:
            errs.append(f"traceability {r}: names no or unknown gates {gs}")
    return errs


# ---- inventory ------------------------------------------------------------


REPOS = ["", "contracts", "services/agent/api", "services/agent/control", "services/agent/workflow", "services/agent/model-proxy",
         "services/agent/knowledge", "services/agent/mcp", "jobs/validator", "jobs/shared/access-sidecar"]


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


def release_reasons() -> list[str]:
    """What the newest P23 release manifest (tools/release-artifacts.py) says about the
    candidate's signed images, SBOMs and charts."""
    manifests = sorted((ROOT / "outputs" / "qualification").glob("*/release/release-manifest.json"))
    if not manifests:
        return ["no signed image, SBOM or published chart exists for any service (G-13/P23)"]
    m = json.loads(manifests[-1].read_text())
    rel = manifests[-1].relative_to(ROOT)
    if m.get("status") == "BLOCKED":
        return [f"the newest release manifest {rel} is BLOCKED: {len(m.get('findings', []))} findings (images and charts signed, SBOMs attested)"]
    return [] if m.get("status") == "SIGNED_DEVELOPMENT_ONLY" else [f"the newest release manifest {rel} has status {m.get('status')}"]


def inventory() -> dict:
    repos = [repo_state(ROOT / r) if r else repo_state(ROOT) for r in REPOS]
    if (studio_dir() / "package.json").exists():
        top = git(["rev-parse", "--show-toplevel"], studio_dir()).stdout.decode().strip()
        if top:
            repos.append(repo_state(pathlib.Path(top)))
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
    dirty = [r["path"] for r in repos if r.get("repository", True) is not False and not r.get("clean", False)]
    unpublished = sorted({v for v in go_pins.values() if v and not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", v)})
    described = git(["describe", "--tags", "--match", "go/v*"], ROOT / "contracts").stdout.decode().strip()
    tag = re.match(r"go/(v[0-9]+\.[0-9]+\.[0-9]+)(?:-([0-9]+)-g[0-9a-f]+)?$", described)
    contracts_release = {"describe": described, "latestGoTag": tag.group(1) if tag else None, "commitsAfterTag": int(tag.group(2) or 0) if tag else None}
    lagging = sorted(k for k, v in go_pins.items() if v and tag and (v != tag.group(1) or int(tag.group(2) or 0) > 0))
    return {
        "schemaVersion": 1,
        "recordedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "repositories": repos,
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
        "releaseCandidate": {
            "qualifiable": not dirty and not unpublished and not lagging,
            "reasons": ([f"uncommitted working trees: {', '.join(dirty)}"] if dirty else [])
            + ([f"consumers pin unreleased contracts versions: {', '.join(unpublished)}"] if unpublished else [])
            + ([f"contracts HEAD is {described}: the consumers' pins ({', '.join(lagging)}) do not name the contracts they are built and tested with in the workspace"] if lagging else [])
            + release_reasons(),
        },
    }


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


def run_case(c: dict, s: dict, raw_dir: pathlib.Path) -> dict:
    d = suite_dir(s)
    out = {"id": c["id"], "suite": c["suite"], "requirements": c.get("requirements", []), "caps": c.get("caps", []), "sec": c.get("sec", []),
           "gates": c.get("gates", []), "profiles": c.get("profiles", []), "budget": c["budget"]}
    if "foundation" in s.get("requires", []) and (why := foundation_ready()):
        return {**out, "status": "NOT_RUN", "reason": why}
    if "network-cluster" in s.get("requires", []) and not os.environ.get("ANVILKIT_NETWORK_KUBECONFIG"):
        return {**out, "status": "NOT_RUN", "reason": "ANVILKIT_NETWORK_KUBECONFIG not set: a cluster whose CNI enforces NetworkPolicy (Cilium) with the anvilkit-network chart installed"}
    if not d.exists():
        return {**out, "status": "NOT_RUN", "reason": f"{d} does not exist"}
    timeout = budget_seconds(c["budget"])
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
        argv, names = ["pnpm", "test"], []
    else:
        argv, names = list(s["argv"]), []
    t0 = time.time()
    env = dict(os.environ)
    prep = contextlib.nullcontext((env, ""))
    if s.get("prepare") == "control":
        # The cross-service dependency the verification chain prepares for
        # the Workflow repository's cluster scenario (the same helper).
        prep = verification_chain().prepared_control(env)
    try:
        with prep as (env, note):
            p = subprocess.run(argv, cwd=d, capture_output=True, timeout=timeout + 120, env=env)
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
    if s["runner"] != "go":
        return {**out, "status": "PASS" if rc == 0 else "FAIL", **({} if rc == 0 else {"reason": f"exit {rc}"})}
    tests: dict[str, dict] = {}
    notes: dict[str, list[str]] = {}
    pkg_fail = False
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        action, test = ev.get("Action"), ev.get("Test")
        if not test:
            pkg_fail = pkg_fail or action == "fail"
            continue
        top = test.split("/")[0]
        if action == "output" and "_test.go:" in (ev.get("Output") or ""):
            notes.setdefault(top, []).append(ev["Output"].strip()[:300])
        if "/" not in test and action in ("pass", "fail", "skip"):
            tests[test] = {"status": action, "elapsed": ev.get("Elapsed")}
    out["tests"] = tests
    if names:
        found = [tests.get(n, {}).get("status") for n in names]
        if any(f == "fail" for f in found):
            return {**out, "status": "FAIL", "reason": "a test failed"}
        if any(f is None for f in found):
            return {**out, "status": "FAIL", "reason": "a named test did not run (build failure or wrong selector)" if rc != 0 else "a named test was not reported"}
        if any(f == "skip" for f in found):
            said = [n for t in names for n in notes.get(t, [])]
            return {**out, "status": "NOT_RUN", "reason": "the test skipped itself: " + (said[-1] if said else "no reason printed")}
        return {**out, "status": "PASS"}
    return {**out, "status": "FAIL" if rc != 0 or pkg_fail else "PASS", **({"reason": f"exit {rc}"} if rc != 0 else {})}


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


def roll_up(reg: dict, results: list[dict]) -> tuple[dict, dict]:
    by_req: dict[str, dict] = {}
    all_reqs = [r for group in reg["requirements"].values() for r in group]
    for r in all_reqs:
        cases = [x for x in results if r in set(x["requirements"]) | set(x["caps"]) | set(x["sec"])]
        nrs = [n for n in reg["notRun"] if r in mapped(n)]
        statuses = [x["status"] for x in cases]
        if "FAIL" in statuses:
            local = "FAIL"
        elif not statuses or "NOT_RUN" in statuses:
            local = "NOT_RUN"
        else:
            local = "PASS"
        by_req[r] = {"local": local, "cases": {x["id"]: x["status"] for x in cases},
                     "gates": sorted(set(reg.get("traceability", {}).get(r, [])) | {g for x in cases for g in x["gates"]} | {g for n in nrs for g in n.get("gates", [])}),
                     "missing": sorted({e for n in nrs for e in n["missing"]}), "notRun": [n["id"] for n in nrs]}
    gates = {}
    for g in sorted(GATES):
        cases = [x for x in results if g in x["gates"]]
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--inventory", action="store_true")
    ap.add_argument("--case", action="append")
    ap.add_argument("--suite", action="append")
    ap.add_argument("--out", default=str(ROOT / "outputs" / "evaluations"))
    ap.add_argument("--registry", default=str(REGISTRY), help="an alternative registry (checker probes)")
    a = ap.parse_args()
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
