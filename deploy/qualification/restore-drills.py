#!/usr/bin/env python3
"""Restore drills of the P23 qualification environment (P23-08; DEVELOPMENT_ONLY placement).

  .local/verification-venv/bin/python deploy/qualification/restore-drills.py --run RUN DRILL [DRILL ...]

Every drill restores from material held in the DR store outside the cluster
(anvilkit-qualification-dr) into something the drill owns, never over live data, and
records the restore's duration and what it proved in outputs/qualification/<RUN>/restores/:

  postgres-business  CloudNativePG point-in-time recovery of anvilkit-business from its
                     Barman Cloud base backup and WAL into the sibling cluster
                     anvilkit-business-pitr: a marker committed before the restore point is
                     present, one committed after it is absent, the domain databases exist
  postgres-temporal  the same for anvilkit-temporal: Temporal's persistence and visibility
                     databases and the anvilkit namespace row at the target
  qdrant             a collection with replication factor 3 is snapshotted, the snapshot is
                     copied to the DR store, the collection is deleted and recovered by
                     uploading the DR copy (checksum-verified); its points and payloads are back
  etcd               an etcd snapshot of the control plane goes to the DR store and is
                     restored into an isolated etcd, which serves the cluster's namespaces
  openbao            (run it with no other unsealer active: bootstrap.py's wait loop unseals too)
                     OpenBao's Shamir shares are held only in the custody location
                     (bootstrap.CUSTODY, supplied out of band; P0.6: never the DR store, and
                     the DR store is checked to hold no init.json), a secret is written under
                     a root token generated from the shares, a Raft snapshot goes to the DR
                     store, every member's storage is destroyed and re-initialized, the
                     snapshot is restored and unsealed with the custody shares; the secret
                     is back (read under a new generated root token, revoked after) and the
                     new keys do not unseal it
  objects            an artifact object is copied to the DR store, lost from the in-cluster
                     store and restored from the DR copy byte for byte
  memory-removals    a memory fact is revoked through the API after a fresh base backup of
                     anvilkit-business (answered only once its record is in the DR store's
                     removal inventory, which its own user can read but not delete); the
                     cluster is recovered to a restore point before the revocation into
                     anvilkit-business-pitr, where the fact is confirmed again, and a
                     Knowledge with no placement but the removal inventory, started on that
                     restored copy, re-applies the revocation under its original command
                     before memory serves; a fact never removed is untouched
MySQL (Apollo) is not deployed in this environment: NOT_RUN.

Every database drill recovers to a named restore point (pg_create_restore_point), a
WAL record between the commits before it and those after it, from the base backup it
took (backupID), never to a clock_timestamp() target: this host's wall clock runs
fast and is stepped back about 1.4 s every half minute (WSL2 time sync), so a time
read just after a commit can precede that commit, and the replay then stops before it.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / ".local/qualification"
KUBECONFIG = STATE / "kubeconfig"
KUBECTL = str(ROOT / ".local/bin/kubectl")
MC = "minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727"
# The same mc for Pods: Docker Hub refuses it to the cluster's anonymous pulls, so the
# nodes take the copy cluster.sh vendors into the environment registry.
MC_IN_CLUSTER = "anvilkit-dev-registry:5000/vendor/minio/mc:a7fe349ef4bd@sha256:bdfae21c72b19fae5a005c56dddba25a873d75fac3dda60f55aea7e417382cbe"
# The etcd release image at the cluster's version (kubeadm's registry.k8s.io/etcd:3.7.0-0 has no etcdutl).
ETCD = "gcr.io/etcd-development/etcd:v3.7.0@sha256:6ecefbe2510c4a30573a62a4d6dd175acf881ca67003fcd91849a16df7a724d5"
CLUSTER = "anvilkit-qualification"


def k(*args: str, stdin: str | None = None, check: bool = True, timeout: int = 120) -> str:
    p = subprocess.run([KUBECTL, "--kubeconfig", str(KUBECONFIG), *args], input=stdin, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:4])}: {p.stderr.strip()[-400:]}")
    return p.stdout


def now() -> float:
    return time.time()


def wait_until(check, timeout: float, step: float = 5.0) -> float | None:
    start = now()
    while now() - start < timeout:
        try:
            if check():
                return now()
        except (RuntimeError, subprocess.TimeoutExpired, KeyError, json.JSONDecodeError):
            pass
        time.sleep(step)
    return None


def dr(script: str, stdin_files: dict[str, bytes] | None = None, user: str = "snapshots") -> str:
    """Runs mc against the DR store as one of its bucket-limited users (credentials never in argv)."""
    creds = json.loads((STATE / "credentials.json").read_text())["dr"][user]
    env = {**os.environ, "DR_ID": creds["id"], "DR_SECRET": creds["secret"]}
    with tempfile.TemporaryDirectory() as tmp:
        for name, data in (stdin_files or {}).items():
            pathlib.Path(tmp, name).write_bytes(data)
        p = subprocess.run(["docker", "run", "--rm", "-i", "--network", "kind", "-v", f"{tmp}:/work", "-w", "/work", "-e", "HOME=/tmp",
                            "-e", "MC_CONFIG_DIR=/tmp/.mc", "-e", "DR_ID", "-e", "DR_SECRET", "--entrypoint", "sh", MC],
                           input='set -eu\nmc alias set dr http://anvilkit-qualification-dr:9000 "$DR_ID" "$DR_SECRET" >/dev/null\n' + script,
                           capture_output=True, text=True, env=env)
        if p.returncode != 0:
            raise RuntimeError(f"DR store: {p.stderr.strip()[-400:]}")
        return p.stdout


def dr_get(key: str) -> bytes:
    """One object of the DR store, as bytes (the snapshots user; credentials never in argv)."""
    creds = json.loads((STATE / "credentials.json").read_text())["dr"]["snapshots"]
    env = {**os.environ, "DR_ID": creds["id"], "DR_SECRET": creds["secret"]}
    p = subprocess.run(["docker", "run", "--rm", "-i", "--network", "kind", "-e", "HOME=/tmp", "-e", "MC_CONFIG_DIR=/tmp/.mc", "-e", "DR_ID", "-e", "DR_SECRET",
                        "--entrypoint", "sh", MC, "-c", 'mc alias set dr http://anvilkit-qualification-dr:9000 "$DR_ID" "$DR_SECRET" >/dev/null && mc cat "dr/$0"', key],
                       capture_output=True, env=env, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(f"DR store: {p.stderr.decode(errors='replace').strip()[-400:]}")
    return p.stdout


def psql(pod: str, sql: str, db: str = "postgres") -> str:
    return k("-n", "anvilkit-data", "exec", pod, "-c", "postgres", "--", "psql", "-At", "-d", db, "-c", sql).strip()


def primary(cluster: str) -> str:
    return json.loads(k("-n", "anvilkit-data", "get", "clusters.postgresql.cnpg.io", cluster, "-o", "json"))["status"]["currentPrimary"]


def base_backup(source: str, tag: str) -> tuple[str, str]:
    """A fresh base backup through the plugin, so the recovery replays a bounded WAL
    range; returns the Backup's name and its backup id."""
    backup = f"{source}-drill-{tag}"
    k("-n", "anvilkit-data", "apply", "-f", "-", stdin=json.dumps({
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Backup", "metadata": {"name": backup},
        "spec": {"cluster": {"name": source}, "method": "plugin", "pluginConfiguration": {"name": "barman-cloud.cloudnative-pg.io"}}}))
    status: dict = {}

    def completed() -> bool:
        nonlocal status
        status = json.loads(k("-n", "anvilkit-data", "get", "backups.postgresql.cnpg.io", backup, "-o", "json")).get("status", {})
        return status.get("phase") == "completed" and bool(status.get("backupId"))

    if not wait_until(completed, 600, 5):
        raise RuntimeError(f"base backup {backup} did not complete")
    return backup, status["backupId"]


