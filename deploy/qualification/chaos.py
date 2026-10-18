#!/usr/bin/env python3
"""Chaos run of the qualification environment (P3.5 AC3).

  .local/verification-venv/bin/python deploy/qualification/chaos.py --run RUN [FAULT ...] \\
      [--profile tests/load/profiles/development.json] [--load-seconds 900] [--lead 60]
  .local/verification-venv/bin/python deploy/qualification/chaos.py --self-test

Each fault runs under its own load: the load tool (tests/load) at the profile's rates
for --load-seconds, the fault injected --lead seconds in, and the drills' business
probe (deploy/qualification/drills.py Load) for the availability window and the
durability check of every operation it submitted. Faults and their designed outcome:
  control-pod       force-delete one of the Control Pods: the Deployment's readiness
                    drops and returns; admissions on the lost Pod fail retryably
  model-proxy-pod   force-delete one of the Model Proxy Pods: its in-flight calls end
                    UNKNOWN and are reconciled (observed or dispositioned), never resent
  mcp-pod           force-delete one of the MCP Pods: its in-flight tool calls end
                    UNKNOWN and are reconciled, never resent
  postgresql        drills.py db-primary (CloudNativePG fails over)
  valkey            drills.py queue-writes (the queue primary lost; owners rebuild)
  nats              drills.py broker (JetStream loses quorum; the outbox drains after)
  object-store      drills.py storage (the inventory store paused: admission is
                    fenced, no operation admitted without its inventory record)
For every fault: no duplicate charge (no dispatch of the window with two `actual`
cost entries), no dispatch of the window past its deadline unobserved and without a
disposition after the settle time, every operation the probe got accepted exists once
and ends, no operation owns two Jobs, and the fault's own check (readiness dropped and
returned / failed over / fenced). Writes outputs/qualification/<RUN>/chaos/<fault>/
{chaos.json,summary.json,load.log}. It injects faults into the qualification cluster:
run it only with that environment operator's authorization. Exit 0 PASS, 1 FAIL, 2
PARTIAL or UNEXECUTED.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import drills  # noqa: E402
import loadrun  # noqa: E402

ROOT = loadrun.ROOT
POD_FAULTS = {"control-pod": "anvilkit-agent-control", "model-proxy-pod": "anvilkit-agent-model-proxy", "mcp-pod": "anvilkit-agent-mcp"}
DRILL_FAULTS = {"postgresql": "db-primary", "valkey": "queue-writes", "nats": "broker", "object-store": "storage"}
FAULTS = [*POD_FAULTS, *DRILL_FAULTS]


def ready_replicas(name: str) -> tuple[int, int]:
    d = json.loads(drills.k("-n", loadrun.APPS, "get", "deployment", name, "-o", "json"))
    return d["status"].get("readyReplicas", 0), d["spec"]["replicas"]


def pod_fault(deployment: str) -> dict:
    """Force-delete one Pod of the Deployment; watch its readiness drop and return."""
    sel = json.loads(drills.k("-n", loadrun.APPS, "get", "deployment", deployment, "-o", "json"))["spec"]["selector"]["matchLabels"]
    pods = json.loads(drills.k("-n", loadrun.APPS, "get", "pods", "-l", ",".join(f"{k}={v}" for k, v in sel.items()), "-o", "json"))["items"]
    victim = sorted(p["metadata"]["name"] for p in pods)[0]
    fault = time.time()
    drills.k("-n", loadrun.APPS, "delete", "pod", victim, "--grace-period=0", "--force", "--wait=false")
    dropped = drills.wait_until(lambda: ready_replicas(deployment)[0] < ready_replicas(deployment)[1], 60, 1)
    restored = drills.wait_until(lambda: ready_replicas(deployment)[0] == ready_replicas(deployment)[1], 600, 2)
    return {"deployment": deployment, "pod": victim, "faultAt": fault,
            "readinessDroppedAfter": round(dropped - fault, 1) if dropped else None,
            "readinessRestoredAfter": round(restored - fault, 1) if restored else None,
            "checks": {"readinessDropped": dropped is not None, "readinessRestored": restored is not None}}


def checks_of(fault_result: dict, durability: dict, led: dict[str, int]) -> dict[str, bool | None]:
    checks: dict[str, bool | None] = dict(fault_result.get("checks", {}))
    checks.update({
        "noDuplicateCharges": led["duplicateCharges"] == 0,
        "noUnresolvedPastDeadline": led["unresolvedPastDeadline"] == 0,
        "noLostAcceptedOperation": not durability.get("lost", ["unverified"]),
        "everyAcceptedOperationFinished": not durability.get("unfinished", ["unverified"]),
        "noOperationWithSeveralJobs": not durability.get("operationsWithSeveralJobs", ["unverified"]),
    })
    return checks


def one(fault: str, run: str, profile: dict, lead: int, settle: int, api: str, token: str, port: int) -> dict:
    out = ROOT / "outputs/qualification" / run / "chaos" / fault
    out.mkdir(parents=True, exist_ok=True)
    since = loadrun.utc_now()
    probe = drills.Load(token, port).start()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(profile, f)
    with loadrun.KubeProxy() as proxy:
        load = loadrun.start_load(pathlib.Path(f.name), f"{run}-{fault}", out, api, token, proxy.prometheus)
        time.sleep(lead)
        try:
            result = pod_fault(POD_FAULTS[fault]) if fault in POD_FAULTS else drills.DRILLS[DRILL_FAULTS[fault]](probe)
        except Exception as e:  # recorded, never hidden
            result = {"error": str(e), "checks": {"faultCompleted": False}}
        load.wait()
    probe.stop()
    until = loadrun.utc_now()
    durability = probe.verify()
    time.sleep(settle)  # deadlines of the window pass; reconciliation runs
    led = loadrun.ledger(since, until)
    summary = loadrun.load_summary(out)
    record = {"fault": fault, "since": since, "until": until, "faultResult": result, "durability": durability, "ledger": led,
              "loadStatus": (summary or {}).get("status"), "loadReasons": (summary or {}).get("reasons"),
              "placement": "DEVELOPMENT_ONLY: the kind qualification cluster on one host"}
    record["checks"] = checks_of(result, durability, led)
    record["status"] = loadrun.status_of(record["checks"])
    (out / "chaos.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def self_test() -> int:
    led = {"duplicateCharges": 0, "unresolvedPastDeadline": 0}
    ok_dur = {"lost": [], "unfinished": [], "operationsWithSeveralJobs": []}
    pod_ok = {"checks": {"readinessDropped": True, "readinessRestored": True}}
    cases = {
        "designed outcome": (checks_of(pod_ok, ok_dur, led), "PASS"),
        "no readiness drop": (checks_of({"checks": {"readinessDropped": False, "readinessRestored": True}}, ok_dur, led), "FAIL"),
        "duplicate charge": (checks_of(pod_ok, ok_dur, {**led, "duplicateCharges": 1}), "FAIL"),
        "unknown never reconciled": (checks_of(pod_ok, ok_dur, {**led, "unresolvedPastDeadline": 1}), "FAIL"),
        "lost operation": (checks_of(pod_ok, {**ok_dur, "lost": ["op"]}, led), "FAIL"),
        "unverified durability": (checks_of(pod_ok, {}, led), "FAIL"),
        "fault crashed": (checks_of({"error": "x", "checks": {"faultCompleted": False}}, ok_dur, led), "FAIL"),
    }
    failures = [name for name, (checks, want) in cases.items() if loadrun.status_of(checks) != want]
    if set(FAULTS) != {"control-pod", "model-proxy-pod", "mcp-pod", "postgresql", "valkey", "nats", "object-store"}:
        failures.append("fault list")
    if any(d not in drills.DRILLS for d in DRILL_FAULTS.values()):
        failures.append("a data-plane fault names no drill")
    print(json.dumps({"selfTest": "PASS" if not failures else "FAIL", "failures": failures}))
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("faults", nargs="*", help=f"faults to inject (default all): {', '.join(FAULTS)}")
    ap.add_argument("--profile", default=str(ROOT / "tests/load/profiles/development.json"))
    ap.add_argument("--load-seconds", type=int, default=900)
    ap.add_argument("--lead", type=int, default=60, help="seconds of load before the fault")
    ap.add_argument("--settle", type=int, default=300, help="seconds after the load before the ledger is read")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.run:
        ap.error("--run is required")
    unknown = sorted(set(a.faults) - set(FAULTS))
    if unknown:
        ap.error(f"unknown faults {unknown}; known: {', '.join(FAULTS)}")
    profile = json.loads(pathlib.Path(a.profile).read_text())
    profile["durationSeconds"] = a.load_seconds
    loadrun.build_load()
    api, token = loadrun.api_target()
    port = int(api.rsplit(":", 1)[1])
    results = {}
    try:
        for fault in a.faults or FAULTS:
            if not drills.wait_until(drills.apps_healthy, 600, 15):
                results[fault] = {"status": "UNEXECUTED", "reason": "the environment is not healthy before the fault"}
                continue
            r = one(fault, a.run, profile, a.lead, a.settle, api, token, port)
            results[fault] = {"status": r["status"], "checks": r["checks"]}
            print(f"{r['status']} {fault}: " + ", ".join(f"{c}={v}" for c, v in r["checks"].items()), flush=True)
    finally:
        drills.k("-n", loadrun.APPS, "delete", "service", "anvilkit-drill-api", "--ignore-not-found", check=False)
    statuses = {v["status"] for v in results.values()}
    (ROOT / "outputs/qualification" / a.run / "chaos" / "chaos.json").write_text(json.dumps(results, indent=2) + "\n")
    return 1 if "FAIL" in statuses else (0 if statuses == {"PASS"} else 2)


if __name__ == "__main__":
    sys.exit(main())
