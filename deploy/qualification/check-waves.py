#!/usr/bin/env python3
"""Sync-wave stop and fix-forward probe of the qualification environment (P23-04).

  .local/verification-venv/bin/python deploy/qualification/check-waves.py --run RUN --revision N [--observe 300]

Precondition: the environment converged (every Application Synced and Healthy) on a
revision of RUN; N is the next unpublished environment revision of RUN.
  1. break: publishes revision N with an overlay (deploy/gitops/publish.py --overlay)
     that points wave -20's anvilkit-streams at an absent NATS server (its PostSync
     setup can never succeed) and gives wave 10's anvilkit-agent-api a marker Pod
     annotation; the root Application moves to N. For --observe seconds every later
     wave must stay as it was: no Application after wave -20 changes its source and
     the API Deployment never carries the marker, while the root sync waits on
     anvilkit-streams.
  2. fix forward: publishes revision N+1 with the marker only (the streams repaired);
     the running syncs of the broken source are terminated (bootstrap.fix_forward)
     and the environment must converge with the marker on the API Pods and the
     probes Application (wave 30) synced to N+1.
Writes outputs/qualification/<RUN>/waves/waves.json and keeps each revision's
environment manifest there. Exit 0 when every check holds, 1 otherwise, 2 when the
environment is not converged at the start.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bootstrap  # noqa: E402  (kubectl, fix_forward, wait: the same Argo CD view)
from bootstrap import kubectl  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = str(ROOT / ".local/verification-venv/bin/python")
NS = "anvilkit-platform"
BROKEN_APP, BROKEN_WAVE = "anvilkit-streams", -20
MARKED_APP = "anvilkit-agent-api"
MARKER_KEY = "anvilkit.io/wave-probe"


def apps() -> dict[str, dict]:
    return {a["metadata"]["name"]: a for a in json.loads(kubectl("-n", NS, "get", "applications.argoproj.io", "-o", "json"))["items"]}


def wave(app: dict) -> int:
    return int(app["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", "0"))


def converged(current: dict[str, dict]) -> bool:
    return len(current) > 1 and all(a.get("status", {}).get("sync", {}).get("status") == "Synced"
                                     and a.get("status", {}).get("health", {}).get("status") == "Healthy" for a in current.values())


def api_marker() -> str | None:
    dep = json.loads(kubectl("-n", "anvilkit-apps", "get", "deployment", MARKED_APP, "-o", "json"))
    return dep["spec"]["template"]["metadata"].get("annotations", {}).get(MARKER_KEY)


def publish(run: str, revision: int, overlay: dict, out: pathlib.Path, label: str) -> dict:
    path = out / f"overlay-{label}.yaml"
    path.write_text(json.dumps(overlay, indent=2) + "\n")
    p = subprocess.run([PY, str(ROOT / "deploy/gitops/publish.py"), "--environment", "qualification", "--run", run,
                        "--revision", str(revision), "--overlay", str(path)], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"publish revision {revision}: {(p.stderr or p.stdout)[-400:]}")
    gitops = ROOT / "outputs/qualification" / run / "gitops"
    shutil.copy(gitops / "environment-manifest.json", out / f"environment-manifest-{label}.json")
    shutil.copy(gitops / "root-application.yaml", out / f"root-application-{label}.yaml")
    kubectl("apply", "-f", str(gitops / "root-application.yaml"))
    bootstrap.fix_forward(NS)
    return json.loads((gitops / "environment-manifest.json").read_text())["environmentChart"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--revision", type=int, required=True, help="the next unpublished environment revision of RUN")
    ap.add_argument("--observe", type=int, default=300)
    ap.add_argument("--wait", type=int, default=2400)
    a = ap.parse_args()
    out = ROOT / "outputs/qualification" / a.run / "waves"
    out.mkdir(parents=True, exist_ok=True)
    before = apps()
    if not converged(before):
        print("UNEXECUTED: the environment is not converged (every Application Synced and Healthy)")
        return 2
    later = {n: app["spec"]["source"] for n, app in before.items() if wave(app) > BROKEN_WAVE and n != "anvilkit-qualification"}  # not the root itself
    marker = f"r{a.revision}-{int(time.time())}"
    record: dict = {"run": a.run, "brokenApplication": BROKEN_APP, "brokenWave": BROKEN_WAVE, "markedApplication": MARKED_APP,
                    "startRevision": before["anvilkit-qualification"]["spec"]["source"]["targetRevision"], "laterApplications": sorted(later)}

    # 1. break
    absent = "nats://anvilkit-nats-absent.anvilkit-data.svc.cluster.local:4222"
    broken = publish(a.run, a.revision, {"applications": {BROKEN_APP: {"values": {"natsUrl": absent}},
                                                          MARKED_APP: {"values": {"podAnnotations": {MARKER_KEY: marker}}}}}, out, "break")
    record["breakRevision"] = broken["version"]
    started = time.time()
    samples = []
    while time.time() - started < a.observe:
        bootstrap.fix_forward(NS)
        current = apps()
        root_op = current["anvilkit-qualification"].get("status", {}).get("operationState", {})
        streams = current[BROKEN_APP].get("status", {})
        changed = sorted(n for n, src in later.items() if n in current and current[n]["spec"]["source"] != src)
        samples.append({"t": round(time.time() - started, 1), "rootTarget": current["anvilkit-qualification"]["spec"]["source"]["targetRevision"],
                        "rootOperation": root_op.get("phase"), "rootMessage": (root_op.get("message") or "")[:200],
                        "brokenSync": streams.get("sync", {}).get("status"), "brokenHealth": streams.get("health", {}).get("status"),
                        "brokenOperation": streams.get("operationState", {}).get("phase"),
                        "brokenApplied": current[BROKEN_APP]["spec"]["source"].get("helm", {}).get("valuesObject", {}).get("natsUrl") == absent,
                        "laterChanged": changed, "apiMarker": api_marker() == marker})
        time.sleep(15)
    record["breakSamples"] = samples
    record["breakChecks"] = {
        "brokenRevisionReachedTheBrokenWave": any(s["brokenApplied"] for s in samples),
        # What the root gates on (bootstrap.APP_HEALTH): Synced, Healthy and the sync operation Succeeded;
        # Argo CD's own sync/health statuses ignore the failing hook.
        "brokenWaveNeverHealthy": all(not (s["brokenApplied"] and s["brokenSync"] == "Synced" and s["brokenHealth"] == "Healthy"
                                           and s["brokenOperation"] == "Succeeded") for s in samples),
        "rootWaitedOnTheBrokenWave": any(BROKEN_APP in s["rootMessage"] for s in samples),
        "laterWavesUnchanged": all(not s["laterChanged"] for s in samples),
        "markerNeverApplied": all(not s["apiMarker"] for s in samples),
    }

    # 2. fix forward
    repaired = publish(a.run, a.revision + 1, {"applications": {MARKED_APP: {"values": {"podAnnotations": {MARKER_KEY: marker}}}}}, out, "repair")
    record["repairRevision"] = repaired["version"]
    started = time.time()
    ok = bootstrap.wait(a.wait)
    current = apps()
    probes_op = current["anvilkit-probes"].get("status", {}).get("operationState", {})
    record["repairSeconds"] = round(time.time() - started, 1)
    record["repairChecks"] = {
        "environmentConverged": ok,
        "markerApplied": api_marker() == marker,
        "probesSyncedToRepair": current["anvilkit-probes"]["spec"]["source"].get("helm", {}).get("valuesObject", {}).get("environmentRevision") == repaired["version"]
                                and probes_op.get("phase") == "Succeeded",
    }
    record["probeOperation"] = {"phase": probes_op.get("phase"), "message": (probes_op.get("message") or "")[:200]}
    record["placement"] = "DEVELOPMENT_ONLY: one Argo CD in the kind qualification cluster"
    passed = all(record["breakChecks"].values()) and all(record["repairChecks"].values())
    record["status"] = "PASS" if passed else "FAIL"
    (out / "waves.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({k: record[k] for k in ("breakRevision", "repairRevision", "breakChecks", "repairChecks", "repairSeconds", "status")}, indent=1))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
