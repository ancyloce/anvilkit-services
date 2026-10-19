#!/usr/bin/env python3
"""Worker Deployment Versioning rollout of the Workflow worker (P1.7).

  .local/verification-venv/bin/python deploy/qualification/workflow-rollout.py rollout \\
      --namespace NS --temporal-address HOST:PORT -f VALUES [-f VALUES] [--set KEY=VALUE] \\
      [--release anvilkit-agent-workflow] [--context CTX] [--kubeconfig PATH] \\
      [--temporal-namespace anvilkit] [--temporal-cli CMD] [--ready-timeout 300] \\
      [--drain-timeout 3600] [--poll 15] [--no-install] [--out DIR]
  .local/verification-venv/bin/python deploy/qualification/workflow-rollout.py retire \\
      --namespace NS --temporal-address HOST:PORT [--release ...] [--drain-timeout 3600]
  .local/verification-venv/bin/python deploy/qualification/workflow-rollout.py --self-test

With config.temporal.versioning.enabled the Workflow chart renders one worker version per
release: its Deployment, ConfigMap, HPA and PDB are named <fullname>-<sha256(buildId)[:10]>
(label anvilkit.io/worker-version) and kept by Helm and Argo CD when a later release no
longer renders them. `rollout` moves the release from its running build(s) A to the
rendered build B:
  1. install  helm upgrade --install creates B's Deployment beside A's (--no-install: the
              environment's GitOps applied it). An unversioned predecessor (the release
              that first enables versioning) is annotated with the keep policies first.
              A's Deployment must still exist, unchanged, afterwards.
  2. ready    B's rollout completes and B's version has registered both task queues and
              every task queue type the current version serves, within --ready-timeout.
              Otherwise ABORTED: nothing is promoted and A stays current and running.
  3. promote  set-current-version B (Temporal's missing-task-queue and no-poller
              protections stay on).
  4. drain    each previous versioned build is polled (describe-version drainage status,
              open runs pinned to it by a visibility count) until it is DRAINED, checked
              after the routing change, with zero open pinned runs, within --drain-timeout.
  5. remove   only then are its Deployment, ConfigMap, PDB and HPA deleted by exact name
              (foreground, waiting for its Pods). The current or ramping build and a
              DRAINING build are never removed: at the bound the result is DRAINING and
              `retire` resumes steps 4-5 later. An unversioned predecessor has no pinned
              runs; it is removed once B is current (its open runs move to the current
              version on their next task, as in a rolling update; their count is recorded).
Temporal is reached through the Temporal CLI (--temporal-cli, default `temporal`; on the
development foundation the pinned admin-tools image, see README.md). Writes
outputs/workflow-rollout/<UTC>/rollout.json (or --out). It changes the target cluster's
workloads and the worker deployment's routing: run it only with that environment's
operator's authorization. Exit 0 COMPLETE, 1 ABORTED or FAILED, 2 REFUSED (nothing
changed), 3 DRAINING (B current, a previous build kept until it drains).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import shlex
import subprocess
import sys
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "services/agent/workflow/deploy/chart"
NAME = "anvilkit-agent-workflow"
VERSION_LABEL = "anvilkit.io/worker-version"
BUILD_ANNOTATION = "anvilkit.io/worker-build-id"
DEPLOYMENT_ANNOTATION = "anvilkit.io/worker-deployment"
KEEP = {"helm.sh/resource-policy": "keep", "argocd.argoproj.io/sync-options": "Prune=false"}
PER_VERSION = ("deployment", "configmap", "poddisruptionbudget", "horizontalpodautoscaler")
EXIT = {"COMPLETE": 0, "ABORTED": 1, "FAILED": 1, "REFUSED": 2, "DRAINING": 3}


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def binary(name: str) -> str:
    local = ROOT / ".local/bin" / name
    return str(local) if local.exists() else name


def run(cmd: list[str], timeout: int) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}: {(p.stderr or p.stdout).strip()[-400:]}")
    return p.stdout


class Kube:
    """kubectl and helm against one release in one namespace."""

    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.common = ["--namespace", a.namespace] + (["--kubeconfig", a.kubeconfig] if a.kubeconfig else [])

    def kubectl(self, *args: str, timeout: int = 120) -> str:
        return run([binary("kubectl"), *self.common, *(["--context", self.a.context] if self.a.context else []), *args], timeout)

    def helm(self, *args: str, timeout: int = 600) -> str:
        return run([binary("helm"), *self.common, *(["--kube-context", self.a.context] if self.a.context else []), *args], timeout)

    def chart_args(self) -> list[str]:
        out = [self.a.release, str(self.a.chart)]
        for f in self.a.values:
            out += ["-f", f]
        for s in self.a.set:
            out += ["--set", s]
        return out

    def render(self) -> list[dict]:
        return [d for d in yaml.safe_load_all(self.helm("template", *self.chart_args())) if d]

    def install(self) -> None:
        self.helm("upgrade", "--install", *self.chart_args())

    def workers(self) -> list[dict]:
        sel = f"app.kubernetes.io/name={NAME},app.kubernetes.io/instance={self.a.release}"
        return json.loads(self.kubectl("get", "deployments", "-l", sel, "-o", "json"))["items"]

    def keep(self, name: str) -> list[str]:
        objects = self.kubectl("get", *(f"{kind}/{name}" for kind in PER_VERSION), "--ignore-not-found", "-o", "name").split()
        if objects:
            self.kubectl("annotate", "--overwrite", *objects, *(f"{k}={v}" for k, v in KEEP.items()))
        return objects

    def ready(self, name: str, seconds: int) -> bool:
        try:
            self.kubectl("rollout", "status", f"deployment/{name}", f"--timeout={max(seconds, 1)}s", timeout=seconds + 30)
            return True
        except (RuntimeError, subprocess.TimeoutExpired):
            return False

    def remove(self, name: str, seconds: int) -> None:
        self.kubectl("delete", *(f"{kind}/{name}" for kind in PER_VERSION), "--ignore-not-found", "--cascade=foreground",
                     "--wait=true", f"--timeout={seconds}s", timeout=seconds + 30)


class Temporal:
    """The Temporal CLI's worker deployment and visibility commands (JSON output)."""

    def __init__(self, a: argparse.Namespace):
        self.cli = shlex.split(a.temporal_cli)
        self.flags = ["--address", a.temporal_address, "--namespace", a.temporal_namespace, "--output", "json"]

    def call(self, *args: str) -> dict | list:
        out = run([*self.cli, *args, *self.flags], 60)
        try:
            return json.loads(out or "{}")
        except ValueError as e:
            raise RuntimeError(f"temporal {' '.join(args[:3])}: output is not JSON") from e

    def version(self, deployment: str, build: str) -> dict | None:
        """describe-version; {} when no poller of the version ever reached the server (so
        no run can be pinned to it), None when it cannot be read (never removed, only
        waited for)."""
        try:
            out = self.call("worker", "deployment", "describe-version", "--deployment-name", deployment, "--build-id", build)
            return out if isinstance(out, dict) else None
        except RuntimeError as e:
            return {} if "Worker Deployment Version not found" in str(e) else None
        except (ValueError, subprocess.TimeoutExpired):
            return None

    def routing(self, deployment: str) -> dict | None:
        """The deployment's routing configuration; {} when the deployment does not exist
        yet (no versioned poller ever reached the server), None when it cannot be read."""
        try:
            out = self.call("worker", "deployment", "describe", "--name", deployment)
            return (out.get("routingConfig") or {}) if isinstance(out, dict) else None
        except RuntimeError as e:
            return {} if "no Worker Deployment found" in str(e) else None
        except (ValueError, subprocess.TimeoutExpired):
            return None

    def set_current(self, deployment: str, build: str) -> None:
        self.call("worker", "deployment", "set-current-version", "--deployment-name", deployment, "--build-id", build, "--yes")

    def count(self, query: str) -> int | None:
        try:
            out = self.call("workflow", "count", "--query", query)
            return int(out.get("count", 0)) if isinstance(out, dict) else None
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            return None


