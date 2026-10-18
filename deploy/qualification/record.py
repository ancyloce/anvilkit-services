#!/usr/bin/env python3
"""The P23 qualification record (P23-11): every piece of evidence bound in one file.

  .local/verification-venv/bin/python deploy/qualification/record.py --run RUN [--earlier RUN ...] [--environment NAME]
  .local/verification-venv/bin/python deploy/qualification/record.py --self-test

Reads the evidence the P23 tools wrote under outputs/qualification/<RUN>/ (and, for
evidence not repeated there, under the --earlier runs, newest first): the release
manifests (tools/release-artifacts.py), the environment manifests (deploy/gitops/
publish.py), admission (check-admission.py), the wave-stop probe (check-waves.py),
redacted telemetry (check-telemetry.py), the rotation and failure drills (drills.py),
the restore drills (restore-drills.py), the PITR proof (pitr/test.jsonl from
`go test -json -run TestPointInTimeRecovery`), model residency (measure-residency.py),
plus the deployment lock's check and P22's release-candidate inventory. It writes
outputs/qualification/<RUN>/qualification-record.{json,md} with each source's path and
SHA-256, each check's result as recorded (never re-derived as a pass), the observed
SLO/RPO/RTO values of this placement, and the gates: G-13 and G-14 stay NOT_RUN while
ENV-01..10 are absent, whatever the drills showed; G-13 binds P24a's newest handover
rehearsal record (outputs/handover/<RUN>/handover-rehearsal.json) and carries its blockers. Exit 0 when the
record was written (its content states what passed), 2 when RUN has no release manifest.

Binding (P3.2): evidence counts for this record only when it was produced against this
run's release and environment: a drill or restore whose environmentRevision differs from
the environment chart this run published, or an admission/telemetry file of another run,
is STALE (with the revision it names), never PASS, even when it is found under --earlier.
Evidence that names no binding and was found only in an earlier run is STALE as well.
The record's status is derived (P3.8): QUALIFIED only when the environment is not a
development placement, every ENV-01..10 input is SUPPLIED and every gate G-01..G-14 is
PASS; NOT_QUALIFIED with the failing or missing items otherwise (DEVELOPMENT_ONLY for a
development placement). --self-test proves the binding and the derivation on synthetic
runs.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT_ROOT = ROOT / "outputs/qualification"
PY = str(ROOT / ".local/verification-venv/bin/python")
P22_INVENTORY = ROOT / "outputs/evaluations/20261001T051455Z/inventory.json"
P22_REPORT = ROOT / "outputs/evaluations/20261001T051455Z/report.json"
ENV_INPUTS = {
    "ENV-01": "machines, CPU/RAM/disks/NICs, OS/kernel/containerd",
    "ENV-02": "failure domains, network/power/storage dependencies, DR location",
    "ENV-03": "CIDRs, VIP, MetalLB, domains/certificates/egress rules",
    "ENV-04": "GPU/CPU inference profile, model weights and LICENSE",
    "ENV-05": "arrival rates, concurrency, document/source sizes",
    "ENV-06": "provider/model/tool prices, quotas and cost caps",
    "ENV-07": "IdP and real Pagix contracts",
    "ENV-08": "Studio host/page API revision, origins/CSP",
    "ENV-09": "RPO/RTO, retention, backup windows, deletion rules, drill frequency",
    "ENV-10": "on-call, alert routes, emergency access, key custody",
}
DRILLS = ["secret-rotation", "node-loss", "db-primary", "queue-writes", "broker", "storage", "control-plane"]
RESTORES = ["postgres-business", "postgres-temporal", "qdrant", "etcd", "openbao", "objects", "memory-removals"]


def sha(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: pathlib.Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def find(runs: list[str], *parts: str) -> pathlib.Path | None:
    for run in runs:
        p = OUT_ROOT / run / pathlib.Path(*parts)
        if p.exists():
            return p
    return None


BINDING: dict = {"run": None, "environmentRevision": None}


def staleness(doc: dict, found_in: str) -> str | None:
    """Why evidence does not belong to this record's release and environment (P3.2)."""
    rev, want_rev = doc.get("environmentRevision"), BINDING["environmentRevision"]
    if rev is not None:
        if want_rev is None:
            return f"produced against environment {rev}; this run published no environment chart"
        return None if rev == want_rev else f"produced against environment {rev}, this run's environment is {want_rev}"
    if doc.get("run") is not None:
        return None if doc["run"] == BINDING["run"] else f"produced by run {doc['run']}, not {BINDING['run']}"
    return None if found_in == BINDING["run"] else f"found under run {found_in} and bound to no release or environment"