def restore_point(pod: str, name: str) -> str:
    """The recovery target: a WAL record after every commit made before it."""
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError(f"restore point name {name!r}")
    psql(pod, f"SELECT pg_create_restore_point('{name}')")
    return name


def recover(source: str, target_cluster: str, point: str, backup_id: str) -> tuple[float, str]:
    """Ships the WAL holding the restore point, then recovers the base backup to it
    into target_cluster (replaced if it exists); returns the seconds to ready and
    where the replay stopped, read from the full-recovery Job Pod while it exists
    (CloudNativePG removes it once the cluster is ready)."""
    pod = primary(source)
    segment = psql(pod, "SELECT pg_walfile_name(pg_switch_wal())")
    if not wait_until(lambda: psql(pod, "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")[:24] >= segment, 300, 2):
        raise RuntimeError(f"WAL segment {segment} of {source} was not archived")
    spec = json.loads(k("-n", "anvilkit-data", "get", "clusters.postgresql.cnpg.io", source, "-o", "json"))["spec"]
    k("-n", "anvilkit-data", "delete", "clusters.postgresql.cnpg.io", target_cluster, "--ignore-not-found", "--wait=true", timeout=300)
    started = now()
    k("-n", "anvilkit-data", "apply", "-f", "-", stdin=json.dumps({
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": target_cluster, "labels": {"anvilkit.io/drill": "restore"}},
        "spec": {"instances": 1, "imageName": spec["imageName"], "storage": spec["storage"], "resources": spec.get("resources", {}),
                 "bootstrap": {"recovery": {"source": "origin", "recoveryTarget": {"targetName": point, "backupID": backup_id}}},
                 "externalClusters": [{"name": "origin", "plugin": {"name": "barman-cloud.cloudnative-pg.io",
                                                                    "parameters": {"barmanObjectName": "dr-store", "serverName": source}}}]}}))
    stop = ""

    def ready_yet() -> bool:
        nonlocal stop
        stop = stop or recovery_stop(target_cluster)
        return json.loads(k("-n", "anvilkit-data", "get", "clusters.postgresql.cnpg.io", target_cluster, "-o", "json")).get("status", {}).get("readyInstances") == 1

    ready = wait_until(ready_yet, 1200, 5)
    if not ready:
        raise RuntimeError(f"{target_cluster} did not become ready")
    return round(ready - started, 1), stop or recovery_stop(target_cluster)


def recovery_stop(cluster: str) -> str:
    """Where the replay stopped, in the restored server's own words ('' until known)."""
    pods = json.loads(k("-n", "anvilkit-data", "get", "pods", "-l", f"cnpg.io/cluster={cluster}", "-o", "json"))["items"]
    for pod in pods:
        logs = k("-n", "anvilkit-data", "logs", pod["metadata"]["name"], "--all-containers", check=False)
        for line in logs.splitlines():
            if "recovery stopping" in line:
                try:
                    return str(json.loads(line).get("record", {}).get("message", line))[:200]
                except ValueError:
                    return line[:200]
    return ""