class Clock:
    now = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    @staticmethod
    def log(message: str) -> None:
        print(f"{utc_now()} {message}", flush=True)


def version_key(build: str) -> str:
    return hashlib.sha256(build.encode()).hexdigest()[:10]


def ts(value: str | None) -> datetime.datetime | None:
    """A server timestamp (RFC 3339, any number of fraction digits)."""
    if not value:
        return None
    head, _, frac = value.rstrip("Z").partition(".")
    return datetime.datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}+00:00")


def queue_set(info: dict | None) -> set[tuple[str, str]]:
    return {(q.get("name"), q.get("type")) for q in (info or {}).get("taskQueuesInfos") or []}


def target_of(docs: list[dict]) -> dict:
    """The rendered worker version: its Deployment's name, Build ID, Temporal deployment
    and task queues, and what the rollout refuses."""
    deps = [d for d in docs if d.get("kind") == "Deployment"]
    if len(deps) != 1:
        raise ValueError(f"the render holds {len(deps)} Deployments, not one worker")
    dep = deps[0]
    meta = dep["metadata"]
    ann = meta.get("annotations") or {}
    if VERSION_LABEL not in (meta.get("labels") or {}) or BUILD_ANNOTATION not in ann:
        raise ValueError("the render is unversioned: set config.temporal.versioning.enabled (a plain rolling update needs no rollout)")
    cms = [d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == meta["name"]]
    if not cms:
        raise ValueError(f"the render holds no ConfigMap {meta['name']}")
    temporal = yaml.safe_load(cms[0]["data"]["config.yaml"])["temporal"]
    if temporal.get("versioning", {}).get("promote"):
        raise ValueError("config.temporal.versioning.promote makes every new Pod promote itself; the rollout promotes only a ready version")
    build = ann[BUILD_ANNOTATION]
    if meta["labels"][VERSION_LABEL] != version_key(build):
        raise ValueError(f"label {VERSION_LABEL} does not match the Build ID {build}")
    return {"name": meta["name"], "buildId": build, "deployment": ann[DEPLOYMENT_ANNOTATION],
            "namespace": temporal.get("namespace"), "queues": [temporal["task_queue"], temporal["control_task_queue"]]}