def evidence(runs: list[str], *parts: str) -> tuple[dict | None, dict]:
    p = find(runs, *parts)
    if not p:
        return None, {"status": "NOT_RUN", "reason": f"no {'/'.join(parts)} in runs {runs}"}
    doc = json.loads(p.read_text())
    ref = {"path": rel(p), "sha256": sha(p)}
    why = staleness(doc, p.relative_to(OUT_ROOT).parts[0]) if isinstance(doc, dict) else None
    if why:
        ref.update({"stale": why})
    return doc, ref


def bound(status: str, ref: dict) -> str:
    """A result recorded against another release or environment is STALE, never PASS."""
    return "STALE" if ref.get("stale") and status not in ("NOT_RUN",) else status


def g13(record: dict) -> dict:
    """G-13 from P24a's handover rehearsal record (deploy/handover/rehearse.py record),
    the newest one: its evidence is bound here and its blockers carried as stated."""
    found = sorted(ROOT.glob("outputs/handover/*/handover-rehearsal.json"), key=lambda p: p.parent.name)
    if not found:
        return {"status": "NOT_RUN", "blockedBy": ["P24a clean-install/closure and transfer rehearsal evidence absent"]}
    h = json.loads(found[-1].read_text())
    record["p24a"] = {"path": rel(found[-1]), "sha256": sha(found[-1]), "status": h["p24a"]["status"], "exit": h["p24a"]["exit"],
                      "release": h["release"]["run"]}
    return {"status": "NOT_RUN", "rehearsal": rel(found[-1]), "blockedBy": h["gates"]["G-13"]["blockedBy"]}


def checks_status(checks: dict | None) -> str:
    if not checks:
        return "NOT_RUN"
    return "PASS" if all(v is True for v in checks.values()) else "FAIL"


def pitr(runs: list[str]) -> dict:
    """Every TestPointInTimeRecovery run kept under <run>/pitr* (go test -json
    files), grouped by directory: pitr holds the runs of the code as first
    qualified, a later pitr-<name> directory the runs after a fix. The status is
    the newest group's; the earlier groups stay in the record with their
    failures, so an intermittent past is never presented as a pass."""
    groups = []
    for run in runs:
        base = OUT_ROOT / run
        for d in sorted(base.glob("pitr*")) if base.exists() else []:
            if not d.is_dir():
                continue
            results = []
            for f in sorted(d.glob("*.jsonl")):
                if f.name == "test.jsonl" and any(x.name.startswith("run") for x in d.glob("run*.jsonl")):
                    continue  # a copy of one of the numbered runs
                if f.name == "neighbours.jsonl":
                    continue  # other scenarios run beside the fix
                final, cause, kept = None, None, None
                for line in f.read_text().splitlines():
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("Test") == "TestPointInTimeRecovery" and ev.get("Action") in ("pass", "fail", "skip"):
                        final = ev
                    out = ev.get("Output") or ""
                    if "did not reopen" in out:
                        cause = "recovery stayed RESTRICTED: " + out.strip()[:160]
                    elif "Error:" in out and cause is None:
                        cause = out.strip()[:160]
                    if "owner closes kept for the recovery:" in out:
                        kept = int(out.rsplit(":", 1)[1].strip().split()[0])
                results.append({"path": rel(f), "sha256": sha(f), "result": {"pass": "PASS", "fail": "FAIL", "skip": "NOT_RUN"}.get((final or {}).get("Action"), "NOT_RUN"),
                                "seconds": (final or {}).get("Elapsed"), "failure": cause if (final or {}).get("Action") == "fail" else None,
                                "ownerClosesKeptForTheRecovery": kept})
            if results:
                passed = sum(r["result"] == "PASS" for r in results)
                groups.append({"directory": rel(d), "passed": passed, "runs": len(results),
                               "status": "PASS" if passed == len(results) else ("INTERMITTENT" if passed else "FAIL"), "results": results})
    if not groups:
        return {"status": "NOT_RUN", "reason": "no TestPointInTimeRecovery evidence"}
    groups.sort(key=lambda g: (pathlib.Path(g["directory"]).name != "pitr", g["directory"]))  # first qualified, then fixes
    latest = groups[-1]
    return {"status": latest["status"], "passed": latest["passed"], "runs": latest["runs"], "current": latest["directory"], "groups": groups}