def drill_postgres(source: str, dbs: list[str]) -> dict:
    target_cluster = f"{source}-pitr"
    pod = primary(source)
    psql(pod, "CREATE TABLE IF NOT EXISTS anvilkit_drill_markers (marker text PRIMARY KEY, at timestamptz NOT NULL DEFAULT clock_timestamp())")
    tag = secrets.token_hex(4)
    psql(pod, f"INSERT INTO anvilkit_drill_markers (marker) VALUES ('before-{tag}')")
    backup, backup_id = base_backup(source, tag)
    # The target marker, the restore point and the after marker follow each other
    # at once: the restore point alone separates them, whatever the clock does.
    psql(pod, f"INSERT INTO anvilkit_drill_markers (marker) VALUES ('target-{tag}')")
    point = restore_point(pod, f"drill-{tag}")
    psql(pod, f"INSERT INTO anvilkit_drill_markers (marker) VALUES ('after-{tag}')")
    restore_seconds, stop = recover(source, target_cluster, point, backup_id)
    rp = primary(target_cluster)
    markers = set(psql(rp, "SELECT marker FROM anvilkit_drill_markers").split())
    present = {db: psql(rp, f"SELECT count(*) FROM pg_database WHERE datname = '{db}'") == "1" for db in dbs}
    result = {"source": source, "backup": backup, "backupId": backup_id, "restorePoint": point, "recoveryStop": stop,
              "restoreSeconds": restore_seconds,
              "markers": {"beforeBackup": f"before-{tag}" in markers, "atTarget": f"target-{tag}" in markers, "afterTarget": f"after-{tag}" in markers},
              "databases": present}
    k("-n", "anvilkit-data", "delete", "clusters.postgresql.cnpg.io", target_cluster, "--wait=false")
    result["checks"] = {"beforeTargetRestored": result["markers"]["beforeBackup"] and result["markers"]["atTarget"],
                        "afterTargetAbsent": not result["markers"]["afterTarget"], "databasesRestored": all(present.values()),
                        "replayStoppedAtTheRestorePoint": f'restore point "{point}"' in stop}
    return result


def drill_postgres_business() -> dict:
    return drill_postgres("anvilkit-business", ["anvilkit_control", "anvilkit_knowledge", "anvilkit_mcp"])


def drill_postgres_temporal() -> dict:
    r = drill_postgres("anvilkit-temporal", ["temporal", "temporal_visibility"])
    return r


QDRANT_LOCAL_PORT = 0  # chosen by kubectl port-forward (a fixed port may already serve another Qdrant)