def previous_of(workers: list[dict], target: dict) -> list[dict]:
    out = []
    for d in workers:
        meta = d["metadata"]
        if meta["name"] == target["name"]:
            continue
        ann = meta.get("annotations") or {}
        grace = d.get("spec", {}).get("template", {}).get("spec", {}).get("terminationGracePeriodSeconds", 45)
        out.append({"name": meta["name"], "buildId": ann.get(BUILD_ANNOTATION), "deployment": ann.get(DEPLOYMENT_ANNOTATION),
                    "generation": meta.get("generation"), "graceSeconds": int(grace)})
    return out


def pinned_query(deployment: str, build: str) -> str:
    return f'TemporalWorkerDeploymentVersion = "{deployment}:{build}" AND ExecutionStatus = "Running"'


def removable(prev: dict, info: dict | None, routing: dict | None, open_runs: int | None, was_current: bool) -> tuple[bool, str]:
    """Whether a previous versioned build may be removed, and why not. DRAINED is
    Temporal's own verdict (it waits a visibility grace period after the last pinned run
    closed); it counts only when checked after the version's last routing change, and
    the visibility count of open runs pinned to the build must agree. A build that was
    never current or ramping (no drainage status: an aborted rollout's) has no pinned
    runs unless an override put some there, which the count shows."""
    build = prev["buildId"]
    if routing is None or info is None or open_runs is None:
        return False, "unknown (the routing, describe-version or the visibility count failed)"
    if build in (routing.get("currentVersionBuildID"), routing.get("rampingVersionBuildID")):
        return False, "current or ramping"
    if open_runs:
        return False, f"{open_runs} open pinned runs"
    if not info:
        return (True, "never registered") if not was_current else (False, "the previous current version is not found")
    drainage = info.get("drainageInfo") or {}
    status = drainage.get("drainageStatus") or "unspecified"
    if status == "drained":
        checked, changed = ts(drainage.get("lastCheckedTime")), ts(info.get("routingChangedTime"))
        if checked is None or (changed is not None and checked < changed):
            return False, "drained before the version's last routing change"
        return True, "drained"
    if status == "unspecified" and not was_current:
        return True, "never routed"
    return False, status


