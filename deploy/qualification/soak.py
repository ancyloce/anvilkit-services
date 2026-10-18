#!/usr/bin/env python3
"""Soak run of the qualification environment (P3.5 AC2).

  .local/verification-venv/bin/python deploy/qualification/soak.py --run RUN \\
      [--profile tests/load/profiles/soak-development.json] [--rollout-every 1800] \\
      [--upstream-count-url URL] [--allow-short]
  .local/verification-venv/bin/python deploy/qualification/soak.py --self-test

Drives the load tool (tests/load) at the profile's rates for its duration (>= 4 h; a
shorter profile is refused unless --allow-short, which records a REHEARSAL that never
passes) with its holds, cancels and stream resumes, and rolls out one application
Deployment of anvilkit-apps every --rollout-every seconds in turn (kubectl rollout
restart, then rollout status). Afterwards:
  noDuplicateSends          the countable upstream's receives equal Control's admitted
                            model dispatches of the window (NOT_MEASURED without
                            --upstream-count-url: the qualification combination has no
                            countable upstream until ENV-06)
  noDuplicateCharges        no dispatch of the window has two `actual` cost entries
  noStuckReconciling        no operation of the window is `reconciling` once its
                            dispatches and effects are resolved
  noSenderLostFromRollouts  the Proxy's SENDER_LOST outcomes did not increase
  noRestartFromLoad         no application container restarted (rolled-out Pods start
                            at zero; a rise is a crash or an OOM kill)
  rolloutsCompleted         every rollout reached its ready state
  loadNotFailed             the load tool's own status is not FAIL
Writes outputs/qualification/<RUN>/soak/{soak.json,summary.json,load.log}. It changes
the qualification cluster's workloads (rollouts) and runs for hours: run it only with
that environment operator's authorization. Exit 0 PASS, 1 FAIL, 2 PARTIAL/REHEARSAL or
UNEXECUTED (the environment is not healthy).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import drills  # noqa: E402
import loadrun  # noqa: E402

ROOT = loadrun.ROOT
SOAK_MINIMUM = 4 * 3600


def deployments() -> list[str]:
    return sorted(d["metadata"]["name"] for d in json.loads(drills.k("-n", loadrun.APPS, "get", "deployments", "-o", "json"))["items"]
                  if d["metadata"]["name"].startswith("anvilkit-agent-"))


def rollout(name: str) -> dict:
    started = time.time()
    drills.k("-n", loadrun.APPS, "rollout", "restart", f"deployment/{name}")
    try:
        drills.k("-n", loadrun.APPS, "rollout", "status", f"deployment/{name}", "--timeout=600s", timeout=620)
        ok = True
    except RuntimeError:
        ok = False
    return {"deployment": name, "at": loadrun.utc_now(), "seconds": round(time.time() - started, 1), "ready": ok}


def checks_of(summary: dict | None, led: dict[str, int], receives: int | None, restarts: list[dict], rollouts: list[dict]) -> dict[str, bool | None]:
    sender_lost = loadrun.metric_delta(summary, "modelProxySenderLost")
    return {
        "noDuplicateSends": None if receives is None else receives == led["admittedModelDispatches"],
        "noDuplicateCharges": led["duplicateCharges"] == 0,
        "noStuckReconciling": led["stuckReconciling"] == 0,
        "noSenderLostFromRollouts": None if sender_lost is None else sender_lost == 0,
        "noRestartFromLoad": not restarts,
        "rolloutsCompleted": bool(rollouts) and all(r["ready"] for r in rollouts),
        "loadNotFailed": summary is not None and summary.get("status") != "FAIL",
    }


def self_test() -> int:
    led = {"duplicateCharges": 0, "stuckReconciling": 0, "admittedModelDispatches": 7}
    summary = {"status": "MEASURED", "cluster": {"modelProxySenderLost": {"delta": 0}}}
    roll = [{"ready": True}]
    cases = {
        "all hold": (checks_of(summary, led, 7, [], roll), "PASS"),
        "a duplicate send": (checks_of(summary, led, 8, [], roll), "FAIL"),
        "no upstream counter": (checks_of(summary, led, None, [], roll), "PARTIAL"),
        "a duplicate charge": (checks_of(summary, {**led, "duplicateCharges": 1}, 7, [], roll), "FAIL"),
        "stuck reconciling": (checks_of(summary, {**led, "stuckReconciling": 2}, 7, [], roll), "FAIL"),
        "sender lost": (checks_of({**summary, "cluster": {"modelProxySenderLost": {"delta": 3}}}, led, 7, [], roll), "FAIL"),
        "a restart": (checks_of(summary, led, 7, [{"pod": "p"}], roll), "FAIL"),
        "no rollout": (checks_of(summary, led, 7, [], []), "FAIL"),
        "load failed": (checks_of({**summary, "status": "FAIL"}, led, 7, [], roll), "FAIL"),
        "no load summary": (checks_of(None, led, 7, [], roll), "FAIL"),
    }
    failures = [name for name, (checks, want) in cases.items() if loadrun.status_of(checks) != want]
    print(json.dumps({"selfTest": "PASS" if not failures else "FAIL", "failures": failures}))
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--profile", default=str(ROOT / "tests/load/profiles/soak-development.json"))
    ap.add_argument("--rollout-every", type=int, default=1800)
    ap.add_argument("--upstream-count-url")
    ap.add_argument("--allow-short", action="store_true", help="run a profile shorter than 4 h as a REHEARSAL (never PASS)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.run:
        ap.error("--run is required")
    profile = json.loads(pathlib.Path(a.profile).read_text())
    rehearsal = profile["durationSeconds"] < SOAK_MINIMUM
    if rehearsal and not a.allow_short:
        print(f"refused: the profile runs {profile['durationSeconds']} s, a soak runs at least {SOAK_MINIMUM} s (--allow-short records a REHEARSAL)")
        return 2
    if not drills.wait_until(drills.apps_healthy, 600, 15):
        print("UNEXECUTED: the environment is not healthy")
        return 2
    out = ROOT / "outputs/qualification" / a.run / "soak"
    out.mkdir(parents=True, exist_ok=True)
    loadrun.build_load()
    api, token = loadrun.api_target()
    names = deployments()
    since = loadrun.utc_now()
    restarts_before = loadrun.app_restarts()
    rollouts: list[dict] = []
    record: dict = {"run": a.run, "profile": profile["profileId"], "classification": profile["classification"], "since": since,
                    "placement": "DEVELOPMENT_ONLY: the kind qualification cluster on one host", "rolloutEverySeconds": a.rollout_every}
    try:
        with loadrun.KubeProxy() as proxy:
            load = loadrun.start_load(pathlib.Path(a.profile), a.run, out, api, token, proxy.prometheus)
            next_rollout = time.time() + a.rollout_every
            while load.poll() is None:
                if time.time() >= next_rollout:
                    rollouts.append(rollout(names[len(rollouts) % len(names)]))
                    next_rollout = time.time() + a.rollout_every
                time.sleep(10)
            record["loadExit"] = load.returncode
    finally:
        drills.k("-n", loadrun.APPS, "delete", "service", "anvilkit-drill-api", "--ignore-not-found", check=False)
    summary = loadrun.load_summary(out)
    led = loadrun.ledger(since)
    receives = loadrun.upstream_receives(a.upstream_count_url)
    restarts = loadrun.new_restarts(restarts_before, loadrun.app_restarts())
    record.update({"until": loadrun.utc_now(), "rollouts": rollouts, "ledger": led, "upstreamReceives": receives, "restarts": restarts,
                   "loadStatus": (summary or {}).get("status"), "loadReasons": (summary or {}).get("reasons")})
    record["checks"] = checks_of(summary, led, receives, restarts, rollouts)
    record["status"] = "REHEARSAL" if rehearsal else loadrun.status_of(record["checks"])
    (out / "soak.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"checks": record["checks"], "status": record["status"]}, indent=1))
    return {"PASS": 0, "FAIL": 1}.get(record["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
