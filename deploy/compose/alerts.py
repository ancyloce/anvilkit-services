#!/usr/bin/env python3
"""Alert evaluation of the Compose placement (RB-A25; DEC-049, DEC-050).

This placement runs no Prometheus, so the signals a Kubernetes environment
reads from kube-state-metrics and the services' gauges are read here from what
the host has: Docker's own state of each container and the services' log
lines. Three alerts, named as the observability chart names theirs:

  AnvilKitStartupFailing   critical  a service container is restarting, dead,
                                     unhealthy, absent or exited non-zero; a
                                     one-shot (migration, setup) exited non-zero.
                                     A service that loads its configuration
                                     once and rejects it shows up here.
  AnvilKitAbnormalRestart  warning   Docker's restart policy restarted a
                                     container inside the window (RestartCount
                                     above zero and a start inside it), or its
                                     last exit was an out-of-memory kill.
  AnvilKitForcedStop       warning   a service logged a stop with forced=true
                                     inside the window (its drain bound passed
                                     and it closed what was still open).

It reads state only (docker ps, docker inspect --format with state fields,
docker logs --since); it changes nothing and prints no log content, only
counts. One line per finding, then a result line. Exit 0 when no alert fires,
1 when one does, 2 when the state could not be read (UNEXECUTED).

    python3 deploy/compose/alerts.py                 # by hand, and from verify.sh
    python3 deploy/compose/alerts.py --window 15m --journal   # the anvilkit-alerts timer
    python3 deploy/compose/alerts.py --self-test     # firing and non-firing cases, no Docker

A recreate (a deployment's `up -d` with a new image) removes the old container
and its log with it: stop the service first and run this script before the
recreate to see a forced stop of the old container (README.md, Alerts).
Runbook: deploy/runbooks/startup-failure.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import subprocess
import sys

PROJECT = "anvilkit"

# The long-running services of compose.yaml: an absent container is a failed start.
SERVICES = (
    "postgres", "temporal", "minio", "nats", "valkey-queue", "valkey-cache", "qdrant", "oidc",
    "anvilkit-agent-control", "anvilkit-agent-api", "anvilkit-agent-workflow", "anvilkit-agent-model-proxy",
    "anvilkit-agent-inference", "anvilkit-agent-knowledge", "knowledge-forwarder", "knowledge-relay",
    "anvilkit-agent-mcp", "mcp-relay", "anvilkit-agent-background-worker",
)

FIELDS = ("service", "status", "exit_code", "oom_killed", "started_at", "restart_count", "health", "restart_policy")
INSPECT_FORMAT = "\t".join((
    '{{index .Config.Labels "com.docker.compose.service"}}',
    "{{.State.Status}}",
    "{{.State.ExitCode}}",
    "{{.State.OOMKilled}}",
    "{{.State.StartedAt}}",
    "{{.RestartCount}}",
    "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
    "{{.HostConfig.RestartPolicy.Name}}",
))

# Every service's stop line carries the field (Go slog and the TypeScript and
# Python loggers all write JSON): "forced":true.
FORCED_RE = re.compile(r'"forced"\s*:\s*true')

SEVERITY = {"AnvilKitStartupFailing": "critical", "AnvilKitAbnormalRestart": "warning", "AnvilKitForcedStop": "warning"}
JOURNAL_PRIORITY = {"critical": "<2>", "warning": "<4>"}


def parse_window(text: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+)([smh])", text)
    if not m or int(m.group(1)) == 0:
        raise argparse.ArgumentTypeError(f"window {text!r}: a positive number followed by s, m or h")
    return dt.timedelta(seconds=int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)])


def parse_time(text: str) -> dt.datetime | None:
    """Docker's RFC 3339 time with nanoseconds; the zero time (never started) is None."""
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z", text)
    if not m or text.startswith("0001-"):
        return None
    micro = int((m.group(2) or "0")[:6].ljust(6, "0"))
    return dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(microsecond=micro, tzinfo=dt.timezone.utc)