class qdrant_forward:
    """A port-forward to one Qdrant peer for the drill's requests: the image has no
    HTTP client, and the API key then travels only in a request header from this host."""

    def __init__(self, pod: str = "anvilkit-qdrant-0") -> None:
        self.pod, self.proc = pod, None

    def __enter__(self) -> "qdrant_forward":
        global QDRANT_LOCAL_PORT
        self.proc = subprocess.Popen([KUBECTL, "--kubeconfig", str(KUBECONFIG), "-n", "anvilkit-data", "port-forward", f"pod/{self.pod}", ":6333"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        line = self.proc.stdout.readline()  # "Forwarding from 127.0.0.1:<port> -> 6333"
        m = re.search(r"127\.0\.0\.1:(\d+)", line)
        if not m:
            self.proc.terminate()
            raise RuntimeError(f"no port-forward to {self.pod}: {line.strip()}")
        QDRANT_LOCAL_PORT = int(m[1])
        return self

    def __exit__(self, *exc) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)


def qdrant_http(method: str, path: str, data: bytes | None = None, content_type: str = "application/json", timeout: int = 300) -> bytes:
    key = json.loads((STATE / "credentials.json").read_text())["qdrant"]
    req = urllib.request.Request(f"http://127.0.0.1:{QDRANT_LOCAL_PORT}{path}", data=data, method=method,
                                 headers={"api-key": key, "content-type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:  # Qdrant's error body (e.g. 404 for a missing collection)
        return e.read()


def qdrant(method: str, path: str, body: dict | None = None) -> dict:
    return json.loads(qdrant_http(method, path, json.dumps(body).encode() if body is not None else None) or b"{}")


def drill_qdrant() -> dict:
    with qdrant_forward():
        return _drill_qdrant()


def _drill_qdrant() -> dict:
    name = f"drill_{secrets.token_hex(4)}"
    qdrant("PUT", f"/collections/{name}", {"vectors": {"size": 4, "distance": "Cosine"}, "replication_factor": 3, "write_consistency_factor": 2})
    points = [{"id": i, "vector": [i, 1, 0, 1], "payload": {"n": i}} for i in range(1, 101)]
    qdrant("PUT", f"/collections/{name}/points?wait=true&ordering=strong", {"points": points})
    snap = qdrant("POST", f"/collections/{name}/snapshots?wait=true")["result"]["name"]
    raw = qdrant_http("GET", f"/collections/{name}/snapshots/{snap}")
    digest = hashlib.sha256(raw).hexdigest()
    dr(f"mc cp /work/{snap} dr/anvilkit-snapshots/qdrant/{snap} >/dev/null\n", {snap: raw})
    qdrant("DELETE", f"/collections/{name}")
    gone = qdrant("GET", f"/collections/{name}").get("status") != "ok"
    started = now()
    # The DR copy travels back through this host into the snapshot upload endpoint:
    # no presigned URL is handed to Qdrant (it would reach Qdrant's logs).
    copy = dr_get(f"anvilkit-snapshots/qdrant/{snap}")
    if hashlib.sha256(copy).hexdigest() != digest:
        raise RuntimeError("the DR copy of the snapshot differs from the snapshot taken")
    boundary = secrets.token_hex(16)
    form = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"snapshot\"; filename=\"{snap}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + copy + f"\r\n--{boundary}--\r\n".encode()
    answer = json.loads(qdrant_http("POST", f"/collections/{name}/snapshots/upload?wait=true&priority=snapshot&checksum={digest}", form,
                                    f"multipart/form-data; boundary={boundary}", timeout=600) or b"{}")
    if answer.get("status") != "ok":
        raise RuntimeError(f"snapshot upload: {json.dumps(answer)[:300]}")
    # Only the peer that received the upload holds the recovered data at first; with
    # priority=snapshot the other replicas take it from that peer. Restored means every
    # replica Active and no transfer in flight, then read with every replica consulted.
    def replicas_active() -> bool:
        c = qdrant("GET", f"/collections/{name}/cluster")["result"]
        states = [x["state"] for x in c.get("local_shards", []) + c.get("remote_shards", [])]
        return bool(states) and all(x == "Active" for x in states) and not c.get("shard_transfers")
    active = wait_until(replicas_active, 300, 2)
    count = qdrant("POST", f"/collections/{name}/points/count?consistency=all", {"exact": True})["result"]["count"]
    sample = qdrant("POST", f"/collections/{name}/points?consistency=all", {"ids": [42], "with_payload": True})["result"]
    restored = now()
    qdrant("DELETE", f"/collections/{name}")
    return {"collection": name, "snapshot": snap, "snapshotSha256": digest, "collectionGoneBeforeRecover": gone,
            "restoreSeconds": round(restored - started, 1), "pointsAfter": count, "payload42": sample[0]["payload"] if sample else None,
            "note": "recovered on the peer that received the upload; Qdrant replicates the recovered shard to the collection's other replicas",
            "replicasActiveAfter": round(active - started, 1) if active else None,
            "checks": {"collectionGoneBeforeRecover": gone, "replicasActive": active is not None, "allPointsBack": count == 100,
                       "payloadBack": bool(sample) and sample[0]["payload"] == {"n": 42}}}


def drill_etcd() -> dict:
    node = f"{CLUSTER}-control-plane"
    certs = "--cacert /etc/kubernetes/pki/etcd/ca.crt --cert /etc/kubernetes/pki/etcd/server.crt --key /etc/kubernetes/pki/etcd/server.key"
    # The etcd image has no shell: etcdctl runs directly.
    k("-n", "kube-system", "exec", f"etcd-{node}", "--", "etcdctl", *certs.split(), "snapshot", "save", "/var/lib/etcd/drill.db", timeout=300)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["docker", "cp", f"{node}:/var/lib/etcd/drill.db", f"{tmp}/etcd.db"], check=True, capture_output=True)
        subprocess.run(["docker", "exec", node, "rm", "-f", "/var/lib/etcd/drill.db"], capture_output=True)
        data = pathlib.Path(tmp, "etcd.db").read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        dr("mc cp /work/etcd.db dr/anvilkit-snapshots/etcd/etcd-$(date +%s).db >/dev/null\n", {"etcd.db": data})
        started = now()
        os.chmod(tmp, 0o777)
        name = f"anvilkit-etcd-restore-{secrets.token_hex(3)}"
        restore = subprocess.run(["docker", "run", "--rm", "-v", f"{tmp}:/w", ETCD, "etcdutl", "snapshot", "restore", "/w/etcd.db", "--data-dir", "/w/data"],
                                 capture_output=True, text=True)
        if restore.returncode != 0:
            raise RuntimeError(restore.stderr[-300:])
        subprocess.run(["docker", "run", "-d", "--name", name, "-v", f"{tmp}:/w", ETCD, "etcd", "--data-dir", "/w/data",
                        "--listen-client-urls", "http://127.0.0.1:2379", "--advertise-client-urls", "http://127.0.0.1:2379"], check=True, capture_output=True)
        try:
            served = wait_until(lambda: "anvilkit-apps" in subprocess.run(["docker", "exec", name, "etcdctl", "get", "/registry/namespaces/", "--prefix", "--keys-only"],
                                                                        capture_output=True, text=True).stdout, 60, 2)
            keys = subprocess.run(["docker", "exec", name, "etcdctl", "get", "/registry/", "--prefix", "--keys-only"], capture_output=True, text=True).stdout.split()
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    return {"snapshotSha256": digest, "snapshotBytes": len(data), "restoreSeconds": round((served or now()) - started, 1), "keys": len(keys),
            "scope": "the snapshot restored into an isolated etcd; replacing the running control plane's members is NOT_RUN (it would end the environment)",
            "checks": {"isolatedEtcdServesNamespaces": served is not None}}


def bao(pod: str, *args: str, stdin: str | None = None, token: str | None = None, check: bool = True) -> str:
    if token:  # the token is the first line of standard input, never a command-line value
        return k("-n", "anvilkit-platform", "exec", "-i", pod, "--", "sh", "-c",
                 'read -r BAO_TOKEN; export BAO_TOKEN BAO_ADDR=https://127.0.0.1:8200; exec bao "$@"', "bao", *args,
                 stdin=token + "\n" + (stdin or ""), check=check, timeout=120)
    return k("-n", "anvilkit-platform", "exec", "-i", pod, "--", "env", "BAO_ADDR=https://127.0.0.1:8200", "bao", *args, stdin=stdin, check=check, timeout=120)


def unseal(pod: str, keys: list[str]) -> bool:
    for key in keys:
        bao(pod, "write", "-format=json", "sys/unseal", "key=-", stdin=key, check=False)  # keeps the share off every command line
    status = json.loads(bao(pod, "status", "-format=json", check=False) or "{}")
    return status.get("sealed") is False


class argo_paused:
    """Argo CD leaves one Application alone while the drill takes its workload apart
    (argocd.argoproj.io/skip-reconcile): self-heal would otherwise scale the members back
    mid-restore. The root Application does not manage the annotation, so it stays until
    the drill removes it."""

    def __init__(self, app: str) -> None:
        self.app = app

    def __enter__(self) -> "argo_paused":
        k("-n", "anvilkit-platform", "annotate", "applications.argoproj.io", self.app, "argocd.argoproj.io/skip-reconcile=true", "--overwrite")
        return self

    def __exit__(self, *exc) -> None:
        k("-n", "anvilkit-platform", "annotate", "applications.argoproj.io", self.app, "argocd.argoproj.io/skip-reconcile-", check=False)


def drill_openbao() -> dict:
    with argo_paused("openbao"):
        return _drill_openbao()


def _drill_openbao() -> dict:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import bootstrap
    init_file = bootstrap.CUSTODY / "init.json"
    status = json.loads(bao("openbao-0", "status", "-format=json", check=False) or "{}")
    if not status.get("initialized") or not init_file.exists():
        raise RuntimeError("OpenBao is not initialized with custody material (bootstrap.py first)")
    keys = json.loads(init_file.read_text())["unseal_keys_b64"][:3]
    # P0.6 custody: the DR store must hold no seal material (an earlier run's copy is a
    # finding, removed here and reported).
    dr_listing = dr("mc ls --recursive dr/anvilkit-snapshots/openbao/ 2>/dev/null || true\n", {})
    dr_held_init = "init.json" in dr_listing
    if dr_held_init:
        dr("mc rm dr/anvilkit-snapshots/openbao/init.json >/dev/null\n", {})
    for pod in ("openbao-0", "openbao-1", "openbao-2"):
        wait_until(lambda pod=pod: unseal(pod, keys), 300, 5)
    root = bootstrap.generate_root("openbao-0")
    try:
        bao("openbao-0", "secrets", "enable", "-path=kv", "kv-v2", token=root, check=False)
        marker = secrets.token_hex(8)
        bao("openbao-0", "kv", "put", "kv/anvilkit/drill", f"marker={marker}", token=root)
        bao("openbao-0", "operator", "raft", "snapshot", "save", "/tmp/raft.snap", token=root)
    finally:
        bao("openbao-0", "token", "revoke", "-self", token=root, check=False)
    snap = subprocess.run([KUBECTL, "--kubeconfig", str(KUBECONFIG), "-n", "anvilkit-platform", "exec", "openbao-0", "--", "cat", "/tmp/raft.snap"],
                          capture_output=True, timeout=120).stdout
    digest = hashlib.sha256(snap).hexdigest()
    dr("mc cp /work/raft.snap dr/anvilkit-snapshots/openbao/raft.snap >/dev/null\n", {"raft.snap": snap})
    # Every member's storage is lost: a new, empty cluster under new keys.
    started = now()
    k("-n", "anvilkit-platform", "scale", "statefulset", "openbao", "--replicas=0")
    wait_until(lambda: not json.loads(k("-n", "anvilkit-platform", "get", "pods", "-l", "app.kubernetes.io/name=openbao,component=server", "-o", "json"))["items"], 300, 5)
    k("-n", "anvilkit-platform", "delete", "pvc", "-l", "app.kubernetes.io/name=openbao", "--wait=true", timeout=300)
    k("-n", "anvilkit-platform", "scale", "statefulset", "openbao", "--replicas=1")
    wait_until(lambda: json.loads(bao("openbao-0", "status", "-format=json", check=False) or "{}").get("initialized") is False, 300, 5)
    fresh = json.loads(bao("openbao-0", "operator", "init", "-key-shares=1", "-key-threshold=1", "-format=json"))
    if not unseal("openbao-0", fresh["unseal_keys_b64"]):
        raise RuntimeError("the fresh cluster did not unseal with its own key (another unsealer active?)")
    # bao 2.6 reports the active member as is_self (there is no ha_mode field).
    wait_until(lambda: json.loads(bao("openbao-0", "status", "-format=json", check=False) or "{}").get("is_self") is True, 120, 3)
    copied = subprocess.run([KUBECTL, "--kubeconfig", str(KUBECONFIG), "-n", "anvilkit-platform", "exec", "-i", "openbao-0", "--", "sh", "-c", "cat > /tmp/raft.snap"],
                            input=snap, capture_output=True, timeout=120)
    if copied.returncode != 0:
        raise RuntimeError(copied.stderr.decode()[-300:])
    bao("openbao-0", "operator", "raft", "snapshot", "restore", "-force", "/tmp/raft.snap", token=fresh["root_token"])
    time.sleep(10)
    # OpenBao 2.6 seals after a forced restore but keeps the fresh cluster's seal
    # configuration in memory (an original share is refused as an invalid key size):
    # the restored barrier's configuration takes effect after the server restarts.
    k("-n", "anvilkit-platform", "delete", "pod", "openbao-0", "--wait=true", timeout=300)
    wait_until(lambda: json.loads(bao("openbao-0", "status", "-format=json", check=False) or "{}").get("initialized") is True, 300, 5)
    seal_config = {key: json.loads(bao("openbao-0", "status", "-format=json", check=False) or "{}").get(key) for key in ("t", "n")}
    # The restored barrier is the original cluster's: the new key must not open
    # it, the custody shares must.
    sealed_after_restore = json.loads(bao("openbao-0", "status", "-format=json", check=False) or "{}").get("sealed")
    new_key_opens = unseal("openbao-0", fresh["unseal_keys_b64"]) if sealed_after_restore else None
    original_opens = wait_until(lambda: unseal("openbao-0", keys), 120, 5) is not None
    after = bootstrap.generate_root("openbao-0")
    try:
        value = json.loads(bao("openbao-0", "kv", "get", "-format=json", "kv/anvilkit/drill", token=after, check=False) or "{}").get("data", {}).get("data", {}).get("marker")
    finally:
        bao("openbao-0", "token", "revoke", "-self", token=after, check=False)
    restored = now()
    k("-n", "anvilkit-platform", "scale", "statefulset", "openbao", "--replicas=3")
    for pod in ("openbao-1", "openbao-2"):
        wait_until(lambda pod=pod: unseal(pod, keys), 600, 10)
    return {"snapshotSha256": digest, "restoreSeconds": round(restored - started, 1), "sealedAfterRestore": sealed_after_restore,
            "sealConfigAfterRestart": seal_config,
            "newKeyOpenedRestoredBarrier": new_key_opens, "originalSharesOpened": original_opens, "markerRestored": value == marker,
            "drHeldInitJson": dr_held_init,
            "custody": f"DEVELOPMENT_ONLY: Shamir 3-of-5 shares only in {bootstrap.CUSTODY} (out of band; ENV-10 custody NOT_VERIFIED); no root token persists",
            "checks": {"secretRestored": value == marker, "originalSealMaterialRequired": original_opens and not new_key_opens,
                       "drStoreHoldsNoSealMaterial": not dr_held_init}}


def drill_objects() -> dict:
    """An artifact object: copied to the DR store, lost from the in-cluster store,
    restored from the DR copy and compared byte for byte (sha256)."""
    creds = json.loads((STATE / "credentials.json").read_text())
    body = secrets.token_bytes(4096)
    digest = hashlib.sha256(body).hexdigest()
    name = f"drill/{secrets.token_hex(6)}"
    pod = f"objects-drill-{secrets.token_hex(3)}"
    # The DR credentials reach the Pod through a Secret that lives for the drill only,
    # never as a value in the Pod specification.
    k("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": pod, "namespace": "anvilkit-data",
                                                                                            "labels": {"anvilkit.io/drill": "restore"}},
                                           "stringData": {"DR_ID": creds["dr"]["snapshots"]["id"], "DR_SECRET": creds["dr"]["snapshots"]["secret"]}}))
    script = "; ".join([
        "set -eu", "echo \"$BODY\" | base64 -d > /tmp/object",
        'mc alias set store http://anvilkit-objects.anvilkit-data.svc.cluster.local:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null',
        'mc alias set dr http://anvilkit-qualification-dr:9000 "$DR_ID" "$DR_SECRET" >/dev/null',
        f"mc cp /tmp/object store/anvilkit-artifacts/{name} >/dev/null",
        f"mc cp store/anvilkit-artifacts/{name} dr/anvilkit-snapshots/objects/{name} >/dev/null",
        f"mc rm --versions --force store/anvilkit-artifacts/{name} >/dev/null",
        f"if mc stat store/anvilkit-artifacts/{name} >/dev/null 2>&1; then echo still-there; else echo lost; fi",
        "date +%s.%N",
        f"mc cp dr/anvilkit-snapshots/objects/{name} store/anvilkit-artifacts/{name} >/dev/null",
        f"mc cat store/anvilkit-artifacts/{name} | sha256sum | cut -d' ' -f1",
        "date +%s.%N",
        f"mc rm --versions --force store/anvilkit-artifacts/{name} >/dev/null",
    ])
    manifest = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": pod, "namespace": "anvilkit-data", "labels": {"anvilkit.io/drill": "restore"}},
                "spec": {"restartPolicy": "Never", "containers": [{"name": "mc", "image": MC_IN_CLUSTER, "command": ["sh", "-c", script],
                         "envFrom": [{"secretRef": {"name": "anvilkit-objects-root"}}, {"secretRef": {"name": pod}}],
                         "env": [{"name": "HOME", "value": "/tmp"}, {"name": "MC_CONFIG_DIR", "value": "/tmp/.mc"}, {"name": "BODY", "value": base64.b64encode(body).decode()}]}]}}
    try:
        k("apply", "-f", "-", stdin=json.dumps(manifest))
        done = wait_until(lambda: k("-n", "anvilkit-data", "get", "pod", pod, "-o", "jsonpath={.status.phase}") in ("Succeeded", "Failed"), 300, 3)
        phase = k("-n", "anvilkit-data", "get", "pod", pod, "-o", "jsonpath={.status.phase}")
        lines = k("-n", "anvilkit-data", "logs", pod).split()
    finally:
        k("-n", "anvilkit-data", "delete", "pod", pod, "--wait=false", check=False)
        k("-n", "anvilkit-data", "delete", "secret", pod, "--ignore-not-found", check=False)
    lost = "lost" in lines
    restored_digest = next((x for x in lines if len(x) == 64 and all(c in "0123456789abcdef" for c in x)), None)
    times = [float(x) for x in lines if x.replace(".", "", 1).isdigit() and "." in x]
    return {"object": name, "sha256": digest, "podPhase": phase, "lostBeforeRestore": lost,
            "restoreSeconds": round(times[1] - times[0], 2) if len(times) == 2 else None,
            "checks": {"completed": done is not None and phase == "Succeeded", "lostBeforeRestore": lost, "restoredByteForByte": restored_digest == digest}}



