#!/usr/bin/env python3
"""Promotion and rollback drill of the qualification environment (P3.2 AC4).

  .local/verification-venv/bin/python deploy/qualification/rollback-drill.py --from RUN_N --to RUN_N1 \\
      [--window 1800] [--wait 2400]

Precondition: the environment converged (every Application Synced and Healthy) on an
environment chart of release RUN_N, and RUN_N1 is a later release whose manifest
deploy/gitops/publish.py accepts. The drill:
  1. promote: publishes the combination against RUN_N1 (deploy/gitops/publish.py, the
     next free --revision of that run), points the root Application at it and waits
     until the environment converged with RUN_N1's service image digests running;
  2. roll back: publishes the combination against RUN_N again as a new environment
     chart version (a published version is never overwritten; the rollback is the
     revert of the promotion's combination change), points the root Application at it
     and measures the time until the environment converged with RUN_N's digests and
     the probe Application (the LocalCheck probe Job, wave 30) synced to that version.
Writes outputs/qualification/<RUN_N>/rollback/rollback.json. PASS only when both
directions converged, every anvilkit-apps Deployment runs exactly the release's
digests afterwards and the rollback converged within --window seconds (the documented
rollback window). It changes the workloads of the qualification cluster: run it only
with that environment's operator's authorization. Exit 0 PASS, 1 FAIL, 2 when the
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
import bootstrap  # noqa: E402  (kubectl, wait, credentials: the same Argo CD view)
from bootstrap import kubectl  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = str(ROOT / ".local/verification-venv/bin/python")
NS = "anvilkit-platform"
ROOT_APP = "anvilkit-qualification"


def apps() -> dict[str, dict]:
    return {a["metadata"]["name"]: a for a in json.loads(kubectl("-n", NS, "get", "applications.argoproj.io", "-o", "json"))["items"]}


def converged(current: dict[str, dict]) -> bool:
    return len(current) > 1 and all(a.get("status", {}).get("sync", {}).get("status") == "Synced"
                                     and a.get("status", {}).get("health", {}).get("status") == "Healthy" for a in current.values())


def running_digests() -> dict[str, list[str]]:
    """The image digests every anvilkit-apps Deployment's Pod template names."""
    out = {}
    for d in json.loads(kubectl("-n", "anvilkit-apps", "get", "deployments", "-o", "json"))["items"]:
        images = [c["image"] for c in d["spec"]["template"]["spec"]["containers"]]
        out[d["metadata"]["name"]] = sorted(i.split("@", 1)[1] for i in images if "@" in i)
    return out


def release_digests(run: str) -> set[str]:
    m = json.loads((ROOT / "outputs/qualification" / run / "release/release-manifest.json").read_text())
    return {e["digest"] for e in m["images"].values() if e.get("kind") == "service"}


def next_revision(run: str) -> int:
    gitops = ROOT / "outputs/qualification" / run / "gitops"
    seen = [1]
    for f in gitops.glob("environment-manifest*.json") if gitops.exists() else []:
        v = json.loads(f.read_text()).get("environmentChart", {}).get("version", "")
        tail = v.rsplit(f".{run}", 1)[-1].lstrip(".")
        seen.append(int(tail) if tail.isdigit() else 1)
    return max(seen) + 1


def publish(run: str, out: pathlib.Path, label: str) -> dict:
    revision = next_revision(run)
    p = subprocess.run([PY, str(ROOT / "deploy/gitops/publish.py"), "--environment", "qualification", "--run", run, "--revision", str(revision)],
                       capture_output=True, text=True)
    (out / f"publish-{label}.log").write_text(p.stdout + p.stderr)
    if p.returncode != 0:
        raise RuntimeError(f"publish {run} revision {revision}: {(p.stdout + p.stderr)[-400:]}")
    gitops = ROOT / "outputs/qualification" / run / "gitops"
    shutil.copy(gitops / "environment-manifest.json", out / f"environment-manifest-{label}.json")
    shutil.copy(gitops / "environment-manifest.json", gitops / f"environment-manifest-r{revision}.json")
    shutil.copy(gitops / "root-application.yaml", out / f"root-application-{label}.yaml")
    kubectl("apply", "-f", str(gitops / "root-application.yaml"))
    return json.loads((gitops / "environment-manifest.json").read_text())["environmentChart"]


def converge(version: str, digests: set[str], seconds: int, creds: dict) -> dict:
    started = time.time()
    ok = bootstrap.wait(seconds, creds)
    current = apps()
    probes = current.get("anvilkit-probes", {})
    running = running_digests()
    foreign = {name: [d for d in ds if d not in digests] for name, ds in running.items() if any(d not in digests for d in ds)}
    return {"seconds": round(time.time() - started, 1), "converged": ok and converged(current),
            "rootTarget": current[ROOT_APP]["spec"]["source"]["targetRevision"], "expectedTarget": version,
            "deploymentsOnOtherDigests": foreign,
            "probesSynced": probes.get("spec", {}).get("source", {}).get("helm", {}).get("valuesObject", {}).get("environmentRevision") == version
                            and probes.get("status", {}).get("operationState", {}).get("phase") == "Succeeded"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="run_from", required=True, help="the release the environment runs now (N)")
    ap.add_argument("--to", dest="run_to", required=True, help="the release promoted and rolled back (N+1)")
    ap.add_argument("--window", type=int, default=1800, help="the documented rollback window in seconds")
    ap.add_argument("--wait", type=int, default=2400)
    a = ap.parse_args()
    out = ROOT / "outputs/qualification" / a.run_from / "rollback"
    out.mkdir(parents=True, exist_ok=True)
    if not converged(apps()):
        print("UNEXECUTED: the environment is not converged (every Application Synced and Healthy)")
        return 2
    creds = bootstrap.credentials()
    record: dict = {"from": a.run_from, "to": a.run_to, "windowSeconds": a.window,
                    "startTarget": apps()[ROOT_APP]["spec"]["source"]["targetRevision"],
                    "placement": "DEVELOPMENT_ONLY: one Argo CD in the kind qualification cluster"}
    promoted = publish(a.run_to, out, "promote")
    record["promote"] = converge(promoted["version"], release_digests(a.run_to), a.wait, creds)
    rolled = publish(a.run_from, out, "rollback")
    record["rollback"] = converge(rolled["version"], release_digests(a.run_from), a.wait, creds)
    record["checks"] = {
        "promotionConverged": record["promote"]["converged"] and not record["promote"]["deploymentsOnOtherDigests"],
        "rollbackConverged": record["rollback"]["converged"] and not record["rollback"]["deploymentsOnOtherDigests"],
        "rollbackProbeGreen": record["rollback"]["probesSynced"],
        "rollbackWithinWindow": record["rollback"]["seconds"] <= a.window,
    }
    record["status"] = "PASS" if all(record["checks"].values()) else "FAIL"
    (out / "rollback.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({k: record[k] for k in ("checks", "status")}, indent=1))
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