def evaluate(containers: list[dict], forced_stops: dict[str, int], now: dt.datetime, window: dt.timedelta,
             services: tuple[str, ...] = SERVICES) -> list[tuple[str, str, str]]:
    """The alerts that fire, as (alert, service, detail), for the given container states and
    the count of forced-stop log lines per service inside the window."""
    alerts: list[tuple[str, str, str]] = []
    seen = {c["service"] for c in containers}
    for name in services:
        if name not in seen:
            alerts.append(("AnvilKitStartupFailing", name, "no container"))
    for c in containers:
        name, status, code = c["service"], c["status"], int(c["exit_code"])
        long_running = c["restart_policy"] not in ("", "no")
        if long_running:
            if status == "restarting":
                alerts.append(("AnvilKitStartupFailing", name, f"restarting, last exit code {code}, {c['restart_count']} restarts"))
            elif status == "dead" or (status == "exited" and code != 0):
                alerts.append(("AnvilKitStartupFailing", name, f"{status} with exit code {code}"))
            elif status == "running" and c["health"] == "unhealthy":
                alerts.append(("AnvilKitStartupFailing", name, "running but unhealthy: its readiness check keeps failing"))
            started = parse_time(c["started_at"])
            if status == "running" and int(c["restart_count"]) > 0 and started is not None and now - started <= window:
                alerts.append(("AnvilKitAbnormalRestart", name,
                               f"restarted by the restart policy at {started.strftime('%Y-%m-%dT%H:%M:%SZ')} ({c['restart_count']} restarts since it was created)"))
        elif status == "exited" and code != 0:
            alerts.append(("AnvilKitStartupFailing", name, f"one-shot exited with code {code}"))
        if str(c["oom_killed"]).lower() == "true":
            alerts.append(("AnvilKitAbnormalRestart", name, "its last exit was an out-of-memory kill"))
    for name in sorted(forced_stops):
        if forced_stops[name] > 0:
            alerts.append(("AnvilKitForcedStop", name, f"{forced_stops[name]} stop(s) with forced=true logged inside the window"))
    return alerts


def docker(*args: str) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise RuntimeError(f"docker {args[0]}: {out.stderr.strip()[:200]}")
    return out.stdout


def read_state(window: dt.timedelta) -> tuple[list[dict], dict[str, int]]:
    ids = docker("ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}", "--format", "{{.ID}}").split()
    if not ids:
        raise RuntimeError(f"no container of the Compose project {PROJECT!r}")
    containers, forced = [], {}
    for cid in ids:
        row = docker("inspect", cid, "--format", INSPECT_FORMAT).rstrip("\n").split("\t")
        if len(row) != len(FIELDS):
            raise RuntimeError(f"container {cid}: unexpected state row")
        c = dict(zip(FIELDS, row))
        containers.append(c)
        if c["restart_policy"] not in ("", "no"):
            logs = subprocess.run(["docker", "logs", "--since", f"{int(window.total_seconds())}s", cid],
                                  capture_output=True, text=True, timeout=120)
            lines = (logs.stdout + logs.stderr).splitlines()
            forced[c["service"]] = forced.get(c["service"], 0) + sum(1 for line in lines if FORCED_RE.search(line))
    return containers, forced


def report(alerts: list[tuple[str, str, str]], journal: bool) -> int:
    for alert, service, detail in alerts:
        prefix = JOURNAL_PRIORITY[SEVERITY[alert]] if journal else ""
        print(f"{prefix}ALERT       {alert} severity={SEVERITY[alert]} service={service}: {detail}")
    firing = sorted({a for a, _, _ in alerts})
    for alert in SEVERITY:
        if alert not in firing:
            print(f"OK          {alert}: not firing")
    print(f"== alerts: {len(alerts)} firing" + (f" ({', '.join(firing)})" if firing else ""))
    return 1 if alerts else 0


