#!/usr/bin/env python3
"""Shared pieces of the P3.5 soak and chaos runs (deploy/qualification/{soak,chaos}.py).

The load is tests/load (the Go load tool) built once into .local/bin/anvilkit-load and
pointed at the API through the drills' NodePort Service with the probe principal's
token (passed in the environment, never on a command line or in a record). The metrics
readout goes through the API server's service proxy (kubectl proxy) to the P3.6
Prometheus. The ledger checks read Control's database on the business cluster's
primary: duplicate charges (a dispatch with more than one `actual` cost entry: Control
books one actual entry per dispatch and every later increase as a correction of it),
operations stuck `reconciling` after their obligations resolved, dispatches past their
deadline with no observation and no disposition, admitted model dispatches (the count
the countable upstream's receives must equal).

  .local/verification-venv/bin/python deploy/qualification/loadrun.py --self-test
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import drills  # noqa: E402  (kubectl, psql, the NodePort Service, health)

ROOT = pathlib.Path(__file__).resolve().parents[2]
LOAD_BIN = ROOT / ".local/bin/anvilkit-load"
PROMETHEUS = "/api/v1/namespaces/anvilkit-observability/services/http:anvilkit-metrics-prometheus:9090/proxy"
APPS = "anvilkit-apps"
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$")


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _ts(value: str) -> str:
    """A timestamp literal of this module's own making, checked before it reaches SQL."""
    if not _TS.match(value):
        raise ValueError(f"not a UTC timestamp: {value!r}")
    return value


def build_load() -> str:
    LOAD_BIN.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["go", "build", "-o", str(LOAD_BIN), "."], cwd=ROOT / "tests/load", env={**os.environ, "GOWORK": "off"}, check=True)
    return str(LOAD_BIN)