def memory_pitr(runs: list[str]) -> dict:
    """Every TestMemoryRemovalRecovery run kept under <run>/memory-pitr* (go test
    -json files): Knowledge's database recovered to a point before memory
    deletions and revocations, which must all be re-applied from the removal
    inventory before memory serves. Grouped like pitr(): memory-pitr holds the runs
    to a clock_timestamp() target, memory-pitr-fix those to a named restore point
    (the clock-step fix). The status is the newest group's."""
    groups = []
    for run in runs:
        base = OUT_ROOT / run
        for d in sorted(base.glob("memory-pitr*")) if base.exists() else []:
            if not d.is_dir():
                continue
            results = []
            for f in sorted(d.glob("*.jsonl")):
                final, subtests, cause = None, {}, None
                for line in f.read_text().splitlines():
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    name, action = ev.get("Test") or "", ev.get("Action")
                    if action in ("pass", "fail", "skip") and name == "TestMemoryRemovalRecovery":
                        final = ev
                    elif action in ("pass", "fail", "skip") and name.startswith("TestMemoryRemovalRecovery/"):
                        subtests[name.split("/", 1)[1]] = action.upper()
                    out = ev.get("Output") or ""
                    if "Error:" in out and cause is None:
                        cause = out.strip()[:160]
                action = (final or {}).get("Action")
                results.append({"path": rel(f), "sha256": sha(f), "result": {"pass": "PASS", "fail": "FAIL"}.get(action, "NOT_RUN"),
                                "seconds": (final or {}).get("Elapsed"), "subtests": subtests, "failure": cause if action == "fail" else None})
            if results:
                passed = sum(r["result"] == "PASS" for r in results)
                groups.append({"directory": rel(d), "passed": passed, "runs": len(results),
                               "status": "PASS" if passed == len(results) else ("INTERMITTENT" if passed else "FAIL"), "results": results})
    if not groups:
        return {"status": "NOT_RUN", "reason": "no TestMemoryRemovalRecovery evidence"}
    latest = groups[-1]
    return {"status": latest["status"], "passed": latest["passed"], "runs": latest["runs"], "current": latest["directory"], "groups": groups}


def lock_check() -> dict:
    p = subprocess.run([PY, str(ROOT / "tools/check-deployment-lock.py")], capture_output=True, text=True)
    return {"command": "tools/check-deployment-lock.py", "status": "PASS" if p.returncode == 0 else "FAIL",
            "summary": (p.stdout.strip().splitlines() or [""])[-1], "lock": rel(ROOT / "deploy/gitops/lock.yaml"), "lockSha256": sha(ROOT / "deploy/gitops/lock.yaml")}