class port_forward:
    """A port-forward to one Service for the drill's requests (local port chosen by kubectl)."""

    def __init__(self, ns: str, target: str, remote: int) -> None:
        self.ns, self.target, self.remote, self.proc, self.port = ns, target, remote, None, 0

    def __enter__(self) -> "port_forward":
        self.proc = subprocess.Popen([KUBECTL, "--kubeconfig", str(KUBECONFIG), "-n", self.ns, "port-forward", self.target, f":{self.remote}"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        m = re.search(r"127\.0\.0\.1:(\d+)", self.proc.stdout.readline())
        if not m:
            self.proc.terminate()
            raise RuntimeError(f"no port-forward to {self.target}")
        self.port = int(m[1])
        return self

    def __exit__(self, *exc) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)


def drill_memory_removals() -> dict:
    c = json.loads((STATE / "credentials.json").read_text())
    token = c["principals"]["tenantA"]  # tenant_a / project_a / user_a
    tag = secrets.token_hex(4)
    source, target_cluster = "anvilkit-business", "anvilkit-business-pitr"
    image = k("-n", "anvilkit-apps", "get", "deployment", "anvilkit-agent-knowledge", "-o",
              "jsonpath={.spec.template.spec.containers[?(@.name=='knowledge')].image}").strip()
    kdb = lambda pod, q: psql(pod, q, db="anvilkit_knowledge")  # noqa: E731
    result: dict = {"tag": tag, "image": image}
    with port_forward("anvilkit-apps", "svc/anvilkit-agent-api", 80) as fw:
        def call(method: str, path: str, body: dict) -> tuple[int, dict]:
            req = urllib.request.Request(f"http://127.0.0.1:{fw.port}/api/v1{path}", data=json.dumps(body).encode(), method=method,
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                return e.code, {"error": e.read().decode(errors="replace")[:200]}

        seq = iter(range(1, 100))

        def confirmed(content: str) -> str:
            n = next(seq)
            code, f = call("POST", "/memories", {"commandId": f"drill-mem-{tag}-p{n}", "subjectType": "actor", "subjectId": "user_a", "content": content})
            if code != 201:
                raise RuntimeError(f"propose answered {code}: {f}")
            code, d = call("POST", f"/memories/{f['factId']}/decisions", {"commandId": f"drill-mem-{tag}-c{n}", "expectedRevision": "1", "decision": "confirm"})
            if code != 200 or d.get("state") != "confirmed":
                raise RuntimeError(f"confirm answered {code}: {d}")
            return f["factId"]

        revoked = confirmed(f"drill {tag}: user_a prefers tabs in reviews")
        kept = confirmed(f"drill {tag}: user_a prefers the green build badge")
        pod = primary(source)
        backup, backup_id = base_backup(source, tag)
        point = restore_point(pod, f"drill-mem-{tag}")
        revoke_cmd = f"drill-mem-{tag}-revoke"
        code, d = call("POST", f"/memories/{revoked}/decisions", {"commandId": revoke_cmd, "expectedRevision": "2", "decision": "revoke", "reasonCode": "OUTDATED"})
        result.update({"facts": {"revoked": revoked, "kept": kept}, "backup": backup, "backupId": backup_id, "restorePoint": point, "revokeAnswer": code})
    row = kdb(pod, f"SELECT state || '|' || inventory_key FROM memory_removals WHERE tenant_id = 'tenant_a' AND command_id = '{revoke_cmd}'")
    state, _, key = row.partition("|")
    # The record, read as the inventory's own user, which may not delete it.
    raw = dr(f'mc cat "dr/anvilkit-memory-removals/{key}"', user="removals") if key else ""
    record = json.loads(raw) if raw else {}
    refused = dr(f'mc rm "dr/anvilkit-memory-removals/{key}" >/dev/null 2>&1 && echo DELETED || echo REFUSED', user="removals").strip() if key else ""
    result.update({"removalRow": state, "record": {k_: record.get(k_) for k_ in ("decision", "factId", "commandId", "decider", "confirmer", "reasonCode", "recordedAt")},
                   "recordDeleteByItsUser": refused})
    result["restoreSeconds"], result["recoveryStop"] = recover(source, target_cluster, point, backup_id)
    rp = primary(target_cluster)
    result["restored"] = {"revokedFactState": kdb(rp, f"SELECT state FROM memory_facts WHERE fact_id = '{revoked}'"),
                          "removalRows": kdb(rp, f"SELECT count(*) FROM memory_removals WHERE command_id = '{revoke_cmd}'")}
    # A Knowledge with no placement but the removal inventory, on the restored copy.
    name = f"anvilkit-drill-knowledge-{tag}"
    url = (f"postgres://anvilkit_knowledge_app:{c['roles']['anvilkit_knowledge_app']}@{target_cluster}-rw.anvilkit-data.svc.cluster.local:5432"
           "/anvilkit_knowledge?sslmode=no-verify")
    k("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                                            "metadata": {"name": name, "namespace": "anvilkit-apps", "labels": {"anvilkit.io/drill": "restore"}},
                                            "stringData": {"url": url}}))
    security = {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532, "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}
    k("apply", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": "anvilkit-apps", "labels": {"app.kubernetes.io/name": "anvilkit-drill-knowledge", "anvilkit.io/drill": "restore"}},
        "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                 "containers": [{"name": "knowledge", "image": image, "securityContext": security,
                                 "env": [{"name": "ANVILKIT_KNOWLEDGE_DATABASE_URL_FILE", "value": "/var/run/secrets/anvilkit/database/url"},
                                         {"name": "ANVILKIT_KNOWLEDGE_REMOVALS_ENDPOINT", "value": "http://anvilkit-qualification-dr:9000"},
                                         {"name": "ANVILKIT_KNOWLEDGE_REMOVALS_CREDENTIALS_FILE", "value": "/var/run/secrets/anvilkit/removals/credentials"}],
                                 "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
                                 "volumeMounts": [{"name": "database", "mountPath": "/var/run/secrets/anvilkit/database", "readOnly": True},
                                                  {"name": "removals", "mountPath": "/var/run/secrets/anvilkit/removals", "readOnly": True}]}],
                 "volumes": [{"name": "database", "secret": {"secretName": name}},
                             {"name": "removals", "secret": {"secretName": "anvilkit-agent-knowledge-removals"}}]}}))
    logs = ""
    try:
        def served() -> bool:
            nonlocal logs
            logs = k("-n", "anvilkit-apps", "logs", name, check=False)
            return "removal inventory reconciled: memory serves" in logs or "not reconciled" in logs
        wait_until(served, 300, 5)
        result["restoredKnowledge"] = {
            "log": [json.loads(x).get("msg") for x in logs.splitlines() if x.startswith("{") and "removal" in x][:6],
            "revokedFactState": kdb(rp, f"SELECT state FROM memory_facts WHERE fact_id = '{revoked}'"),
            "removalRow": kdb(rp, f"SELECT state FROM memory_removals WHERE command_id = '{revoke_cmd}'"),
            "decision": kdb(rp, f"SELECT decision || '|' || decider FROM memory_decisions WHERE tenant_id = 'tenant_a' AND command_id = '{revoke_cmd}'"),
            "keptFactState": kdb(rp, f"SELECT state FROM memory_facts WHERE fact_id = '{kept}'")}
    finally:
        k("-n", "anvilkit-apps", "delete", "pod", name, "--ignore-not-found", "--wait=false", check=False)
        k("-n", "anvilkit-apps", "delete", "secret", name, "--ignore-not-found", check=False)
        k("-n", "anvilkit-data", "delete", "clusters.postgresql.cnpg.io", target_cluster, "--wait=false", check=False)
    rk = result.get("restoredKnowledge", {})
    rec = result["record"]
    result["checks"] = {
        "revocationAnsweredOnceRecorded": result["revokeAnswer"] == 200 and result["removalRow"] == "recorded",
        "recordInDrStoreWithoutContent": rec.get("decision") == "revoke" and rec.get("factId") == revoked and rec.get("commandId") == revoke_cmd
                                         and "prefers" not in raw,
        "recordNotDeletableByItsUser": result["recordDeleteByItsUser"] == "REFUSED",
        "restoreRevivedTheRevokedFact": result["restored"]["revokedFactState"] == "confirmed" and result["restored"]["removalRows"] == "0",
        "memoryServedOnlyAfterReconciliation": any("reconciled: memory serves" in (m or "") for m in rk.get("log", [])),
        "revocationReappliedUnderItsCommand": rk.get("revokedFactState") == "revoked" and rk.get("removalRow") == "restored" and rk.get("decision") == "revoke|user_a",
        "factNeverRemovedUntouched": rk.get("keptFactState") == "confirmed",
        "replayStoppedAtTheRestorePoint": f'restore point "{result["restorePoint"]}"' in result.get("recoveryStop", ""),
    }
    return result