def drain_and_remove(kube, tmp, clock, record: dict, previous: list[dict], deployment: str, a, was_current: set[str]) -> str:
    """Steps 4-5 for every previous build; the routing must already name the current one."""
    routing = tmp.routing(deployment) or {}
    current = routing.get("currentVersionBuildID")
    pending = []
    for prev in previous:
        if prev["buildId"] is None:
            # The unversioned predecessor: its runs are not pinned; they move to the
            # current version on their next task.
            entry = {"name": prev["name"], "buildId": None, "unversionedOpenRuns": None}
            if not current:
                entry["result"] = "kept: no current version"
                record["kept"].append(entry)
                continue
            entry["unversionedOpenRuns"] = [tmp.count(f'TaskQueue = "{q}" AND ExecutionStatus = "Running" AND TemporalWorkerDeploymentVersion IS NULL')
                                            for q in record.get("target", {}).get("queues", [])]
            kube.remove(prev["name"], prev["graceSeconds"] + 60)
            clock.log(f"removed unversioned {prev['name']} (open unversioned runs per queue: {entry['unversionedOpenRuns']})")
            entry["result"], entry["at"] = "removed after the promotion", utc_now()
            record["removed"].append(entry)
            continue
        if prev["deployment"] != deployment:
            record["kept"].append({"name": prev["name"], "buildId": prev["buildId"], "result": f"kept: worker deployment {prev['deployment']}, not {deployment}"})
            continue
        pending.append({"prev": prev, "polls": []})
    deadline = clock.now() + a.drain_timeout
    while pending:
        routing = tmp.routing(deployment)
        for p in list(pending):
            prev = p["prev"]
            info = tmp.version(deployment, prev["buildId"])
            open_runs = tmp.count(pinned_query(deployment, prev["buildId"]))
            ok, why = removable(prev, info, routing, open_runs, prev["buildId"] in was_current)
            p["polls"].append({"at": utc_now(), "status": ((info or {}).get("drainageInfo") or {}).get("drainageStatus"), "openPinnedRuns": open_runs, "verdict": why})
            clock.log(f"drain {prev['name']} build={prev['buildId']} openPinnedRuns={open_runs} -> {why}")
            if ok:
                kube.remove(prev["name"], prev["graceSeconds"] + 60)
                clock.log(f"removed {prev['name']} build={prev['buildId']}")
                record["removed"].append({"name": prev["name"], "buildId": prev["buildId"], "result": f"removed: {why}", "at": utc_now(), "polls": p["polls"]})
                pending.remove(p)
        if not pending:
            break
        if clock.now() >= deadline:
            for p in pending:
                record["kept"].append({"name": p["prev"]["name"], "buildId": p["prev"]["buildId"], "result": f"kept: {p['polls'][-1]['verdict']} at the drain bound",
                                       "polls": p["polls"]})
            return "DRAINING"
        clock.sleep(a.poll)
    return "COMPLETE"