def self_test() -> int:
    now = dt.datetime(2026, 11, 9, 20, 0, 0, tzinfo=dt.timezone.utc)
    window = dt.timedelta(minutes=15)

    def at(minutes_ago: float) -> str:
        return (now - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")

    def box(service: str, status: str = "running", code: int = 0, restarts: int = 0, started: float = 600,
            health: str = "healthy", policy: str = "unless-stopped", oom: bool = False) -> dict:
        return {"service": service, "status": status, "exit_code": str(code), "oom_killed": str(oom).lower(),
                "started_at": at(started), "restart_count": str(restarts), "health": health, "restart_policy": policy}

    healthy = [box(s) for s in SERVICES]

    def swap(replacement: dict) -> list[dict]:
        return [replacement if c["service"] == replacement["service"] else c for c in healthy]

    api, control = "anvilkit-agent-api", "anvilkit-agent-control"
    cases: list[tuple[str, list[dict], dict[str, int], set[tuple[str, str]]]] = [
        # Non-firing.
        ("every service healthy, never restarted", healthy, {}, set()),
        ("a deployment's recreate: fresh containers, no restart, still starting", swap(box(api, started=1, health="starting")), {}, set()),
        ("a restart by the policy long before the window", swap(box(api, restarts=3, started=240)), {}, set()),
        ("a service an operator stopped cleanly", swap(box(api, status="exited", code=0)), {}, set()),
        ("the one-shots completed", healthy + [box("control-migration", status="exited", policy="no", health="")], {}, set()),
        ("stops that were not forced", healthy, {api: 0, control: 0}, set()),
        # Firing: startup failures.
        ("a service rejects its configuration and restarts in a loop", swap(box(api, status="restarting", code=1, restarts=7, started=0.2, health="")),
         {}, {("AnvilKitStartupFailing", api)}),
        ("a service exited non-zero and stays down", swap(box(control, status="exited", code=2)), {}, {("AnvilKitStartupFailing", control)}),
        ("a service runs but never becomes ready", swap(box(api, health="unhealthy", started=30)), {}, {("AnvilKitStartupFailing", api)}),
        ("a service has no container", [c for c in healthy if c["service"] != api], {}, {("AnvilKitStartupFailing", api)}),
        ("a migration one-shot failed", healthy + [box("control-migration", status="exited", code=1, policy="no", health="")],
         {}, {("AnvilKitStartupFailing", "control-migration")}),
        # Firing: abnormal restarts.
        ("the policy restarted a service inside the window", swap(box(api, restarts=1, started=4)), {}, {("AnvilKitAbnormalRestart", api)}),
        ("a restart at the window's edge", swap(box(api, restarts=2, started=15)), {}, {("AnvilKitAbnormalRestart", api)}),
        ("an out-of-memory kill", swap(box(control, status="exited", code=137, oom=True)),
         {}, {("AnvilKitStartupFailing", control), ("AnvilKitAbnormalRestart", control)}),
        ("a crash loop that is running at this moment", swap(box(api, restarts=9, started=0.1, health="starting")), {}, {("AnvilKitAbnormalRestart", api)}),
        # Firing: forced stops.
        ("the API closed connections at its drain bound", healthy, {api: 1, control: 0}, {("AnvilKitForcedStop", api)}),
    ]
    failures = 0
    for name, containers, forced, want in cases:
        got = {(a, s) for a, s, _ in evaluate(containers, forced, now, window)}
        ok = got == want
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {'fires' if want else 'silent'}: {name}" + ("" if ok else f" (got {sorted(got)}, want {sorted(want)})"))
    line_cases = [
        ('{"time":"2026-11-09T19:55:00Z","level":"INFO","msg":"api stopped","drainSeconds":1.0003,"forced":true}', True),
        ('{"level":"info","msg":"knowledge stopped","drainSeconds":0.4,"forced": true}', True),
        ('{"time":"2026-11-09T19:55:00Z","level":"INFO","msg":"api stopped","drainSeconds":0.008,"forced":false}', False),
        ('{"level":"INFO","msg":"api serving","listen":"0.0.0.0:9100"}', False),
    ]
    for line, want in line_cases:
        ok = bool(FORCED_RE.search(line)) == want
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  log line {'counts' if want else 'does not count'} as a forced stop: {line[:70]}")
    for text, want in (("2026-11-09T19:56:00.123456789Z", dt.datetime(2026, 11, 9, 19, 56, 0, 123456, tzinfo=dt.timezone.utc)),
                       ("2026-11-09T19:56:00Z", dt.datetime(2026, 11, 9, 19, 56, 0, tzinfo=dt.timezone.utc)),
                       ("0001-01-01T00:00:00Z", None)):
        ok = parse_time(text) == want
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  time {text}")
    # SERVICES is compose.yaml's long-running set (a restart policy, directly or through the
    # x-service anchor): a service added there without a row here would never count as absent.
    compose = pathlib.Path(__file__).resolve().parent / "compose.yaml"
    declared: set[str] = set()
    current, inside = None, False
    for line in compose.read_text(encoding="utf-8").splitlines():
        if re.fullmatch(r"services:\s*", line):
            inside = True
        elif inside and re.fullmatch(r"  ([a-z0-9-]+):\s*", line):
            current = line.strip().rstrip(":")
        elif inside and current and re.fullmatch(r"    (<<: \*service|restart: unless-stopped)\s*", line):
            declared.add(current)
    ok = declared == set(SERVICES)
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  SERVICES equals compose.yaml's {len(declared)} long-running services"
          + ("" if ok else f" (only here: {sorted(set(SERVICES) - declared)}, only there: {sorted(declared - set(SERVICES))})"))
    print(f"== self-test: {len(cases) + len(line_cases) + 4} cases, {failures} failed")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--window", type=parse_window, default=dt.timedelta(minutes=15), help="how far back a restart or a forced stop counts (default 15m)")
    ap.add_argument("--journal", action="store_true", help="prefix alert lines with their journal priority (the systemd timer)")
    ap.add_argument("--self-test", action="store_true", help="evaluate fixed firing and non-firing states; needs no Docker")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    try:
        containers, forced = read_state(args.window)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        print(f"UNEXECUTED  alerts: the container state could not be read ({e})")
        return 2
    return report(evaluate(containers, forced, dt.datetime.now(dt.timezone.utc), args.window), args.journal)


if __name__ == "__main__":
    sys.exit(main())