class KubeProxy:
    """kubectl proxy on a free loopback port for the run's duration."""

    def __enter__(self) -> "KubeProxy":
        self.p = subprocess.Popen([drills.KUBECTL, "--kubeconfig", str(drills.KUBECONFIG), "proxy", "--port=0"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        line = self.p.stdout.readline()  # "Starting to serve on 127.0.0.1:PORT"
        m = re.search(r"127\.0\.0\.1:(\d+)", line)
        if not m:
            self.p.kill()
            raise RuntimeError(f"kubectl proxy: {line.strip()!r}")
        self.base = f"http://127.0.0.1:{m.group(1)}"
        return self

    @property
    def prometheus(self) -> str:
        return self.base + PROMETHEUS

    def __exit__(self, *exc) -> None:
        self.p.terminate()
        self.p.wait(timeout=10)


def api_target() -> tuple[str, str]:
    """The API through the drills' NodePort on a live worker, and the probe token."""
    creds = json.loads((drills.STATE / "credentials.json").read_text())
    port = drills.drill_service()
    return f"http://{drills.node_ip(drills.workers()[0])}:{port}", creds["principals"]["probe"]


def start_load(profile: pathlib.Path, run: str, out: pathlib.Path, api: str, token: str, prometheus: str) -> subprocess.Popen:
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "load.log", "w")
    return subprocess.Popen([str(LOAD_BIN), "--api", api, "--profile", str(profile), "--prometheus", prometheus, "--out", str(out), "--run", run],
                            env={**os.environ, "ANVILKIT_LOAD_TOKEN": token}, stdout=log, stderr=subprocess.STDOUT)


def load_summary(out: pathlib.Path) -> dict | None:
    f = out / "summary.json"
    return json.loads(f.read_text()) if f.exists() else None


def control_sql(sql: str) -> str:
    return drills.psql("anvilkit-business", sql, "anvilkit_control")


def ledger_queries(since: str, until: str | None = None) -> dict[str, str]:
    since = _ts(since)
    window = f"d.admitted_at >= '{since}'" + (f" AND d.admitted_at < '{_ts(until)}'" if until else "")
    return {
        "duplicateCharges": "SELECT count(*) FROM (SELECT c.dispatch_id FROM cost_entries c JOIN dispatches d USING (dispatch_id) "
                            f"WHERE {window} AND c.kind = 'actual' GROUP BY c.dispatch_id HAVING count(*) > 1) s",
        "stuckReconciling": "SELECT count(*) FROM operations o WHERE o.created_at >= " f"'{since}' AND o.lifecycle = 'reconciling' "
                            "AND NOT EXISTS (SELECT 1 FROM dispatches d WHERE d.operation_id = o.operation_id AND d.state IN ('prepared', 'authorized', 'unknown') "
                            "AND NOT EXISTS (SELECT 1 FROM obligation_dispositions x WHERE x.obligation_id = d.dispatch_id)) "
                            "AND NOT EXISTS (SELECT 1 FROM effect_intents e WHERE e.operation_id = o.operation_id AND e.state IN ('prepared', 'sent', 'unknown'))",
        "unresolvedPastDeadline": f"SELECT count(*) FROM dispatches d WHERE {window} AND d.state IN ('prepared', 'authorized', 'unknown') "
                                  "AND d.deadline < now() AND NOT EXISTS (SELECT 1 FROM obligation_dispositions x WHERE x.obligation_id = d.dispatch_id)",
        "unknownDispatches": f"SELECT count(*) FROM dispatches d WHERE {window} AND d.state = 'unknown'",
        "admittedModelDispatches": f"SELECT count(*) FROM dispatches d WHERE {window} AND d.kind = 'model' AND d.state NOT IN ('denied', 'confirmed_not_sent')",
    }


def ledger(since: str, until: str | None = None) -> dict[str, int]:
    return {name: int(control_sql(sql) or 0) for name, sql in ledger_queries(since, until).items()}


def app_restarts() -> list[dict]:
    """Containers of the applications that restarted (the restart count of the Pod
    they run in; a rolled-out Pod starts again at zero)."""
    out = []
    for p in json.loads(drills.k("-n", APPS, "get", "pods", "-o", "json"))["items"]:
        for c in p.get("status", {}).get("containerStatuses", []) + p.get("status", {}).get("initContainerStatuses", []):
            if c.get("restartCount", 0) > 0:
                last = c.get("lastState", {}).get("terminated", {})
                out.append({"pod": p["metadata"]["name"], "container": c["name"], "restarts": c["restartCount"], "reason": last.get("reason"), "exitCode": last.get("exitCode")})
    return out


def upstream_receives(url: str | None) -> int | None:
    """The countable upstream's receive count ({"receives": N}); None without one."""
    if not url:
        return None
    with urllib.request.urlopen(url, timeout=10) as r:
        return int(json.loads(r.read())["receives"])


def new_restarts(before: list[dict], after: list[dict]) -> list[dict]:
    """Restarts that happened during the run: a container whose count rose, or a
    restarted container of a Pod that did not exist before."""
    seen = {(r["pod"], r["container"]): r["restarts"] for r in before}
    return [r for r in after if r["restarts"] > seen.get((r["pod"], r["container"]), 0)]


def status_of(checks: dict[str, bool | None]) -> str:
    """FAIL when a check failed, PARTIAL when one was not measured (None), else PASS."""
    if any(v is False for v in checks.values()):
        return "FAIL"
    if any(v is None for v in checks.values()):
        return "PARTIAL"
    return "PASS"


def metric_delta(summary: dict | None, name: str) -> float | None:
    m = ((summary or {}).get("cluster") or {}).get(name)
    if not m or m.get("missing"):
        return None
    return m.get("delta", 0.0)


def self_test() -> int:
    failures = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    q = ledger_queries("2026-10-13T10:00:00+00:00", "2026-10-13T11:00:00+00:00")
    check("window bounds every dispatch query", all("d.admitted_at >= '2026-10-13T10:00:00+00:00'" in v for k, v in q.items() if k != "stuckReconciling"))
    check("until bound", "d.admitted_at < '2026-10-13T11:00:00+00:00'" in q["duplicateCharges"])
    check("duplicate charge is a second actual entry", "c.kind = 'actual'" in q["duplicateCharges"] and "count(*) > 1" in q["duplicateCharges"])
    try:
        ledger_queries("2026-10-13'; DROP TABLE operations; --")
        check("refuses a non-timestamp", False)
    except ValueError:
        pass
    check("utc_now is accepted", bool(_TS.match(utc_now())))
    before = [{"pod": "a", "container": "c", "restarts": 1}]
    after = [{"pod": "a", "container": "c", "restarts": 1}, {"pod": "b", "container": "c", "restarts": 2}]
    check("pre-existing restarts are not the run's", new_restarts(before, after) == [after[1]])
    check("a rise counts", new_restarts(before, [{"pod": "a", "container": "c", "restarts": 3}]) != [])
    check("status FAIL", status_of({"a": True, "b": False, "c": None}) == "FAIL")
    check("status PARTIAL", status_of({"a": True, "c": None}) == "PARTIAL")
    check("status PASS", status_of({"a": True}) == "PASS")
    check("metric missing is None", metric_delta({"cluster": {"x": {"missing": True}}}, "x") is None and metric_delta(None, "x") is None)
    check("metric delta", metric_delta({"cluster": {"x": {"delta": 2.0}}}, "x") == 2.0)
    print(json.dumps({"selfTest": "PASS" if not failures else "FAIL", "failures": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)
    sys.exit(2)