def rollout(kube, tmp, clock, a, record: dict) -> str:
    try:
        target = target_of(kube.render())
    except (ValueError, KeyError, TypeError) as e:
        record["reason"] = str(e)
        return "REFUSED"
    record["target"] = target
    if target["namespace"] != a.temporal_namespace:
        record["reason"] = f"the render's Temporal namespace {target['namespace']} is not --temporal-namespace {a.temporal_namespace}"
        return "REFUSED"
    deployment = target["deployment"]
    previous = previous_of(kube.workers(), target)
    record["previous"] = previous
    foreign = [p["name"] for p in previous if p["buildId"] is not None and p["deployment"] != deployment]
    if foreign:
        record["reason"] = f"previous builds of another worker deployment ({', '.join(foreign)}): renaming the deployment strands their pinned runs"
        return "REFUSED"
    routing = tmp.routing(deployment)
    record["routingBefore"] = routing
    if routing is None:
        record["reason"] = f"the routing of worker deployment {deployment} cannot be read"
        return "REFUSED"
    current = routing.get("currentVersionBuildID") or None
    if current and current != target["buildId"] and current not in {p["buildId"] for p in previous}:
        record["reason"] = f"the current version {current} has no Deployment in this release: retiring its workers is not this tool's to decide"
        return "REFUSED"
    current_queues = queue_set(tmp.version(deployment, current)) if current and current != target["buildId"] else set()
    # 1. install
    started = clock.now()
    if not a.no_install:
        for p in previous:
            if p["buildId"] is None:
                record["steps"].append({"step": "keep", "name": p["name"], "objects": kube.keep(p["name"]), "at": utc_now()})
        kube.install()
        clock.log(f"installed {target['name']} (build {target['buildId']}) beside {[p['name'] for p in previous]}")
        record["steps"].append({"step": "install", "release": a.release, "at": utc_now()})
    after = {d["metadata"]["name"]: d["metadata"].get("generation") for d in kube.workers()}
    changed = [p["name"] for p in previous if after.get(p["name"]) != p["generation"]]
    record["steps"].append({"step": "previous-unchanged", "changed": changed, "at": utc_now()})
    if changed:
        record["reason"] = f"previous worker Deployments missing or changed by the install: {', '.join(changed)}"
        return "FAILED"
    # 2. ready
    remaining = max(int(a.ready_timeout - (clock.now() - started)), 1)
    if not kube.ready(target["name"], remaining):
        record["steps"].append({"step": "ready", "ok": False, "at": utc_now()})
        record["reason"] = f"{target['name']} did not become ready within {a.ready_timeout}s: nothing promoted, the current version stays {current}"
        return "ABORTED"
    needed = {(q, "workflow") for q in target["queues"][:1]} | current_queues
    names = set(target["queues"])
    while True:
        registered = queue_set(tmp.version(deployment, target["buildId"]))
        if names <= {n for n, _ in registered} and needed <= registered:
            break
        if clock.now() - started >= a.ready_timeout:
            record["steps"].append({"step": "ready", "ok": False, "registered": sorted(map(list, registered)), "at": utc_now()})
            record["reason"] = f"build {target['buildId']} did not register its task queues within {a.ready_timeout}s: nothing promoted, the current version stays {current}"
            return "ABORTED"
        clock.sleep(a.poll)
    record["steps"].append({"step": "ready", "ok": True, "registered": sorted(map(list, registered)), "at": utc_now()})
    clock.log(f"{target['name']} ready; build {target['buildId']} polls {sorted(registered)}")
    # 3. promote
    if current != target["buildId"]:
        try:
            tmp.set_current(deployment, target["buildId"])
        except RuntimeError as e:
            record["reason"] = f"set-current-version refused: {e}"
            return "ABORTED"
    routing = tmp.routing(deployment) or {}
    record["steps"].append({"step": "promote", "current": routing.get("currentVersionBuildID"), "at": utc_now()})
    clock.log(f"current version of {deployment}: {routing.get('currentVersionBuildID')} (was {current})")
    if routing.get("currentVersionBuildID") != target["buildId"]:
        record["reason"] = f"the routing names {routing.get('currentVersionBuildID')} as current after the promotion"
        return "FAILED"
    # 4-5. drain and remove
    return drain_and_remove(kube, tmp, clock, record, previous, deployment, a, {current} if current else set())


def retire(kube, tmp, clock, a, record: dict) -> str:
    workers = kube.workers()
    deployments = {(d["metadata"].get("annotations") or {}).get(DEPLOYMENT_ANNOTATION) for d in workers} - {None}
    if len(deployments) != 1:
        record["reason"] = f"the release's versioned Deployments name {len(deployments)} worker deployments"
        return "REFUSED"
    deployment = deployments.pop()
    current = (tmp.routing(deployment) or {}).get("currentVersionBuildID")
    target = next((d for d in workers if (d["metadata"].get("annotations") or {}).get(BUILD_ANNOTATION) == current), None)
    if target is None:
        record["reason"] = f"the current version {current} of {deployment} has no Deployment in this release"
        return "REFUSED"
    record["target"] = {"name": target["metadata"]["name"], "buildId": current, "deployment": deployment}
    record["previous"] = previous_of(workers, record["target"])
    return drain_and_remove(kube, tmp, clock, record, record["previous"], deployment, a, set())


def execute(kube, tmp, clock, a) -> tuple[str, dict]:
    record: dict = {"command": a.command, "release": a.release, "namespace": a.namespace, "context": a.context,
                    "temporal": {"address": a.temporal_address, "namespace": a.temporal_namespace}, "since": utc_now(),
                    "steps": [], "removed": [], "kept": []}
    try:
        status = (rollout if a.command == "rollout" else retire)(kube, tmp, clock, a, record)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        # A failed kubectl, helm or CLI call: everything not yet removed is kept.
        status, record["reason"] = "FAILED", str(e)
    record.update({"until": utc_now(), "status": status})
    return status, record