def environment_revision() -> str | None:
    """The environment chart version the root Application targets while the drill ran."""
    try:
        return k("-n", "anvilkit-platform", "get", "applications.argoproj.io", "anvilkit-qualification", "-o", "jsonpath={.spec.source.targetRevision}").strip() or None
    except RuntimeError:
        return None

DRILLS = {"memory-removals": drill_memory_removals, "postgres-business": drill_postgres_business, "postgres-temporal": drill_postgres_temporal, "qdrant": drill_qdrant,
          "etcd": drill_etcd, "openbao": drill_openbao, "objects": drill_objects}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("drills", nargs="+", choices=sorted(DRILLS))
    a = ap.parse_args()
    out = ROOT / "outputs/qualification" / a.run / "restores"
    out.mkdir(parents=True, exist_ok=True)
    failures = 0
    for name in a.drills:
        started = now()
        try:
            result = DRILLS[name]()
        except Exception as e:  # recorded, never hidden
            result = {"error": str(e), "checks": {"completed": False}}
        result.update({"drill": name, "startedAt": started, "endedAt": now(), "environmentRevision": environment_revision(), "placement": "DEVELOPMENT_ONLY: the DR store shares the host"})
        ok = all(result["checks"].values())
        failures += not ok
        (out / f"{name}.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"{'PASS' if ok else 'FAIL'} {name}: " + ", ".join(f"{c}={v}" for c, v in result["checks"].items()), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