def derive_status(record: dict, development: bool) -> str:
    """QUALIFIED only on a non-development placement with every ENV input SUPPLIED and
    every gate PASS (P3.8); otherwise the reasons, never a hard-coded verdict."""
    gates = record.get("gates") or {}
    missing_gates = sorted(g for g in (f"G-{i:02d}" for i in range(1, 15)) if (gates.get(g) or {}).get("status") != "PASS")
    missing_env = sorted(k for k, v in (record.get("envInputs") or {}).items() if v.get("status") != "SUPPLIED")
    if not development and not missing_gates and not missing_env and len(record.get("envInputs") or {}) == 10:
        return "QUALIFIED"
    reasons = ([f"gates not PASS: {', '.join(missing_gates)}"] if missing_gates else []) + ([f"ENV inputs not SUPPLIED: {', '.join(missing_env)}"] if missing_env else [])
    return f"{'DEVELOPMENT_ONLY' if development else 'NOT_QUALIFIED'} ({'; '.join(reasons) or 'development placement'})"


def self_test() -> int:
    """P3.2 AC3 and P3.8 AC6 on synthetic runs: evidence from another release or
    environment is STALE, never PASS; the status derives from gates and ENV inputs."""
    import tempfile
    global OUT_ROOT
    saved = OUT_ROOT
    cases = []
    with tempfile.TemporaryDirectory(prefix="record-selftest-") as tmp:
        OUT_ROOT = pathlib.Path(tmp)
        for run, revision in (("new", "0.1.0-qualification.new"), ("old", "0.1.0-qualification.old")):
            (OUT_ROOT / run / "drills").mkdir(parents=True)
            (OUT_ROOT / run / "drills" / "broker.json").write_text(json.dumps({"environmentRevision": revision, "checks": {"ok": True}}))
        (OUT_ROOT / "old" / "drills" / "storage.json").write_text(json.dumps({"environmentRevision": "0.1.0-qualification.old", "checks": {"ok": True}}))
        (OUT_ROOT / "old" / "admission").mkdir()
        (OUT_ROOT / "old" / "admission" / "admission.json").write_text(json.dumps({"run": "old", "cases": [{"case": "x", "pass": True}]}))
        (OUT_ROOT / "old" / "unbound.json").write_text(json.dumps({"checks": {"ok": True}}))
        BINDING.update({"run": "new", "environmentRevision": "0.1.0-qualification.new"})
        doc, ref = evidence(["new", "old"], "drills", "broker.json")
        cases.append(("evidence of this run's environment is PASS", bound(checks_status(doc.get("checks")), ref) == "PASS"))
        doc, ref = evidence(["new", "old"], "drills", "storage.json")
        cases.append(("a drill of an earlier environment revision is STALE", bound(checks_status(doc.get("checks")), ref) == "STALE"))
        doc, ref = evidence(["new", "old"], "admission", "admission.json")
        cases.append(("admission evidence of another run is STALE", bound("PASS", ref) == "STALE"))
        doc, ref = evidence(["new", "old"], "unbound.json")
        cases.append(("unbound evidence found only in an earlier run is STALE", bound("PASS", ref) == "STALE"))
        BINDING.update({"environmentRevision": None})
        doc, ref = evidence(["new"], "drills", "broker.json")
        cases.append(("a drill when this run published no environment is STALE", bound("PASS", ref) == "STALE"))
    OUT_ROOT = saved
    all_pass = {f"G-{i:02d}": {"status": "PASS"} for i in range(1, 15)}
    supplied = {f"ENV-{i:02d}": {"status": "SUPPLIED"} for i in range(1, 11)}
    cases.append(("every gate PASS and every input SUPPLIED on a production placement is QUALIFIED",
                  derive_status({"gates": all_pass, "envInputs": supplied}, False) == "QUALIFIED"))
    cases.append(("one gate NOT_RUN is NOT_QUALIFIED", derive_status({"gates": {**all_pass, "G-14": {"status": "NOT_RUN"}}, "envInputs": supplied}, False).startswith("NOT_QUALIFIED")))
    cases.append(("one input REQUIRED is NOT_QUALIFIED", derive_status({"gates": all_pass, "envInputs": {**supplied, "ENV-10": {"status": "REQUIRED"}}}, False).startswith("NOT_QUALIFIED")))
    cases.append(("a development placement is never QUALIFIED", derive_status({"gates": all_pass, "envInputs": supplied}, True).startswith("DEVELOPMENT_ONLY")))
    failures = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(f"{'PASS' if ok else 'FAIL':<5} self-test: {n}")
    print(f"record self-test: {len(cases) - len(failures)}/{len(cases)} PASS")
    return 1 if failures else 0