# --- self-test: a fake cluster and Temporal under a fake clock ---------------------------


class FakeClock:
    def __init__(self, world: "World"):
        self.t, self.world = 0.0, world

    def log(self, message: str) -> None:
        self.world.log.append(message)

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        self.world.tick(self.t)


class World:
    """One release with build A current (or an unversioned A), B rendered by the next release."""

    def __init__(self, a_build: str | None = "A", open_runs: int = 2, b_ready: bool = True, b_registers: bool = True,
                 drains_at: float | None = 200, runs_end_at: float = 30, stale_drained: bool = False, versioned_render: bool = True,
                 promote: bool = False):
        self.calls: list[str] = []
        self.deployment = "anvilkit-agent-workflow"
        self.queues = [("anvilkit-workflow", "workflow"), ("anvilkit-workflow", "activity"), ("anvilkit-workflow-control", "activity")]
        self.workers = {self.name(a_build): self.worker(a_build)}
        self.versions = {}
        self.routing = {"currentVersionBuildID": "", "currentVersionChangedTime": "2026-10-18T00:00:00Z", "rampingVersionBuildID": ""}
        if a_build:
            self.versions[a_build] = {"taskQueuesInfos": [{"name": n, "type": t} for n, t in self.queues], "drainageInfo": None}
            self.routing["currentVersionBuildID"] = a_build
        self.open = {a_build: open_runs} if a_build else {}
        self.b_ready, self.b_registers, self.drains_at, self.runs_end_at = b_ready, b_registers, drains_at, runs_end_at
        self.stale_drained, self.versioned_render, self.promote = stale_drained, versioned_render, promote
        self.routing_unreadable, self.install_changes_a = False, False
        self.log: list[str] = []
        self.promoted_at: float | None = None
        self.t = 0.0

    @staticmethod
    def name(build: str | None) -> str:
        return "anvilkit-agent-workflow" + (f"-{version_key(build)}" if build else "")

    def worker(self, build: str | None) -> dict:
        ann = {BUILD_ANNOTATION: build, DEPLOYMENT_ANNOTATION: self.deployment} if build else {}
        return {"metadata": {"name": self.name(build), "generation": 1, "annotations": ann,
                             "labels": {VERSION_LABEL: version_key(build)} if build else {}},
                "spec": {"template": {"spec": {"terminationGracePeriodSeconds": 45}}}}

    def stamp(self, t: float) -> str:
        return (datetime.datetime(2026, 10, 18, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def tick(self, t: float) -> None:
        self.t = t
        if self.promoted_at is None:
            return
        for build, info in self.versions.items():
            if build == "B" or info.get("drainageInfo") is None:
                continue
            if t >= self.promoted_at + self.runs_end_at:
                self.open[build] = 0
            if self.drains_at is not None and t >= self.promoted_at + self.drains_at:
                info["drainageInfo"] = {"drainageStatus": "drained", "lastCheckedTime": self.stamp(t)}


class FakeKube:
    def __init__(self, w: World):
        self.w = w

    def render(self) -> list[dict]:
        dep = self.w.worker("B" if self.w.versioned_render else None)
        cm = {"kind": "ConfigMap", "metadata": {"name": dep["metadata"]["name"]}, "data": {"config.yaml": yaml.safe_dump({"temporal": {
            "namespace": "anvilkit", "task_queue": "anvilkit-workflow", "control_task_queue": "anvilkit-workflow-control",
            "versioning": {"enabled": self.w.versioned_render, "deployment_name": self.w.deployment, "promote": self.w.promote}}})}}
        return [{"kind": "Deployment", **dep}, cm]

    def install(self) -> None:
        self.w.calls.append("install")
        self.w.workers[self.w.name("B")] = self.w.worker("B")
        if self.w.install_changes_a:
            self.w.workers[self.w.name("A")]["metadata"]["generation"] = 2
        if self.w.b_ready and self.w.b_registers:
            self.w.versions["B"] = {"taskQueuesInfos": [{"name": n, "type": t} for n, t in self.w.queues], "drainageInfo": None}

    def workers(self) -> list[dict]:
        return [json.loads(json.dumps(d)) for d in self.w.workers.values()]

    def keep(self, name: str) -> list[str]:
        self.w.calls.append(f"keep {name}")
        return [f"deployment.apps/{name}"]

    def ready(self, name: str, seconds: int) -> bool:
        self.w.calls.append(f"ready {name}")
        return self.w.b_ready

    def remove(self, name: str, seconds: int) -> None:
        self.w.calls.append(f"remove {name} at {self.w.t:.0f}")
        self.w.workers.pop(name, None)


class FakeTemporal:
    def __init__(self, w: World):
        self.w = w

    def version(self, deployment: str, build: str) -> dict | None:
        return json.loads(json.dumps(self.w.versions[build])) if build in self.w.versions else {}

    def routing(self, deployment: str) -> dict | None:
        return None if self.w.routing_unreadable else dict(self.w.routing)

    def set_current(self, deployment: str, build: str) -> None:
        self.w.calls.append(f"set-current {build}")
        old = self.w.routing["currentVersionBuildID"]
        self.w.routing.update({"currentVersionBuildID": build, "currentVersionChangedTime": self.w.stamp(self.w.t)})
        self.w.promoted_at = self.w.t
        if old in self.w.versions:
            # A stale record would be a DRAINED checked before this demotion.
            self.w.versions[old]["routingChangedTime"] = self.w.stamp(self.w.t)
            self.w.versions[old]["drainageInfo"] = {"drainageStatus": "drained" if self.w.stale_drained else "draining",
                                                    "lastCheckedTime": self.w.stamp(self.w.t - 1 if self.w.stale_drained else self.w.t)}

    def count(self, query: str) -> int | None:
        if "IS NULL" in query:
            return 1
        build = query.split(":", 1)[1].split('"', 1)[0]
        return self.w.open.get(build, 0)


def self_test() -> int:
    def go(world: World, command: str = "rollout", drain_timeout: int = 900) -> tuple[str, dict]:
        a = argparse.Namespace(command=command, release="anvilkit-agent-workflow", namespace="anvilkit-apps", context=None,
                               temporal_address="t:7233", temporal_namespace="anvilkit", ready_timeout=300, drain_timeout=drain_timeout,
                               poll=15, no_install=False)
        return execute(FakeKube(world), FakeTemporal(world), FakeClock(world), a)

    a_name, b_name = World.name("A"), World.name("B")
    cases: dict[str, bool] = {}

    w = World()
    status, rec = go(w)
    removal = next((c for c in w.calls if c.startswith(f"remove {a_name}")), "")
    polls = rec["removed"][0]["polls"] if rec["removed"] else []
    cases["removes A once it drained"] = (status == "COMPLETE" and w.calls[:3] == ["install", f"ready {b_name}", "set-current B"]
                                          and bool(removal) and float(removal.rsplit(" ", 1)[1]) >= 200 and a_name not in w.workers)
    cases["reports the open pinned runs while draining"] = [p["openPinnedRuns"] for p in polls][:1] == [2] and polls[-1]["openPinnedRuns"] == 0

    w = World(drains_at=None, runs_end_at=10**9, open_runs=1)
    status, rec = go(w, drain_timeout=300)
    cases["refuses removal while draining"] = (status == "DRAINING" and EXIT[status] == 3 and a_name in w.workers
                                               and not any(c.startswith("remove") for c in w.calls) and rec["kept"][0]["name"] == a_name)
    status, rec = go(w, command="retire", drain_timeout=60)
    cases["retire keeps a draining build"] = status == "DRAINING" and a_name in w.workers
    w.drains_at, w.runs_end_at = 0, 0
    w.tick(w.t)
    status, rec = go(w, command="retire")
    cases["retire removes it once drained"] = status == "COMPLETE" and a_name not in w.workers and b_name in w.workers

    w = World(b_ready=False)
    status, rec = go(w)
    cases["aborts and keeps A when B never becomes ready"] = (status == "ABORTED" and EXIT[status] == 1 and w.routing["currentVersionBuildID"] == "A"
                                                              and a_name in w.workers and not any(c.startswith(("set-current", "remove")) for c in w.calls))
    w = World(b_registers=False)
    status, rec = go(w)
    cases["aborts when B's pollers never register"] = (status == "ABORTED" and w.routing["currentVersionBuildID"] == "A" and a_name in w.workers
                                                       and "set-current B" not in w.calls)

    status, rec = go(w, command="retire")
    cases["retire removes the aborted build that never registered"] = status == "COMPLETE" and b_name not in w.workers and a_name in w.workers

    w = World(stale_drained=True, drains_at=None, runs_end_at=0)
    status, rec = go(w, drain_timeout=120)
    cases["a drained status older than the promotion does not count"] = status == "DRAINING" and a_name in w.workers

    w = World(runs_end_at=10**9, open_runs=1)
    status, rec = go(w, drain_timeout=600)
    cases["drained with open pinned runs in visibility is kept"] = status == "DRAINING" and a_name in w.workers

    w = World(a_build=None)
    status, rec = go(w)
    unversioned = World.name(None)
    cases["first enable keeps the unversioned Deployment through the install"] = w.calls[:2] == [f"keep {unversioned}", "install"]
    cases["first enable removes it once B is current"] = (status == "COMPLETE" and unversioned not in w.workers
                                                          and rec["removed"][0]["unversionedOpenRuns"] == [1, 1])

    w = World()
    w.install_changes_a = True
    status, rec = go(w)
    cases["an install that changed A fails before any promotion"] = (status == "FAILED" and "set-current B" not in w.calls
                                                                       and not any(c.startswith("remove") for c in w.calls))

    w = World()
    w.routing_unreadable = True
    status, rec = go(w)
    cases["refuses when the routing cannot be read"] = status == "REFUSED" and not w.calls

    cases["timestamps of any precision compare in time order"] = (ts("2026-10-18T19:00:04.1383495Z") > ts("2026-10-18T19:00:03.652334473Z")
                                                                  and ts("2026-10-18T19:00:04.15Z") > ts("2026-10-18T19:00:04.1383495Z")
                                                                  and ts("2026-10-18T19:00:04Z") < ts("2026-10-18T19:00:04.000001Z"))

    w = World(versioned_render=False)
    status, rec = go(w)
    cases["refuses an unversioned render"] = status == "REFUSED" and EXIT[status] == 2 and not w.calls
    w = World(promote=True)
    status, rec = go(w)
    cases["refuses a self-promoting render"] = status == "REFUSED" and not w.calls

    failed = [name for name, ok in cases.items() if not ok]
    for name, ok in cases.items():
        print(f"{'PASS' if ok else 'FAIL'}  self-test: {name}")
    print(f"workflow-rollout self-test: {len(cases) - len(failed)}/{len(cases)} PASS")
    return 1 if failed else 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("rollout", "retire"):
        p = sub.add_parser(name)
        p.add_argument("--namespace", required=True, help="the release's Kubernetes namespace")
        p.add_argument("--release", default=NAME)
        p.add_argument("--context")
        p.add_argument("--kubeconfig")
        p.add_argument("--temporal-address", required=True)
        p.add_argument("--temporal-namespace", default="anvilkit")
        p.add_argument("--temporal-cli", default="temporal", help="the Temporal CLI command; TLS flags may be part of it as --flag=value")
        p.add_argument("--drain-timeout", type=int, default=3600, help="seconds to wait for previous builds to drain")
        p.add_argument("--poll", type=int, default=15)
        p.add_argument("--out")
        if name == "rollout":
            p.add_argument("-f", "--values", action="append", default=[])
            p.add_argument("--set", action="append", default=[])
            p.add_argument("--chart", default=str(CHART))
            p.add_argument("--ready-timeout", type=int, default=300)
            p.add_argument("--no-install", action="store_true", help="B's Deployment was applied by the environment's GitOps")
    a = ap.parse_args()
    status, record = execute(Kube(a), Temporal(a), Clock(), a)
    out = pathlib.Path(a.out) if a.out else ROOT / "outputs/workflow-rollout" / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.command}.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({k: record.get(k) for k in ("status", "reason", "removed", "kept")}, indent=1, default=str))
    return EXIT[status]


if __name__ == "__main__":
    sys.exit(main())
