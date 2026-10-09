#!/usr/bin/env python3
"""The P23 qualification record (P23-11): every piece of evidence bound in one file.

  .local/verification-venv/bin/python deploy/qualification/record.py --run RUN [--earlier RUN ...]

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
    return str(path.relative_to(ROOT))


def find(runs: list[str], *parts: str) -> pathlib.Path | None:
    for run in runs:
        p = ROOT / "outputs/qualification" / run / pathlib.Path(*parts)
        if p.exists():
            return p
    return None


def evidence(runs: list[str], *parts: str) -> tuple[dict | None, dict]:
    p = find(runs, *parts)
    if not p:
        return None, {"status": "NOT_RUN", "reason": f"no {'/'.join(parts)} in runs {runs}"}
    return json.loads(p.read_text()), {"path": rel(p), "sha256": sha(p)}


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
        base = ROOT / "outputs/qualification" / run
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
        base = ROOT / "outputs/qualification" / run
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--earlier", action="append", default=[], help="earlier runs whose evidence this record also binds (newest first)")
    a = ap.parse_args()
    runs = [a.run, *a.earlier]
    manifest_path = ROOT / "outputs/qualification" / a.run / "release/release-manifest.json"
    if not manifest_path.exists():
        print(f"UNEXECUTED: {rel(manifest_path)} is missing")
        return 2
    record: dict = {"schemaVersion": 1, "recordedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "run": a.run,
                    "earlierRuns": a.earlier, "placement": "DEVELOPMENT_ONLY: kind anvilkit-qualification (3 control-plane + 3 workers) on one host; "
                                                         "zones qa-a/b/c are labels; the DR store shares the host"}

    # release candidate
    releases = []
    for run in runs:
        p = ROOT / "outputs/qualification" / run / "release/release-manifest.json"
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
    record["environment"] = {**ref, "environmentChart": (gitops or {}).get("environmentChart", {}).get("version"), "status": (gitops or {}).get("status", "NOT_RUN"),
                             "applications": len((gitops or {}).get("applications", []))}
    waves, ref = evidence(runs, "waves", "waves.json")
    record["waves"] = {**ref, "status": (waves or {}).get("status", "NOT_RUN"), "breakChecks": (waves or {}).get("breakChecks"),
                       "repairChecks": (waves or {}).get("repairChecks"), "repairSeconds": (waves or {}).get("repairSeconds")}
    adm, ref = evidence(runs, "admission", "admission.json")
    cases = (adm or {}).get("cases") or []
    record["admission"] = {**ref, "status": ("PASS" if all(c.get("pass") for c in cases) else "FAIL") if cases else "NOT_RUN",
                           "cases": {c["case"]: f"expected {c['expected']}, observed {c['observed']}" for c in cases}}
    tel, ref = evidence(runs, "telemetry", "telemetry.json")
    record["telemetry"] = {**ref, "status": ("PASS" if tel and not tel.get("findings") else "FAIL") if tel else "NOT_RUN",
                           "findings": (tel or {}).get("findings"), "counts": (tel or {}).get("counts"), "spanServices": (tel or {}).get("spanServices"),
                           "metricSources": (tel or {}).get("metricSources"), "attributesRemovedByCollector": (tel or {}).get("attributesRemovedByCollector")}

    # drills, restores, PITR, residency
    record["drills"] = {}
    for d in DRILLS:
        r, ref = evidence(runs, "drills", f"{d}.json")
        record["drills"][d] = {**ref, "status": checks_status((r or {}).get("checks")), "checks": (r or {}).get("checks"),
                               "window": (r or {}).get("window"), "durability": {k: v for k, v in ((r or {}).get("durability") or {}).items() if k != "lifecycles"},
                               "error": (r or {}).get("error")}
    record["restores"] = {}
    for d in RESTORES:
        r, ref = evidence(runs, "restores", f"{d}.json")
        record["restores"][d] = {**ref, "status": checks_status((r or {}).get("checks")), "checks": (r or {}).get("checks"),
                                 "restoreSeconds": (r or {}).get("restoreSeconds"), "error": (r or {}).get("error")}
    record["restores"]["mysql-apollo"] = {"status": "NOT_RUN", "reason": "Apollo/MySQL is not deployed in this environment (ENV-07/ENV-10 inputs)"}
    record["restores"]["ceph-rgw"] = {"status": "NOT_RUN", "reason": "no Ceph/RGW here: the in-cluster object store is a MinIO stand-in (ENV-02)"}
    record["pitr"] = pitr(runs)
    record["memoryPitr"] = memory_pitr(runs)
    res, ref = evidence(runs, "residency", "residency.json")
    record["residency"] = {**ref, "status": checks_status((res or {}).get("checks")), "coldStartToReadySeconds": (res or {}).get("coldStartToReadySeconds"),
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
    record["envInputs"] = {k: {"status": "REQUIRED", "scope": v} for k, v in ENV_INPUTS.items()}
    record["gates"] = {
        **{k: {"status": v.get("status"), "source": "P22 report"} for k, v in g.items() if k not in ("G-13", "G-14")},
        "G-13": g13(record),
        "G-14": {"status": "NOT_RUN", "blockedBy": ["ENV-01..10 absent: no actual topology, failure domains or SLO targets",
                                                    "DEVELOPMENT_ONLY placement: every node and the DR store share one host"]},
    }
    record["status"] = "DEVELOPMENT_ONLY (not qualified: G-13/G-14 NOT_RUN)"
    out = ROOT / "outputs/qualification" / a.run
    (out / "qualification-record.json").write_text(json.dumps(record, indent=2) + "\n")

    lines = [f"# P23 qualification record — run {a.run}", "", f"Status: **{record['status']}**. Placement: {record['placement']}.", "",
             "| Evidence | Status | Source |", "| --- | --- | --- |"]
    def row(name: str, r: dict) -> None:
        lines.append(f"| {name} | {r.get('status')} | `{r.get('path', r.get('reason', ''))}` |")
    for r in releases:
        lines.append(f"| release {r['run']} | {r['status']} ({r['images']} images, {r['charts']} charts, {len(r['blockingFindings'])} blocking findings) | `{r['path']}` |")
    row("deployment lock", {**record["deploymentLock"], "path": record["deploymentLock"]["lock"]})
    row("environment (GitOps)", record["environment"])
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
              "", "ENV-01..10: REQUIRED (none supplied)."]
    (out / "qualification-record.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nrecord: {rel(out / 'qualification-record.json')} {sha(out / 'qualification-record.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