def env_inputs(environment: str) -> dict:
    """ENV-01..10 as the environment's inputs file states them (deploy/gitops/environments/
    <environment>.inputs.yaml: {ENV-nn: {status: SUPPLIED, evidence: ...}}), REQUIRED otherwise."""
    supplied = {}
    path = ROOT / "deploy/gitops/environments" / f"{environment}.inputs.yaml"
    if path.exists():
        import yaml
        supplied = yaml.safe_load(path.read_text()) or {}
    return {k: {"status": "SUPPLIED" if (supplied.get(k) or {}).get("status") == "SUPPLIED" and (supplied.get(k) or {}).get("evidence") else "REQUIRED",
                "scope": v, **({"evidence": supplied[k]["evidence"]} if (supplied.get(k) or {}).get("evidence") else {})} for k, v in ENV_INPUTS.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--earlier", action="append", default=[], help="earlier runs whose evidence this record also binds (newest first)")
    ap.add_argument("--environment", default="qualification", help="the combination the run published (deploy/gitops/environments/<name>.yaml)")
    ap.add_argument("--self-test", action="store_true", help="prove the binding and status rules on synthetic runs and exit")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.run:
        ap.error("--run is required")
    runs = [a.run, *a.earlier]
    manifest_path = OUT_ROOT / a.run / "release/release-manifest.json"
    if not manifest_path.exists():
        print(f"UNEXECUTED: {rel(manifest_path)} is missing")
        return 2
    import yaml
    combination = yaml.safe_load((ROOT / "deploy/gitops/environments" / f"{a.environment}.yaml").read_text())
    development = bool(combination.get("development"))
    published = OUT_ROOT / a.run / "gitops/environment-manifest.json"
    BINDING.update({"run": a.run, "environmentRevision": json.loads(published.read_text()).get("environmentChart", {}).get("version") if published.exists() else None})
    record: dict = {"schemaVersion": 1, "recordedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "run": a.run,
                    "earlierRuns": a.earlier, "environment": a.environment, "binding": dict(BINDING),
                    "placement": combination.get("placement") or ("DEVELOPMENT_ONLY: kind anvilkit-qualification (3 control-plane + 3 workers) on one host; "
                                                                  "zones qa-a/b/c are labels; the DR store shares the host" if development else "production")}

    # release candidate
    releases = []
    for run in runs:
        p = OUT_ROOT / run / "release/release-manifest.json"
        if p.exists():
            m = json.loads(p.read_text())
            releases.append({"run": run, "path": rel(p), "sha256": sha(p), "status": m.get("status"), "images": len(m.get("images", {})),
                             "charts": len(m.get("charts", {})), "blockingFindings": m.get("findings", [])})
    p22 = json.loads(P22_INVENTORY.read_text()) if P22_INVENTORY.exists() else None
    record["releaseCandidate"] = {
        "releases": releases,
        "p22Inventory": {"path": rel(P22_INVENTORY), "sha256": sha(P22_INVENTORY), "recordedAt": p22["recordedAt"],
                         "qualifiable": p22["releaseCandidate"].get("qualifiable"), "reasons": p22["releaseCandidate"].get("reasons")} if p22 else
                        {"status": "NOT_RUN", "reason": "P22 inventory missing"},
    }
    inv = find(runs, "inventory", "inventory.json")
    if inv and p22:
        cur = json.loads(inv.read_text())
        before = {r["path"]: r for r in p22["repositories"]}
        changed = [r["path"] for r in cur["repositories"] if r["path"] not in before
                   or (before[r["path"]]["head"], before[r["path"]]["workingTreeDigest"]) != (r["head"], r["workingTreeDigest"])]
        record["releaseCandidate"]["currentInventory"] = {"path": rel(inv), "sha256": sha(inv), "recordedAt": cur["recordedAt"],
                                                          "qualifiable": cur["releaseCandidate"].get("qualifiable"),
                                                          "repositoriesChangedSinceP22": changed}
    record["deploymentLock"] = lock_check()

    # environment
    gitops, ref = evidence(runs, "gitops", "environment-manifest.json")
    record["environmentPublication"] = {**ref, "environmentChart": (gitops or {}).get("environmentChart", {}).get("version"), "status": (gitops or {}).get("status", "NOT_RUN"),
                             "applications": len((gitops or {}).get("applications", []))}
    waves, ref = evidence(runs, "waves", "waves.json")
    record["waves"] = {**ref, "status": bound((waves or {}).get("status", "NOT_RUN"), ref), "breakChecks": (waves or {}).get("breakChecks"),
                       "repairChecks": (waves or {}).get("repairChecks"), "repairSeconds": (waves or {}).get("repairSeconds")}
    adm, ref = evidence(runs, "admission", "admission.json")
    cases = (adm or {}).get("cases") or []
    record["admission"] = {**ref, "status": bound(("PASS" if all(c.get("pass") for c in cases) else "FAIL") if cases else "NOT_RUN", ref),
                           "cases": {c["case"]: f"expected {c['expected']}, observed {c['observed']}" for c in cases}}
    tel, ref = evidence(runs, "telemetry", "telemetry.json")
    record["telemetry"] = {**ref, "status": bound(("PASS" if tel and not tel.get("findings") else "FAIL") if tel else "NOT_RUN", ref),
                           "findings": (tel or {}).get("findings"), "counts": (tel or {}).get("counts"), "spanServices": (tel or {}).get("spanServices"),
                           "metricSources": (tel or {}).get("metricSources"), "attributesRemovedByCollector": (tel or {}).get("attributesRemovedByCollector")}

    # drills, restores, PITR, residency
    record["drills"] = {}
    for d in DRILLS:
        r, ref = evidence(runs, "drills", f"{d}.json")
        record["drills"][d] = {**ref, "status": bound(checks_status((r or {}).get("checks")), ref), "checks": (r or {}).get("checks"),
                               "window": (r or {}).get("window"), "durability": {k: v for k, v in ((r or {}).get("durability") or {}).items() if k != "lifecycles"},
                               "error": (r or {}).get("error")}
    record["restores"] = {}
    for d in RESTORES:
        r, ref = evidence(runs, "restores", f"{d}.json")
        record["restores"][d] = {**ref, "status": bound(checks_status((r or {}).get("checks")), ref), "checks": (r or {}).get("checks"),
                                 "restoreSeconds": (r or {}).get("restoreSeconds"), "error": (r or {}).get("error")}
    record["restores"]["mysql-apollo"] = {"status": "NOT_RUN", "reason": "Apollo/MySQL is not deployed in this environment (ENV-07/ENV-10 inputs)"}
    record["restores"]["ceph-rgw"] = {"status": "NOT_RUN", "reason": "no Ceph/RGW here: the in-cluster object store is a MinIO stand-in (ENV-02)"}
    record["pitr"] = pitr(runs)
    record["memoryPitr"] = memory_pitr(runs)
    res, ref = evidence(runs, "residency", "residency.json")
    record["residency"] = {**ref, "status": bound(checks_status((res or {}).get("checks")), ref), "coldStartToReadySeconds": (res or {}).get("coldStartToReadySeconds"),
                           "memoryAfterLoad": (res or {}).get("memoryAfterLoad"), "withinChartRequest6GiB": (res or {}).get("withinChartRequest6GiB"),
                           "latencyMs": ((res or {}).get("load") or {}).get("latencyMs"), "restart": (res or {}).get("restart")}

    # observations (never targets: ENV-09 sets RPO/RTO)
    obs = {}
    for d, r in record["drills"].items():
        w = r.get("window") or {}
        if w:
            obs[d] = {"unavailableSeconds": w.get("unavailableSeconds"), "recoverySeconds": w.get("recoverySeconds"),
                      "failedProbes": w.get("failedProbes"), "lostAcceptedOperations": len((r.get("durability") or {}).get("lost") or [])}
    record["observations"] = {"drills": obs, "restoreSeconds": {d: r.get("restoreSeconds") for d, r in record["restores"].items() if r.get("restoreSeconds") is not None},
                              "note": "observations of this single-host placement; ENV-09 owns the RPO/RTO targets and ENV-05 the load profile"}

    # gates
    g = json.loads(P22_REPORT.read_text())["gates"] if P22_REPORT.exists() else {}
    record["envInputs"] = env_inputs(a.environment)
    record["gates"] = {
        **{k: {"status": v.get("status"), "source": "P22 report"} for k, v in g.items() if k not in ("G-13", "G-14")},
        "G-13": g13(record),
        "G-14": {"status": "NOT_RUN", "blockedBy": ["ENV-01..10 absent: no actual topology, failure domains or SLO targets",
                                                    "DEVELOPMENT_ONLY placement: every node and the DR store share one host"]},
    }
    record["status"] = derive_status(record, development)
    out = OUT_ROOT / a.run
    (out / "qualification-record.json").write_text(json.dumps(record, indent=2) + "\n")

    lines = [f"# P23 qualification record — run {a.run}", "", f"Status: **{record['status']}**. Placement: {record['placement']}.", "",
             "| Evidence | Status | Source |", "| --- | --- | --- |"]
    def row(name: str, r: dict) -> None:
        lines.append(f"| {name} | {r.get('status')} | `{r.get('path', r.get('reason', ''))}` |")
    for r in releases:
        lines.append(f"| release {r['run']} | {r['status']} ({r['images']} images, {r['charts']} charts, {len(r['blockingFindings'])} blocking findings) | `{r['path']}` |")
    row("deployment lock", {**record["deploymentLock"], "path": record["deploymentLock"]["lock"]})
    row("environment (GitOps)", record["environmentPublication"])
    row("sync waves stop / fix forward", record["waves"])
    row("signed-image admission", record["admission"])
    row("redacted telemetry", record["telemetry"])
    for d, r in record["drills"].items():
        row(f"drill {d}", r)
    for d, r in record["restores"].items():
        row(f"restore {d}", r)
    history = "; ".join(f"`{g['directory']}` {g['status']} {g['passed']}/{g['runs']}" for g in record["pitr"].get("groups", []))
    lines.append(f"| PITR, five obligation classes | {record['pitr'].get('status')} ({record['pitr'].get('passed', 0)}/{record['pitr'].get('runs', 0)} runs of the current code) | {history} |")
    mp = record["memoryPitr"]
    mhistory = "; ".join(f"`{g['directory']}` {g['status']} {g['passed']}/{g['runs']}" for g in mp.get("groups", [])) or mp.get("reason", "")
    lines.append(f"| PITR, memory deletion/revocation preservation | {mp.get('status')} ({mp.get('passed', 0)}/{mp.get('runs', 0)} runs of the current code) | {mhistory} |")
    row("model residency (two replicas)", record["residency"])
    lines += ["", "Gates: " + ", ".join(f"{k} {v['status']}" for k, v in sorted(record["gates"].items())),
              "", "ENV-01..10: " + ", ".join(f"{k} {v['status']}" for k, v in record["envInputs"].items()) + "."]
    (out / "qualification-record.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nrecord: {rel(out / 'qualification-record.json')} {sha(out / 'qualification-record.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
