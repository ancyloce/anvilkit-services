#!/usr/bin/env python3
"""Failure drills of the P23 qualification environment (P23-07; DEVELOPMENT_ONLY placement).

  .local/verification-venv/bin/python deploy/qualification/drills.py --run RUN DRILL [DRILL ...]

Drills (each injects one fault into the qualification cluster while a business load runs):
  node-loss      stop the worker node of zone qa-b (its container), keep it down past the
                 Pod eviction timeout, start it again
  db-primary     force-delete the business PostgreSQL primary Pod (CloudNativePG fails over)
  queue-writes   create background requests through both owners, then force-delete the
                 queue Valkey primary before the Worker drains them (Sentinel fails over,
                 asynchronously replicated writes may be lost; owners must rebuild them)
  broker         force-delete two of the three NATS servers (JetStream loses quorum) while
                 owners commit outbox facts; the outbox must drain after the servers return
  storage        pause the DR object store that holds Control's obligation inventory: no
                 operation may be admitted without its inventory record; resume it
  control-plane  stop one control-plane node (quorum kept), then a second (etcd quorum
                 lost: the Kubernetes API is unavailable, running Pods keep serving)
  secret-rotation  (P23-05) rotate the database credentials of Control (environment: a
                 rolling restart under its PodDisruptionBudget), Knowledge and MCP (mounted
                 files: a new configuration generation, the old pool drained, no restart):
                 CloudNativePG applies each role's new password from its Secret; the old
                 password must be refused afterwards and every live session must use the new one

The load is the environment's business probe at a fixed rate: GET /readyz and a LocalCheck
submission through a NodePort on a live worker (never through a port-forward that dies with
its Pod). Afterwards every operation the API accepted (202) must exist exactly once and
reach a terminal lifecycle, and no operation may own more than one Job. Each drill writes
outputs/qualification/<RUN>/drills/<drill>.json: the fault's timestamps, the availability
timeline, the unavailability window, the time from fault to restored service (RTO), lost
acknowledged operations (an RPO observation), and the drill's own checks. Exit 1 when a
drill's check failed. Nothing here measures a real failure domain: every node shares one host.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / ".local/qualification"
KUBECONFIG = STATE / "kubeconfig"
KUBECTL = str(ROOT / ".local/bin/kubectl")
CLUSTER = "anvilkit-qualification"
SUBJECT = "sha256:0dc7fa9db7237a2b5c96f70f59bb00f73bb86a0ca5554e91c312f9ada26e18b3"
MC = "minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727"  # host-cached; runs on this host
TERMINAL = {"succeeded", "failed", "canceled", "rejected", "expired"}


def k(*args: str, stdin: str | None = None, check: bool = True, timeout: int = 60) -> str:
    p = subprocess.run([KUBECTL, "--kubeconfig", str(KUBECONFIG), "--request-timeout", f"{timeout}s", *args],
                       input=stdin, capture_output=True, text=True, timeout=timeout + 10)
    if check and p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:4])}: {p.stderr.strip()[-300:]}")
    return p.stdout


def docker(*args: str) -> str:
    p = subprocess.run(["docker", *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {p.stderr.strip()[-300:]}")
    return p.stdout.strip()


def node_ip(node: str) -> str:
    return docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", node)


def workers() -> list[str]:
    return [n for n in docker("ps", "-a", "--filter", f"label=io.x-k8s.kind.cluster={CLUSTER}", "--format", "{{.Names}}").split()
            if "worker" in n]


def now() -> float:
    return time.time()


class Load:
    """The business probe: readiness every interval, a LocalCheck every submit_every."""

    def __init__(self, token: str, node_port: int, interval: float = 1.0, submit_every: float = 5.0) -> None:
        self.token, self.port, self.interval, self.submit_every = token, node_port, interval, submit_every
        self.samples: list[dict] = []
        self.accepted: list[dict] = []
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.seq = 0

    def call(self, method: str, path: str, body: bytes | None = None, timeout: float = 5.0) -> tuple[int, dict | None, str]:
        last = (0, None, "no live worker")
        for node in sorted(workers()):
            if docker("inspect", "-f", "{{.State.Running}}", node) != "true":
                continue
            req = urllib.request.Request(f"http://{node_ip(node)}:{self.port}{path}", data=body, method=method,
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                    return r.status, (json.loads(raw) if raw else None), node
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    return e.code, json.loads(raw), node
                except ValueError:
                    return e.code, None, node
            except OSError as e:
                last = (0, None, f"{node}: {e}")
        return last

    def loop(self) -> None:
        next_submit = now()
        while not self.stop_flag.is_set():
            t = now()
            code, _, via = self.call("GET", "/readyz", timeout=2.0)
            self.samples.append({"t": t, "kind": "ready", "ok": 200 <= code < 300, "code": code, "via": via})  # the API answers 204
            if t >= next_submit:
                next_submit = t + self.submit_every
                self.seq += 1
                cmd = f"drill-{int(t)}-{self.seq}"
                body = json.dumps({"commandId": cmd, "kind": "local_check", "subject": {"profileId": "local-check-v1", "subjectDigest": SUBJECT}}).encode()
                code, out, via = self.call("POST", "/api/v1/operations", body, timeout=10.0)
                ok = code == 202 and out and out.get("operationId")
                self.samples.append({"t": t, "done": now(), "kind": "submit", "ok": bool(ok), "code": code, "via": via, "commandId": cmd})
                if ok:
                    self.accepted.append({"t": t, "done": now(), "commandId": cmd, "operationId": out["operationId"]})
            self.stop_flag.wait(self.interval)

    def start(self) -> "Load":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_flag.set()
        self.thread.join(timeout=30)

    def verify(self, wait: float = 600.0) -> dict:
        """Every accepted operation exists once and ends; no operation owns two Jobs."""
        pending = {a["operationId"]: a for a in self.accepted}
        final: dict[str, str] = {}
        end = now() + wait
        while pending and now() < end:
            for op_id in list(pending):
                code, out, _ = self.call("GET", f"/api/v1/operations/{op_id}")
                if code == 200 and out and out.get("lifecycle") in TERMINAL:
                    final[op_id] = out["lifecycle"]
                    pending.pop(op_id)
                elif code == 404:
                    final[op_id] = "lost"
                    pending.pop(op_id)
            time.sleep(5)
        jobs = json.loads(k("-n", "anvilkit-components", "get", "jobs", "-o", "json"))["items"]
        per_op: dict[str, int] = {}
        for j in jobs:
            op = j["metadata"].get("labels", {}).get("anvilkit.io/operation-id")
            if op:
                per_op[op] = per_op.get(op, 0) + 1
        lifecycles: dict[str, int] = {}
        for v in final.values():
            lifecycles[v] = lifecycles.get(v, 0) + 1
        return {"accepted": len(self.accepted), "lifecycles": lifecycles, "unfinished": sorted(pending), "lost": sorted(o for o, v in final.items() if v == "lost"),
                "operationsWithSeveralJobs": sorted(o for o, n in per_op.items() if n > 1)}

    def window(self, fault_at: float) -> dict:
        """Unavailability around the fault: first and last failed probe, time to steady success."""
        after = [s for s in self.samples if s["t"] >= fault_at]
        failed = [s for s in after if not s["ok"]]
        if not failed:
            return {"failedProbes": 0, "unavailableSeconds": 0.0, "recoverySeconds": 0.0}
        last_fail = max(s["t"] for s in failed)
        recovered = next((s["t"] for s in after if s["t"] > last_fail and s["ok"]), None)
        return {"failedProbes": len(failed), "failedSubmits": sum(1 for s in failed if s["kind"] == "submit"),
                "firstFailureAfterFault": round(min(s["t"] for s in failed) - fault_at, 1),
                "unavailableSeconds": round(last_fail - min(s["t"] for s in failed), 1),
                "recoverySeconds": round((recovered or last_fail) - fault_at, 1), "recovered": recovered is not None}


def drill_service() -> int:
    """A NodePort Service on the API Pods, owned by the drill runner."""
    svc = {"apiVersion": "v1", "kind": "Service",
           "metadata": {"name": "anvilkit-drill-api", "namespace": "anvilkit-apps", "labels": {"anvilkit.io/drill": "qualification"}},
           "spec": {"type": "NodePort", "selector": {"app.kubernetes.io/name": "anvilkit-agent-api"},
                    "ports": [{"port": 80, "targetPort": "http", "nodePort": 30910}]}}
    k("apply", "-f", "-", stdin=json.dumps(svc))
    return 30910


def wait_until(check, timeout: float, step: float = 5.0) -> float | None:
    start = now()
    while now() - start < timeout:
        try:
            if check():
                return now()
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        time.sleep(step)
    return None


UNSEALS: list[float] = []
UNHEALTHY: list[str] = []  # the Applications not Healthy at the last check


def apps_healthy() -> bool:
    # A restarted OpenBao member is sealed: the operator's unseal step (bootstrap.py with
    # the shares held outside the cluster; production unseals through ENV-10's KMS).
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import bootstrap
    before = bootstrap.kubectl("-n", "anvilkit-platform", "get", "pods", "-l", "app.kubernetes.io/name=openbao", "-o", "jsonpath={.items[*].status.containerStatuses[*].ready}")
    bootstrap.openbao(bootstrap.credentials())
    if "false" in before:
        UNSEALS.append(now())
    apps = json.loads(k("-n", "anvilkit-platform", "get", "applications.argoproj.io", "-o", "json"))["items"]
    UNHEALTHY[:] = sorted(f"{a['metadata']['name']}={a.get('status', {}).get('health', {}).get('status')}" for a in apps
                          if a.get("status", {}).get("health", {}).get("status") != "Healthy")
    return bool(apps) and not UNHEALTHY


def node_ready(name: str) -> bool:
    conds = json.loads(k("get", "node", name, "-o", "json"))["status"]["conditions"]
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conds)


def pods_on(node: str) -> list[str]:
    out = k("get", "pods", "-A", "--field-selector", f"spec.nodeName={node}", "-o", "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{\"\\n\"}{end}")
    return [p for p in out.split() if p.startswith("anvilkit-")]


def psql(cluster: str, sql: str, db: str = "postgres") -> str:
    primary = json.loads(k("-n", "anvilkit-data", "get", "clusters.postgresql.cnpg.io", cluster, "-o", "json"))["status"]["currentPrimary"]
    return k("-n", "anvilkit-data", "exec", primary, "-c", "postgres", "--", "psql", "-At", "-d", db, "-c", sql).strip()


def drill_node_loss(load: Load) -> dict:
    node = f"{CLUSTER}-worker2"
    on_node = pods_on(node)
    primary_before = json.loads(k("-n", "anvilkit-data", "get", "clusters.postgresql.cnpg.io", "anvilkit-business", "-o", "json"))["status"]["currentPrimary"]
    fault = now()
    docker("stop", node)
    not_ready = wait_until(lambda: not node_ready(node), 180)
    time.sleep(max(0, 360 - (now() - fault)))  # past the 300 s not-ready eviction toleration
    primary_during = json.loads(k("-n", "anvilkit-data", "get", "clusters.postgresql.cnpg.io", "anvilkit-business", "-o", "json"))["status"]["currentPrimary"]
    restart = now()
    docker("start", node)
    ready = wait_until(lambda: node_ready(node), 300)
    healthy = wait_until(apps_healthy, 900, 15)
    return {"node": node, "zone": "qa-b", "anvilkitPodsOnNode": on_node, "faultAt": fault, "nodeNotReadyAfter": round((not_ready or now()) - fault, 1),
            "businessPrimaryBefore": primary_before, "businessPrimaryDuring": primary_during, "restartedAt": restart,
            "nodeReadyAfterRestart": round((ready or now()) - restart, 1) if ready else None,
            "environmentHealthyAfterRestart": round(healthy - restart, 1) if healthy else None, "window": load.window(fault),
            "checks": {"environmentRecovered": healthy is not None}}


def drill_db_primary(load: Load) -> dict:
    status = lambda: json.loads(k("-n", "anvilkit-data", "get", "clusters.postgresql.cnpg.io", "anvilkit-business", "-o", "json"))["status"]  # noqa: E731
    before = status()["currentPrimary"]
    fault = now()
    k("-n", "anvilkit-data", "delete", "pod", before, "--grace-period=0", "--force", "--wait=false")
    switched = wait_until(lambda: status().get("currentPrimary") not in (None, before), 300, 2)
    ready = wait_until(lambda: status().get("readyInstances") == 3, 600, 5)
    return {"primaryBefore": before, "primaryAfter": status().get("currentPrimary"), "faultAt": fault,
            "failoverSeconds": round(switched - fault, 1) if switched else None, "allInstancesReadyAfter": round(ready - fault, 1) if ready else None,
            "synchronous": psql("anvilkit-business", "SHOW synchronous_standby_names"), "window": load.window(fault),
            "checks": {"failedOver": switched is not None}}


# DEVELOPMENT_ONLY authorization fixtures the owners' local-check requests are bound to
# (the shapes tests/integration/background_integration_test.go seeds): one Knowledge
# source and one MCP server, approved descriptor and active grant, owned by the drills.
FIXTURES = {
    "anvilkit_knowledge": [
        "INSERT INTO sources (source_id, tenant_id, kind, locator, command_id, request_digest) VALUES ('src_drill', 'tenant_a', 'document', "
        "'file://qualification-drill', 'cmd_src_drill', 'sha256:0000000000000000000000000000000000000000000000000000000000000000') "
        "ON CONFLICT (source_id) DO UPDATE SET deleted = false"],
    "anvilkit_mcp": [
        "INSERT INTO servers (server_id, tenant_id, canonical_resource, transport) VALUES ('srv_drill', 'tenant_a', 'https://mcp.example/drill', "
        "'streamable-http') ON CONFLICT DO NOTHING",
        "INSERT INTO descriptors (server_id, revision, protocol_version, provenance, descriptor_digest, tools, state, command_id, request_digest, "
        "data_class, revision_evidence, discovered_by, reviewer, review_id) VALUES ('srv_drill', 1, '2025-06-18', 'test', "
        "'sha256:0000000000000000000000000000000000000000000000000000000000000001', '[]', 'approved', 'cmd_drill', "
        "'sha256:0000000000000000000000000000000000000000000000000000000000000002', 'internal', 'fixture', 'fixture-discoverer', "
        "'fixture-reviewer', 'rev_fixture') ON CONFLICT DO NOTHING",
        "INSERT INTO grants (grant_id, tenant_id, subject_type, subject_id, server_id, descriptor_revision, descriptor_digest, methods, purpose, "
        "cost_cap_currency, cost_cap_amount, state, expires_at, command_id, request_digest, canonical_resource, transport, protocol_version, "
        "audience, data_class, policy_digest, registration_command_id, control_receipt_id) VALUES ('g_drill', 'tenant_a', 'actor', 'user_a', "
        "'srv_drill', 1, 'sha256:0000000000000000000000000000000000000000000000000000000000000001', '{tools/call}', 'test', 'USD', 0, 'active', "
        "now() + interval '12 hours', 'cmd_grant_drill', 'sha256:0000000000000000000000000000000000000000000000000000000000000003', "
        "'https://mcp.example/drill', 'streamable-http', '2025-06-18', 'https://mcp.example/drill', 'internal', "
        "'sha256:0000000000000000000000000000000000000000000000000000000000000004', 'reg_drill', 'rcpt_fixture') ON CONFLICT (grant_id) DO UPDATE "
        "SET state = 'active', expires_at = now() + interval '12 hours', control_receipt_id = 'rcpt_fixture', revocation_command_id = NULL, "
        "control_state = 'none', revoked_at = NULL"],
}


def owner_request(owner: str, n: int, tag: str) -> list[str]:
    for db, statements in FIXTURES.items():
        for sql in statements:
            psql("anvilkit-business", sql, db)
    ids = []
    for i in range(n):
        if owner == "knowledge":
            out = k("-n", "anvilkit-apps", "exec", "deploy/anvilkit-agent-knowledge", "-c", "knowledge", "--",
                    "node", "/anvilkit/knowledge/dist/localcheck.js", "request", "--source", "src_drill", "--task-id", f"task_{tag}_{i}")
        else:
            out = k("-n", "anvilkit-apps", "exec", "deploy/anvilkit-agent-mcp", "-c", "mcp", "--",
                    "/usr/local/bin/anvilkit-agent-mcp", "local-check", "request", "-grant", "g_drill", "-task-id", f"task_{tag}_{i}")
        ids.append(json.loads(out)["taskId"])
    return ids


def task_states(owner: str, ids: list[str]) -> dict[str, str]:
    db = "anvilkit_knowledge" if owner == "knowledge" else "anvilkit_mcp"
    quoted = ",".join(f"'{i}'" for i in ids)
    # The owner's background_requests ledger (one row per task and generation; the latest counts).
    rows = psql("anvilkit-business", f"SELECT DISTINCT ON (task_id) task_id, state FROM background_requests WHERE task_id IN ({quoted}) "
                                     "ORDER BY task_id, generation DESC", db)
    return dict(line.split("|", 1) for line in rows.splitlines() if "|" in line)


def drill_queue_writes(load: Load) -> dict:
    sentinel = "anvilkit-valkey-queue-0"
    master = k("-n", "anvilkit-data", "exec", sentinel, "-c", "sentinel", "--", "sh", "-c",
               # the password is the container's own environment, never a command-line value
               'REDISCLI_AUTH="$VALKEY_PASSWORD" valkey-cli -p 26379 SENTINEL get-master-addr-by-name mymaster').split()
    tag = f"q{int(now())}"
    ids = {"knowledge": owner_request("knowledge", 5, tag), "mcp": owner_request("mcp", 5, tag)}
    master_pod = next(p for p in k("-n", "anvilkit-data", "get", "pods", "-o", "name").split() if master and master[0].split(".")[0] in p)
    fault = now()
    k("-n", "anvilkit-data", "delete", master_pod, "--grace-period=0", "--force", "--wait=false")
    done = wait_until(lambda: all(s == "accepted" for o in ids for s in task_states(o, ids[o]).values())
                      and all(len(task_states(o, ids[o])) == len(ids[o]) for o in ids), 900, 10)
    final = {o: task_states(o, ids[o]) for o in ids}
    return {"masterBefore": master[0] if master else None, "masterPod": master_pod, "faultAt": fault, "tasks": final,
            "allAcceptedAfter": round(done - fault, 1) if done else None, "window": load.window(fault),
            "checks": {"everyRequestAccepted": done is not None}}


def outbox_backlog(db: str) -> tuple[int, float]:
    # Watermill's PostgreSQL outbox (watermill-sql v4): a message is forwarded once its
    # (transaction id, offset) is at or before the consumer group's acknowledged position.
    row = psql("anvilkit-business", "SELECT count(*), coalesce(extract(epoch from localtimestamp - min(o.created_at)), 0) FROM outbox o "
               "LEFT JOIN outbox_offsets f ON true WHERE f.consumer_group IS NULL OR o.transaction_id > f.last_processed_transaction_id "
               "OR (o.transaction_id = f.last_processed_transaction_id AND o.\"offset\" > coalesce(f.offset_acked, 0))", db)
    n, age = row.split("|")
    return int(n), float(age)


def drill_broker(load: Load) -> dict:
    fault = now()
    for pod in ("anvilkit-nats-1", "anvilkit-nats-2"):
        k("-n", "anvilkit-data", "delete", "pod", pod, "--grace-period=0", "--force", "--wait=false")
    tag = f"b{int(now())}"
    ids = {"knowledge": owner_request("knowledge", 3, tag), "mcp": owner_request("mcp", 3, tag)}
    peak = {"knowledge": (0, 0.0), "mcp": (0, 0.0)}
    for _ in range(12):
        for o, db in (("knowledge", "anvilkit_knowledge"), ("mcp", "anvilkit_mcp")):
            try:
                n, age = outbox_backlog(db)
                peak[o] = max(peak[o], (n, age))
            except RuntimeError:
                pass
        time.sleep(5)
    quorum = wait_until(lambda: k("-n", "anvilkit-data", "get", "statefulset", "anvilkit-nats", "-o", "jsonpath={.status.readyReplicas}") == "3", 600, 5)
    drained = wait_until(lambda: all(outbox_backlog(db)[0] == 0 for db in ("anvilkit_knowledge", "anvilkit_mcp")), 600, 5)
    done = wait_until(lambda: all(s == "accepted" for o in ids for s in task_states(o, ids[o]).values()), 600, 10)
    return {"faultAt": fault, "peakBacklog": {o: {"messages": n, "oldestSeconds": round(a, 1)} for o, (n, a) in peak.items()},
            "quorumBackAfter": round(quorum - fault, 1) if quorum else None, "outboxDrainedAfter": round(drained - fault, 1) if drained else None,
            "tasks": {o: task_states(o, ids[o]) for o in ids}, "window": load.window(fault),
            "checks": {"outboxDrained": drained is not None, "everyRequestAccepted": done is not None}}


def inventory_written(op_ids: list[str]) -> dict[str, float]:
    """When each operation's intake obligation reached the DR inventory (its object's time)."""
    if not op_ids:
        return {}
    creds = json.loads((STATE / "credentials.json").read_text())["dr"]["inventory"]
    env = {**os.environ, "DR_ID": creds["id"], "DR_SECRET": creds["secret"]}
    script = 'mc alias set dr http://anvilkit-qualification-dr:9000 "$DR_ID" "$DR_SECRET" >/dev/null; for o in "$@"; do mc stat --json "dr/anvilkit-inventory/control/intake/$o"; done'
    p = subprocess.run(["docker", "run", "--rm", "--network", "kind", "-e", "HOME=/tmp", "-e", "MC_CONFIG_DIR=/tmp/.mc", "-e", "DR_ID", "-e", "DR_SECRET",
                        "--entrypoint", "sh", MC, "-c", script, "sh", *op_ids], capture_output=True, text=True, env=env)
    out = {}
    for line in p.stdout.splitlines():
        if line.startswith("{"):
            j = json.loads(line)
            if j.get("status") == "success":
                out[j["name"].rsplit("/", 1)[-1]] = datetime.datetime.fromisoformat(j["lastModified"].replace("Z", "+00:00")).timestamp()
    return out


def drill_storage(load: Load) -> dict:
    fault = now()
    docker("pause", "anvilkit-qualification-dr")
    time.sleep(90)
    resumed = now()
    docker("unpause", "anvilkit-qualification-dr")
    time.sleep(60)
    during = [s for s in load.samples if s["kind"] == "submit" and fault <= s["t"] <= resumed]
    # Inventory first: an operation submitted while the inventory was unavailable may be
    # accepted only once its intake obligation is in the inventory, i.e. after the resume.
    started_during = [a for a in load.accepted if fault <= a["t"] <= resumed]
    written = inventory_written([a["operationId"] for a in started_during])
    answered_during = [a for a in load.accepted if fault <= a["done"] <= resumed]
    return {"pausedAt": fault, "resumedAt": resumed, "submitsStartedWhilePaused": len(during),
            "acceptedSubmissionsStartedWhilePaused": [{"operationId": a["operationId"], "answeredAfterResume": round(a["done"] - resumed, 2),
                                                       "inventoryWrittenAfterResume": round(written[a["operationId"]] - resumed, 2) if a["operationId"] in written else None}
                                                      for a in started_during],
            "acceptancesAnsweredWhilePaused": len(answered_during),
            "afterResume": load.window(resumed), "window": load.window(fault),
            "checks": {"noAcceptanceWhileInventoryUnavailable": not answered_during,
                       "everyAcceptanceHasItsInventoryRecordFirst": all(a["operationId"] in written and written[a["operationId"]] >= resumed - 1 for a in started_during),
                       "admittedSomethingAfterResume": any(a["t"] > resumed for a in load.accepted)}}


def drill_control_plane(load: Load) -> dict:
    cp2, cp3 = f"{CLUSTER}-control-plane2", f"{CLUSTER}-control-plane3"
    fault = now()
    docker("stop", cp3)
    time.sleep(10)
    api_with_quorum = wait_until(lambda: bool(k("get", "--raw", "/readyz", timeout=10)), 60, 2)
    time.sleep(60)
    second = now()
    docker("stop", cp2)
    time.sleep(90)
    try:
        k("get", "--raw", "/readyz", timeout=10)
        api_without_quorum = "answered"
    except (RuntimeError, subprocess.TimeoutExpired):
        api_without_quorum = "unavailable"
    reads = [s for s in load.samples if s["kind"] == "ready" and s["t"] >= second]
    restart = now()
    docker("start", cp2)
    docker("start", cp3)
    api_back = wait_until(lambda: bool(k("get", "--raw", "/readyz", timeout=10)), 300, 5)
    healthy = wait_until(apps_healthy, 900, 15)
    return {"faultAt": fault, "oneNodeDown": {"apiServedAfter": round(api_with_quorum - fault, 1) if api_with_quorum else None},
            "twoNodesDown": {"at": second, "kubernetesApi": api_without_quorum,
                             "businessReadinessWhileApiDown": f"{sum(1 for s in reads if s['ok'])}/{len(reads)} probes ok"},
            "restartedAt": restart, "apiBackAfterRestart": round(api_back - restart, 1) if api_back else None,
            "environmentHealthyAfterRestart": round(healthy - restart, 1) if healthy else None, "window": load.window(fault),
            "checks": {"apiKeptQuorumWithOneNodeDown": api_with_quorum is not None, "apiLostQuorumWithTwoNodesDown": api_without_quorum == "unavailable",
                       "environmentRecovered": healthy is not None}}


def rotate_role(creds: dict, role: str) -> str:
    """P0.6: the role's new password goes to OpenBao first (bootstrap.openbao re-writes
    the changed credential paths under a root token generated from the custody shares,
    revoked after), then to the database over the primary's local socket (ALTER ROLE);
    no Kubernetes Secret is involved. The CSI driver refreshes the services' mounted
    files within its rotation poll interval. Returns the old password."""
    import secrets as _secrets
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import bootstrap
    old = creds["roles"][role]
    creds["roles"][role] = _secrets.token_hex(24)
    (STATE / "credentials.json").write_text(json.dumps(creds, indent=2))
    bootstrap.openbao(creds)
    if not bootstrap.role_passwords(creds):
        raise RuntimeError("the CNPG primaries are not running: the rotated password was not applied")
    return old


def login_works(role: str, password: str, db: str) -> bool:
    primary = json.loads(k("-n", "anvilkit-data", "get", "clusters.postgresql.cnpg.io", "anvilkit-business", "-o", "json"))["status"]["currentPrimary"]
    # The password reaches psql through standard input, never a command line.
    p = subprocess.run([KUBECTL, "--kubeconfig", str(KUBECONFIG), "-n", "anvilkit-data", "exec", "-i", primary, "-c", "postgres", "--", "sh", "-c",
                        f'read -r PGPASSWORD; export PGPASSWORD; exec psql -h 127.0.0.1 -U {role} -d {db} -Atc "select 1"'],
                       input=password + "\n", capture_output=True, text=True)
    return p.returncode == 0 and p.stdout.strip() == "1"


def drill_secret_rotation(load: Load) -> dict:
    creds = json.loads((STATE / "credentials.json").read_text())
    # Control reads its database URL file once at load (a rolling restart takes the
    # rotated file); Knowledge and MCP reload the mounted file as a new generation.
    plan = [("anvilkit_control_app", "anvilkit_control", "anvilkit-agent-control"),
            ("anvilkit_knowledge_app", "anvilkit_knowledge", None),
            ("anvilkit_mcp_app", "anvilkit_mcp", None)]
    fault = now()
    rows = []
    for role, db, restart in plan:
        started = now()
        old = rotate_role(creds, role)
        applied = wait_until(lambda: login_works(role, creds["roles"][role], db) and not login_works(role, old, db), 180, 3)
        if restart:
            # The CSI driver's rotation poll (2 minutes) refreshes the file first.
            time.sleep(150)
            k("-n", "anvilkit-apps", "rollout", "restart", f"deployment/{restart}")
            k("-n", "anvilkit-apps", "rollout", "status", f"deployment/{restart}", "--timeout=300s", timeout=320)
        # Every session of the role was opened after the rotation: the old pool drained.
        drained = wait_until(lambda: psql("anvilkit-business", f"SELECT count(*) FROM pg_stat_activity WHERE usename = '{role}' "
                                                               f"AND backend_start < to_timestamp({started})") == "0", 300, 5)
        # Reconnected: the service opened a session after the rotation (the old password is
        # refused by then, so only the new one can have opened it). An idle pool may hold no
        # session at a given instant, so the drill watches for one for two minutes.
        reconnected = wait_until(lambda: psql("anvilkit-business", f"SELECT count(*) FROM pg_stat_activity WHERE usename = '{role}' "
                                                                   f"AND backend_start > to_timestamp({started})") != "0", 120, 2)
        rows.append({"role": role, "mechanism": "OpenBao, CSI file, rolling restart" if restart else "OpenBao, CSI file, configuration generation",
                     "passwordAppliedAfter": round(applied - started, 1) if applied else None,
                     "oldSessionsGoneAfter": round(drained - started, 1) if drained else None,
                     "oldPasswordRefused": not login_works(role, old, db),
                     "newSessionAfter": round(reconnected - started, 1) if reconnected else None})
    return {"faultAt": fault, "rotations": rows, "window": load.window(fault),
            "checks": {"oldPasswordsRefused": all(r["oldPasswordRefused"] for r in rows),
                       "oldSessionsDrained": all(r["oldSessionsGoneAfter"] is not None for r in rows),
                       "servicesReconnected": all(r["newSessionAfter"] is not None for r in rows),
                       "noProbeFailures": load.window(fault)["failedProbes"] == 0}}


DRILLS = {"node-loss": drill_node_loss, "db-primary": drill_db_primary, "queue-writes": drill_queue_writes,
          "broker": drill_broker, "storage": drill_storage, "control-plane": drill_control_plane,
          "secret-rotation": drill_secret_rotation}


def environment_revision() -> str | None:
    """The environment chart version the root Application targets while the drill ran."""
    try:
        return k("-n", "anvilkit-platform", "get", "applications.argoproj.io", "anvilkit-qualification", "-o", "jsonpath={.spec.source.targetRevision}").strip() or None
    except RuntimeError:
        return None

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("drills", nargs="+", choices=sorted(DRILLS))
    a = ap.parse_args()
    creds = json.loads((STATE / "credentials.json").read_text())
    out = ROOT / "outputs/qualification" / a.run / "drills"
    out.mkdir(parents=True, exist_ok=True)
    port = drill_service()
    failures = 0
    for name in a.drills:
        if not wait_until(apps_healthy, 600, 15):
            print(f"{name}: UNEXECUTED, the environment is not healthy before the fault")
            failures += 1
            continue
        load = Load(creds["principals"]["probe"], port)
        # The drill's NodePort answers only once kube-proxy programmed it on the nodes.
        if not wait_until(lambda: 200 <= load.call("GET", "/readyz", timeout=2.0)[0] < 300, 120, 2):
            print(f"{name}: UNEXECUTED, the API does not answer through the drill NodePort")
            failures += 1
            continue
        load.start()
        time.sleep(20)
        started = now()
        try:
            result = DRILLS[name](load)
        except Exception as e:  # recorded, never hidden
            result = {"error": str(e), "checks": {"completed": False}}
        time.sleep(20)
        load.stop()
        result["durability"] = load.verify()
        result["checks"]["noLostAcceptedOperation"] = not result["durability"]["lost"]
        result["checks"]["noOperationWithSeveralJobs"] = not result["durability"]["operationsWithSeveralJobs"]
        result["checks"]["everyAcceptedOperationFinished"] = not result["durability"]["unfinished"]
        result["operatorUnseals"] = len([u for u in UNSEALS if u >= started])
        result["unhealthyAtLastCheck"] = list(UNHEALTHY)
        result.update({"drill": name, "startedAt": started, "endedAt": now(), "environmentRevision": environment_revision(), "samples": load.samples, "acceptedOperations": load.accepted,
                       "placement": "DEVELOPMENT_ONLY: six kind nodes on one host; zones are labels"})
        ok = all(result["checks"].values())
        failures += not ok
        (out / f"{name}.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"{'PASS' if ok else 'FAIL'} {name}: " + ", ".join(f"{c}={v}" for c, v in result["checks"].items())
              + f"; window {result.get('window')}", flush=True)
    k("-n", "anvilkit-apps", "delete", "service", "anvilkit-drill-api", "--ignore-not-found")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
