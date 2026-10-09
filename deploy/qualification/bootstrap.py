#!/usr/bin/env python3
"""Bootstrap the P23 qualification environment onto its cluster (DEVELOPMENT_ONLY).

  .local/verification-venv/bin/python deploy/qualification/bootstrap.py --run RUN [--wait 1800]

After deploy/qualification/cluster.sh up, tools/release-artifacts.py --run RUN and
deploy/gitops/publish.py --environment qualification --run RUN:
  1. credentials: .local/qualification/credentials.json (0600), generated once; every
     database role, object-store user, queue password, NATS NKey and DEVELOPMENT_ONLY
     bearer principal of the environment. Never printed, never in Git or argv: they
     reach OpenBao and the database through kubectl's standard input.
  2. the components node-pool label on the workers and the namespaces. P0.6: no
     Kubernetes Secret with credential material: the earlier runs' bootstrap Secrets
     are deleted, the workloads read their credentials from OpenBao through the CSI
     driver (the service charts' SecretProviderClasses, anvilkit-secrets for the
     third-party charts that read only Secrets).
  3. the DR store outside the cluster: buckets for base backups/WAL, Control's
     obligation inventory, Knowledge's memory removal inventory (its user lists,
     reads and creates, never deletes) and the restore drills' snapshots, one user
     and one bucket-limited policy each.
  4. Argo CD 3.5.3 (its chart mirrored into the registry first) in anvilkit-platform,
     with the Application health check that makes app-of-apps waves wait, and the
     environment's OCI repositories.
  5. the root Application (outputs/qualification/<RUN>/gitops/root-application.yaml),
     then waits until every child Application is Synced and Healthy or --wait expires,
     initializing OpenBao once and unsealing each member as it starts, and then
     (P0.6) configuring it: KV v2, Kubernetes auth, one read-only policy and role per
     workload, the credentials of every workload path; the CNPG role passwords (over
     the primary's local socket, no Secret), the NATS NKey users (anvilkit-nats-auth,
     public keys only) and the CA bundle the clients verify the data plane against
     (anvilkit-trust, public). Seal custody (ENV-10, DEVELOPMENT_ONLY placement): the
     Shamir 3-of-5 shares stay in the custody directory (.local/qualification/openbao,
     or ANVILKIT_OPENBAO_CUSTODY) and never reach the DR store; the initial root token
     is revoked after the first configuration, and a later configuration (a new
     credential, a rotation) uses a root token generated from the shares
     (generate-root) and revokes it again.
Exit 0 when the environment converged, 1 otherwise, 2 when an input is missing.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tools"))
import pyenv_check  # noqa: E402

pyenv_check.require("yaml")
import yaml  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / ".local/qualification"
KUBECONFIG = STATE / "kubeconfig"
KUBECTL = str(ROOT / ".local/bin/kubectl")
HELM = str(ROOT / ".local/bin/helm")
MC = "minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727"
NATS_BOX = "natsio/nats-box:0.19.0@sha256:00f10dd4156f28ece84bb3a52cf356ef220ebaa0494310caa90dec184d9e2b89"
REGISTRY = "anvilkit-dev-registry:5000"
ARGOCD_CHART = ("https://argoproj.github.io/argo-helm", "argo-cd", "10.9.6")  # Argo CD 3.5.3
BUSINESS = "anvilkit-business-rw.anvilkit-data.svc.cluster.local:5432"
QUEUE = "anvilkit-valkey-queue-master.anvilkit-data.svc.cluster.local:6379"
# P0.6: the CA bundle every client verifies the data plane against (the qualification
# root, public), mounted from the anvilkit-trust ConfigMap at this path.
TRUST = "/etc/anvilkit/trust/ca.crt"
TRUST_NAMESPACES = ["anvilkit-apps", "anvilkit-data", "anvilkit-platform"]
CUSTODY = pathlib.Path(os.environ.get("ANVILKIT_OPENBAO_CUSTODY", str(STATE / "openbao")))
# The NKey users of the JetStream and what each may publish (all receive replies on
# _INBOX.>): the forwarders their owner's events, the relays their own durable pull
# consumer on their owner's stream, the streams Job the ANVILKIT_* stream API.
def _relay(stream: str) -> list[str]:
    return ["$JS.API.INFO", f"$JS.API.STREAM.INFO.{stream}", f"$JS.API.CONSUMER.*.{stream}", f"$JS.API.CONSUMER.*.{stream}.>",
            f"$JS.API.CONSUMER.MSG.NEXT.{stream}.>", f"$JS.API.CONSUMER.DURABLE.CREATE.{stream}.>", f"$JS.ACK.{stream}.>"]
NATS_USERS = {
    "knowledge-forwarder": ["anvilkit.knowledge.>", "$JS.API.INFO", "$JS.API.STREAM.INFO.ANVILKIT_KNOWLEDGE"],
    "mcp-forwarder": ["anvilkit.mcp.>", "$JS.API.INFO", "$JS.API.STREAM.INFO.ANVILKIT_MCP"],
    "knowledge-relay": _relay("ANVILKIT_KNOWLEDGE"),
    "mcp-relay": _relay("ANVILKIT_MCP"),
    "streams": ["$JS.API.INFO"] + [f"$JS.API.STREAM.{op}.ANVILKIT_{s}" for op in ("INFO", "CREATE", "UPDATE") for s in ("KNOWLEDGE", "MCP", "CONTROL")],
}
# One OpenBao role per workload identity: (role = ServiceAccount, namespace, KV v2 path
# under kv/). Each role's policy reads its own path and nothing else.
OPENBAO_ROLES = [
    ("anvilkit-agent-control", "anvilkit-apps", "anvilkit/apps/anvilkit-agent-control"),
    ("anvilkit-agent-control-migration", "anvilkit-apps", "anvilkit/apps/anvilkit-agent-control-migration"),
    ("anvilkit-agent-knowledge", "anvilkit-apps", "anvilkit/apps/anvilkit-agent-knowledge"),
    ("anvilkit-agent-mcp", "anvilkit-apps", "anvilkit/apps/anvilkit-agent-mcp"),
    ("anvilkit-agent-model-proxy", "anvilkit-apps", "anvilkit/apps/anvilkit-agent-model-proxy"),
    ("anvilkit-agent-background-worker", "anvilkit-apps", "anvilkit/apps/anvilkit-agent-background-worker"),
    ("anvilkit-migrations", "anvilkit-apps", "anvilkit/apps/migrations"),
    ("anvilkit-streams", "anvilkit-data", "anvilkit/data/streams"),
    ("anvilkit-secrets-data", "anvilkit-data", "anvilkit/sync/data"),
    ("anvilkit-secrets-platform", "anvilkit-platform", "anvilkit/sync/platform"),
    ("anvilkit-secrets-apps", "anvilkit-apps", "anvilkit/sync/apps"),
]
NAMESPACES = ["anvilkit-apps", "anvilkit-components", "anvilkit-parsing", "anvilkit-data", "anvilkit-platform", "anvilkit-observability"]
ROLES = ["anvilkit_control_app", "anvilkit_control_migrator", "anvilkit_knowledge_app", "anvilkit_knowledge_migrator",
         "anvilkit_knowledge_relay", "anvilkit_knowledge_forwarder", "anvilkit_knowledge_store_migrator", "anvilkit_knowledge_store",
         "anvilkit_mcp_app", "anvilkit_mcp_migrator", "anvilkit_mcp_relay", "temporal"]
OBJECT_USERS = ["ARTIFACTS", "MODEL_PROXY", "KNOWLEDGE"]
DR_USERS = {"backup": "anvilkit-backups", "inventory": "anvilkit-inventory", "snapshots": "anvilkit-snapshots",
            "removals": "anvilkit-memory-removals"}
# Knowledge's removal records are immutable: its user lists, reads and creates, never deletes.
DR_ACTIONS = {"removals": (["s3:ListBucket", "s3:GetBucketLocation"], ["s3:GetObject", "s3:PutObject"])}
# A child Application counts as healthy only once it is Synced, its last sync
# operation Succeeded and its own resources are Healthy. A freshly created child
# briefly reports Healthy before its first sync, and a child whose hook Job keeps
# failing stays Synced and Healthy (Argo CD leaves hooks out of both statuses):
# either would let the next wave start early. A failed operation is Degraded.
APP_HEALTH = """hs = {}
hs.status = "Progressing"
hs.message = "waiting for the first sync"
if obj.status ~= nil and obj.status.health ~= nil and obj.status.sync ~= nil and obj.status.sync.status == "Synced" then
  local op = obj.status.operationState
  if op ~= nil and op.phase ~= "Succeeded" then
    if op.phase == "Failed" or op.phase == "Error" then hs.status = "Degraded" end
    hs.message = "sync " .. tostring(op.phase) .. ": " .. tostring(op.message)
    return hs
  end
  hs.status = obj.status.health.status
  if obj.status.health.message ~= nil then hs.message = obj.status.health.message end
end
return hs
"""


def kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    p = subprocess.run([KUBECTL, "--kubeconfig", str(KUBECONFIG), *args], input=stdin, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {p.stderr.strip()[-400:]}")
    return p.stdout


def token(n: int = 24) -> str:
    return secrets.token_hex(n)


def nkey() -> dict[str, str]:
    """A NATS NKey user (seed and public key) from the pinned nats-box's nsc; the seed
    leaves the container on its standard output only."""
    out = subprocess.run(["docker", "run", "--rm", NATS_BOX, "nsc", "generate", "nkey", "--user"], capture_output=True, text=True, check=True).stdout.split()
    seed = next(w for w in out if w.startswith("SU"))
    public = next(w for w in out if w.startswith("U") and len(w) == 56)
    return {"seed": seed, "public": public}


def credentials() -> dict:
    path = STATE / "credentials.json"
    if path.exists():
        c = json.loads(path.read_text())
        added = False
        if "qdrant" not in c:  # added after the first environment: generated once, kept
            c["qdrant"] = token()
            added = True
        for user in NATS_USERS:  # P0.6: the NKey users, likewise
            if user not in c.setdefault("nats", {}):
                c["nats"][user] = nkey()
                added = True
        for u in DR_USERS:  # Knowledge's removal inventory user (P23) arrived later, likewise
            if u not in c["dr"]:
                c["dr"][u] = {"id": f"anvilkit-dr-{u}", "secret": token()}
                added = True
        if added:
            path.write_text(json.dumps(c, indent=2))
        return c
    c = {"roles": {r: token() for r in ROLES},
         "objectsRoot": {"user": "objects-admin", "password": token()},
         "objects": {u: {"id": f"anvilkit-{u.lower().replace('_', '-')}", "secret": token()} for u in OBJECT_USERS},
         "dr": {u: {"id": f"anvilkit-dr-{u}", "secret": token()} for u in DR_USERS},
         "queue": token(),
         "qdrant": token(),
         "principals": {"probe": token(32), "tenantA": token(32), "tenantB": token(32), "modelProxyWorkflow": token(32)},
         "nats": {user: nkey() for user in NATS_USERS}}
    old = os.umask(0o077)
    try:
        path.write_text(json.dumps(c, indent=2))
    finally:
        os.umask(old)
    return c


def dsn(role: str, db: str, password: str) -> str:
    """P0.6: every DSN verifies the server (the CNPG server certificate the qualification
    CA issued) against the trust bundle the workloads mount; pgx and node-postgres read
    sslmode=verify-full with sslrootcert alike."""
    return f"postgres://{role}:{password}@{BUSINESS}/{db}?sslmode=verify-full&sslrootcert={TRUST}"


def kv_payloads(c: dict) -> dict[str, dict[str, str]]:
    """Every workload's credentials by KV v2 path (kv/<path>): the keys the charts'
    SecretProviderClasses name (one file each) and the Secrets anvilkit-secrets syncs."""
    r, o, dr, n, p = c["roles"], c["objects"], c["dr"], c["nats"], c["principals"]
    queue = f"rediss://default:{c['queue']}@{QUEUE}"
    kv = lambda u, prefix: f"{prefix}_ACCESS_KEY_ID={u['id']}\n{prefix}_SECRET_ACCESS_KEY={u['secret']}\n"
    principals = {p["probe"]: {"tenantId": "tenant_probe", "projectId": "project_probe", "actorId": "probe", "roles": ["author"]},
                  p["tenantA"]: {"tenantId": "tenant_a", "projectId": "project_a", "actorId": "user_a", "roles": ["author"]},
                  p["tenantB"]: {"tenantId": "tenant_b", "projectId": "project_b", "actorId": "user_b", "roles": ["author"]}}
    return {
        "anvilkit/apps/anvilkit-agent-control": {
            "database-url": dsn("anvilkit_control_app", "anvilkit_control", r["anvilkit_control_app"]),
            "inventory-access-key-id": dr["inventory"]["id"], "inventory-secret-access-key": dr["inventory"]["secret"],
            "artifacts-access-key-id": o["ARTIFACTS"]["id"], "artifacts-secret-access-key": o["ARTIFACTS"]["secret"]},
        "anvilkit/apps/anvilkit-agent-control-migration": {
            "migration-dsn": dsn("anvilkit_control_migrator", "anvilkit_control", r["anvilkit_control_migrator"])},
        "anvilkit/apps/anvilkit-agent-knowledge": {
            "database-url": dsn("anvilkit_knowledge_app", "anvilkit_knowledge", r["anvilkit_knowledge_app"]),
            "objects-credentials": kv(o["KNOWLEDGE"], "KNOWLEDGE"),
            # P23: Knowledge's removal inventory user, limited to its own DR bucket.
            "removals-credentials": kv(dr["removals"], "REMOVALS"),
            "qdrant-api-key": c["qdrant"],
            "store-database-url": dsn("anvilkit_knowledge_store", "anvilkit_knowledge", r["anvilkit_knowledge_store"]),
            "forwarder-database-url": dsn("anvilkit_knowledge_forwarder", "anvilkit_knowledge", r["anvilkit_knowledge_forwarder"]),
            "nats-creds": n["knowledge-forwarder"]["seed"],
            "relay-database-url": dsn("anvilkit_knowledge_relay", "anvilkit_knowledge", r["anvilkit_knowledge_relay"]),
            "relay-queue-url": queue, "relay-nats-creds": n["knowledge-relay"]["seed"]},
        "anvilkit/apps/anvilkit-agent-mcp": {
            "database-url": dsn("anvilkit_mcp_app", "anvilkit_mcp", r["anvilkit_mcp_app"]),
            "nats-creds": n["mcp-forwarder"]["seed"],
            "relay-database-url": dsn("anvilkit_mcp_relay", "anvilkit_mcp", r["anvilkit_mcp_relay"]),
            "relay-queue-url": queue, "relay-nats-creds": n["mcp-relay"]["seed"]},
        "anvilkit/apps/anvilkit-agent-model-proxy": {
            "store-access-key-id": o["MODEL_PROXY"]["id"], "store-secret-access-key": o["MODEL_PROXY"]["secret"]},
        "anvilkit/apps/anvilkit-agent-background-worker": {"queue-url": queue},
        "anvilkit/apps/migrations": {
            "migration-knowledge": dsn("anvilkit_knowledge_migrator", "anvilkit_knowledge", r["anvilkit_knowledge_migrator"]),
            "migration-mcp": dsn("anvilkit_mcp_migrator", "anvilkit_mcp", r["anvilkit_mcp_migrator"]),
            "migration-knowledge-store": dsn("anvilkit_knowledge_store_migrator", "anvilkit_knowledge", r["anvilkit_knowledge_store_migrator"])},
        "anvilkit/data/streams": {"nats-streams-nkey": n["streams"]["seed"]},
        "anvilkit/sync/data": {
            "dr-backup-access-key-id": dr["backup"]["id"], "dr-backup-secret-access-key": dr["backup"]["secret"],
            "valkey-password": c["queue"], "qdrant-api-key": c["qdrant"],
            "objects-root-user": c["objectsRoot"]["user"], "objects-root-password": c["objectsRoot"]["password"],
            **{f"objects-{u.lower().replace('_', '-')}-{k}": v for u, x in o.items() for k, v in (("access-key-id", x["id"]), ("secret-access-key", x["secret"]))}},
        "anvilkit/sync/platform": {"temporal-db-password": r["temporal"], "probe-token": p["probe"]},
        "anvilkit/sync/apps": {
            "api-principals": json.dumps(principals),
            "model-proxy-principals": json.dumps({p["modelProxyWorkflow"]: {"principalId": "anvilkit-agent-workflow", "kind": "workflow"}})},
    }


def retire_bootstrap_secrets() -> None:
    """P0.6: the Secrets earlier bootstrap runs created (label anvilkit.io/bootstrap) are
    deleted: their material now lives in OpenBao only."""
    kubectl("delete", "secrets", "-A", "-l", "anvilkit.io/bootstrap=qualification", "--ignore-not-found")


def trust_bundle() -> bool:
    """The anvilkit-trust ConfigMaps (key ca.crt): the qualification root's public
    certificate, once cert-manager has issued it. Public material: no Secret."""
    # The self-signed root's own certificate (public); its key never leaves cert-manager.
    ca = kubectl("-n", "cert-manager", "get", "secret", "anvilkit-qualification-ca-root", "-o", "jsonpath={.data.tls\\.crt}", check=False).strip()
    if not ca:
        return False
    import base64
    pem = base64.b64decode(ca).decode()
    for ns in TRUST_NAMESPACES:
        kubectl("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "anvilkit-trust", "namespace": ns,
                "labels": {"app.kubernetes.io/part-of": "anvilkit"}}, "data": {"ca.crt": pem}}))
    return True


def nats_auth(c: dict) -> None:
    """The NATS server's NKey users and their permissions (public keys only), the
    ConfigMap the combination's NATS mounts and includes; its reloader re-reads it."""
    users = []
    for user, publish in NATS_USERS.items():
        users.append(f"    {{ nkey: {json.dumps(c['nats'][user]['public'])}, permissions: {{ publish: {{ allow: {json.dumps(publish)} }}, "
                     f"subscribe: {{ allow: [\"_INBOX.>\"] }} }} }}")
    conf = "authorization {\n  users: [\n" + "\n".join(users) + "\n  ]\n}\n"
    kubectl("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "anvilkit-nats-auth", "namespace": "anvilkit-data",
            "labels": {"app.kubernetes.io/part-of": "anvilkit"}}, "data": {"auth.conf": conf}}))


def role_passwords(c: dict) -> bool:
    """P0.6: the login roles' passwords, set over each CNPG primary's local socket
    (psql as postgres, the statements on standard input); the operator manages only
    the roles' existence. Returns False until both primaries run."""
    clusters = {"anvilkit-business": [r for r in ROLES if r != "temporal"], "anvilkit-temporal": ["temporal"]}
    for cluster, roles in clusters.items():
        pods = kubectl("-n", "anvilkit-data", "get", "pods", "-l", f"cnpg.io/cluster={cluster},cnpg.io/instanceRole=primary",
                       "-o", "jsonpath={.items[?(@.status.phase==\"Running\")].metadata.name}").split()
        if not pods:
            return False
        sql = "".join(f"ALTER ROLE \"{role}\" WITH LOGIN PASSWORD '{c['roles'][role]}';\n" for role in roles)
        try:
            kubectl("-n", "anvilkit-data", "exec", "-i", pods[0], "-c", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", "postgres", "-d", "postgres",
                    stdin="SET log_statement = 'none';\n" + sql)
        except RuntimeError:
            return False  # a role the operator has not created yet: the next round retries
    return True


def dr_store(c: dict) -> None:
    env = dict(line.split("=", 1) for line in (STATE / "dr.env").read_text().split())
    script = ["set -eu", 'mc alias set dr http://anvilkit-qualification-dr:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null']
    for user, bucket in DR_USERS.items():
        if user in DR_ACTIONS:
            on_bucket, on_objects = DR_ACTIONS[user]
            statements = [{"Effect": "Allow", "Action": on_bucket, "Resource": [f"arn:aws:s3:::{bucket}"]},
                          {"Effect": "Allow", "Action": on_objects, "Resource": [f"arn:aws:s3:::{bucket}/*"]}]
        else:
            statements = [{"Effect": "Allow", "Action": ["s3:*"], "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}]
        policy = json.dumps({"Version": "2012-10-17", "Statement": statements})
        script += [f"mc mb --ignore-existing dr/{bucket} >/dev/null",
                   f"printf '%s' '{policy}' > /tmp/{bucket}.json",
                   f"mc admin policy create dr {bucket} /tmp/{bucket}.json >/dev/null",
                   f'mc admin user info dr "$DR_{user.upper()}_ID" >/dev/null 2>&1 || mc admin user add dr "$DR_{user.upper()}_ID" "$DR_{user.upper()}_SECRET" >/dev/null',
                   f'mc admin policy attach dr {bucket} --user "$DR_{user.upper()}_ID" >/dev/null 2>&1 || true']
    envs = {**env, **{f"DR_{u.upper()}_ID": v["id"] for u, v in c["dr"].items()}, **{f"DR_{u.upper()}_SECRET": v["secret"] for u, v in c["dr"].items()}}
    cmd = ["docker", "run", "--rm", "-i", "--network", "kind", "-e", "HOME=/tmp", "-e", "MC_CONFIG_DIR=/tmp/.mc"]
    for k in envs:
        cmd += ["-e", k]
    p = subprocess.run(cmd + ["--entrypoint", "sh", MC], input="\n".join(script) + "\n", capture_output=True, text=True, env={**os.environ, **envs})
    if p.returncode != 0:
        raise RuntimeError(f"DR store setup: {p.stderr.strip()[-400:]}")


def argocd() -> None:
    repo, name, version = ARGOCD_CHART
    tmp = STATE / "charts"
    tmp.mkdir(exist_ok=True)
    pkg = tmp / f"{name}-{version}.tgz"
    if not pkg.exists():
        subprocess.run([HELM, "pull", name, "--repo", repo, "--version", version, "-d", str(tmp)], check=True, capture_output=True)
        subprocess.run([HELM, "push", str(pkg), "oci://localhost:5001/platform", "--plain-http"], check=True, capture_output=True)
    repos = {path: {"url": f"{REGISTRY}/{path}", "name": f"anvilkit-{path}", "type": "helm", "enableOCI": "true", "insecureOCIForceHttp": "true"}
             for path in ("charts", "platform", "environments")}
    values = {"dex": {"enabled": False}, "notifications": {"enabled": False},
              "configs": {"params": {"server.insecure": True},
                          "cm": {"resource.customizations.health.argoproj.io_Application": APP_HEALTH, "timeout.reconciliation": "60s"},
                          "repositories": repos}}
    vf = STATE / "argocd-values.yaml"
    vf.write_text(yaml.safe_dump(values))
    subprocess.run([HELM, "--kubeconfig", str(KUBECONFIG), "upgrade", "--install", "argocd", f"oci://localhost:5001/platform/{name}",
                    "--version", version, "--plain-http", "-n", "anvilkit-platform", "-f", str(vf), "--wait", "--timeout", "30m"], check=True, capture_output=True)


def bao(pod: str, *args: str, stdin: str | None = None, token: str | None = None) -> str:
    """bao inside an OpenBao member over its TLS listener (the chart sets BAO_CACERT; the
    server certificate names 127.0.0.1). A token travels as the first line of standard
    input, never on a command line."""
    cmd = ["-n", "anvilkit-platform", "exec", "-i", pod, "--", "env", "BAO_ADDR=https://127.0.0.1:8200"]
    if token is None:
        return kubectl(*cmd, "bao", *args, stdin=stdin, check=False)
    return kubectl(*cmd, "sh", "-c", 'read -r BAO_TOKEN; export BAO_TOKEN; exec bao "$@"', "bao", *args,
                   stdin=token + "\n" + (stdin or ""), check=False)


def custody() -> dict:
    return json.loads((CUSTODY / "init.json").read_text())


def generate_root(pod: str) -> str:
    """A root token from the custody shares through the unauthenticated root-generation
    endpoint (sys/generate-root; bao 2.6's CLI uses the authenticated variant, so the
    API is driven directly, and the listener keeps the endpoint enabled: completing it
    needs the threshold of shares). The server's one-time pad is applied here; the
    shares and the encoded token never appear on a command line."""
    import base64
    bao(pod, "delete", "sys/generate-root/attempt")
    start = json.loads(bao(pod, "write", "-format=json", "-f", "sys/generate-root/attempt") or "{}").get("data", {})
    nonce, otp = start.get("nonce"), start.get("otp")
    if not nonce or not otp:
        raise RuntimeError("root generation did not start (is the unauthenticated generate-root endpoint enabled on the listener?)")
    out: dict = {}
    for share in custody()["unseal_keys_b64"][:3]:
        out = json.loads(bao(pod, "write", "-format=json", "sys/generate-root/update", f"nonce={nonce}", "key=-", stdin=share) or "{}").get("data", {})
    encoded = out.get("encoded_token") or out.get("encoded_root_token")
    if not out.get("complete") or not encoded:
        bao(pod, "delete", "sys/generate-root/attempt")
        raise RuntimeError("root generation did not complete with the custody shares")
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
    return bytes(a ^ b for a, b in zip(raw, otp.encode())).decode()


def configure_openbao(pod: str, c: dict) -> None:
    """KV v2 at kv/, Kubernetes auth (the CSI provider presents each Pod's
    ServiceAccount token of audience openbao), one read-only policy and role per
    workload path, every path's credentials; once per change of the configuration
    (its digest in the custody directory). The root token used is revoked at the end:
    the initial one after the first configuration, a generated one after each later."""
    import hashlib
    payloads = kv_payloads(c)
    digest = hashlib.sha256(json.dumps([OPENBAO_ROLES, payloads], sort_keys=True).encode()).hexdigest()
    marker = CUSTODY / "configured"
    if marker.exists() and marker.read_text().strip() == digest:
        return
    material = custody()
    root = material.get("root_token") or generate_root(pod)
    try:
        def call(*args: str, stdin: str | None = None) -> str:
            return bao(pod, *args, stdin=stdin, token=root)
        mounts = json.loads(call("secrets", "list", "-format=json") or "{}")
        if "kv/" not in mounts:
            call("secrets", "enable", "-path=kv", "-version=2", "kv")
        if "kubernetes/" not in json.loads(call("auth", "list", "-format=json") or "{}"):
            call("auth", "enable", "kubernetes")
        call("write", "auth/kubernetes/config", "kubernetes_host=https://kubernetes.default.svc:443")
        for role, namespace, path in OPENBAO_ROLES:
            call("policy", "write", role, "-", stdin=f'path "kv/data/{path}" {{ capabilities = ["read"] }}\n')
            call("write", f"auth/kubernetes/role/{role}", f"bound_service_account_names={role}", f"bound_service_account_namespaces={namespace}",
                 "audience=openbao", f"token_policies={role}", "token_ttl=10m", "token_max_ttl=20m", "token_no_default_policy=true")
        for path, data in payloads.items():
            out = call("kv", "put", "-mount=kv", "-format=json", path, "-", stdin=json.dumps(data))
            if '"version"' not in out:
                raise RuntimeError(f"OpenBao kv put {path} failed")
        marker.write_text(digest + "\n")
        print(f"{pod}: OpenBao configured ({len(OPENBAO_ROLES)} roles, {len(payloads)} credential paths)", flush=True)
    finally:
        bao(pod, "token", "revoke", "-self", token=root)
        if "root_token" in material:
            material.pop("root_token")
            (CUSTODY / "init.json").write_text(json.dumps(material))
            print(f"{pod}: the initial root token is revoked; later configurations generate one from the custody shares", flush=True)


def openbao(c: dict) -> None:
    """Initialize OpenBao once (Shamir 3-of-5; the shares only in the custody directory,
    never in the DR store or the cluster), unseal every running member that is sealed,
    then configure it from the active member. The StatefulSet starts its members in
    order, so each later member appears only after the previous one is unsealed.
    Production unseals through ENV-10's KMS, not with shares on a workstation."""
    init_file = CUSTODY / "init.json"
    pods = json.loads(kubectl("-n", "anvilkit-platform", "get", "pods", "-l", "app.kubernetes.io/name=openbao,component=server", "-o", "json"))["items"]
    unsealed = []
    for pod in sorted(p["metadata"]["name"] for p in pods if p.get("status", {}).get("phase") == "Running"):
        status = json.loads(bao(pod, "status", "-format=json") or "{}")
        if not status:
            continue
        if not status.get("initialized"):
            if pod != "openbao-0" or init_file.exists():
                continue  # a follower joins through raft; an existing init.json is never overwritten
            CUSTODY.mkdir(parents=True, exist_ok=True)
            os.chmod(CUSTODY, 0o700)
            out = bao(pod, "operator", "init", "-key-shares=5", "-key-threshold=3", "-format=json")
            json.loads(out)
            old = os.umask(0o077)
            try:
                init_file.write_text(out)
            finally:
                os.umask(old)
            print(f"{pod}: OpenBao initialized (3-of-5 shares in the custody directory only)", flush=True)
            status = json.loads(bao(pod, "status", "-format=json") or "{}")
        if status.get("sealed") and init_file.exists():
            for key in custody()["unseal_keys_b64"][:3]:
                # `operator unseal -` does not read standard input in bao 2.6; `write key=-` does,
                # which keeps the share off every command line.
                bao(pod, "write", "-format=json", "sys/unseal", "key=-", stdin=key)
            print(f"{pod}: unsealed", flush=True)
            status = json.loads(bao(pod, "status", "-format=json") or "{}")
        if status.get("initialized") and not status.get("sealed"):
            unsealed.append((pod, status))
    # The configuration goes to the active member (status is_self under HA raft in bao 2.6).
    active = next((pod for pod, st in unsealed if st.get("is_self") or st.get("ha_mode") == "active" or not st.get("ha_enabled")), None)
    if active and init_file.exists():
        configure_openbao(active, c)


def fix_forward(namespace: str) -> None:
    """A sync of an earlier source (chart version or values) that waits on a resource
    the current source repairs never finishes by itself (it waits for health that
    source cannot reach), and Argo CD starts the current source only after it, for the
    root and for each child Application alike. Terminate such an operation, as
    `argocd app terminate-op` does, so the current source syncs."""
    for app in json.loads(kubectl("-n", namespace, "get", "applications.argoproj.io", "-o", "json"))["items"]:
        op = app.get("status", {}).get("operationState", {})
        syncing = op.get("syncResult", {}).get("source")  # chart, version and values the operation applies
        if op.get("phase") == "Running" and syncing and syncing != app["spec"].get("source"):
            kubectl("-n", namespace, "patch", "applications.argoproj.io", app["metadata"]["name"], "--type", "merge",
                    "-p", json.dumps({"status": {"operationState": {"phase": "Terminating"}}}))
            print(f"{app['metadata']['name']}: terminated the running sync of an earlier source "
                  f"({syncing.get('targetRevision')}); {app['spec']['source'].get('targetRevision')} follows", flush=True)


def wait(seconds: int, c: dict) -> bool:
    end = time.time() + seconds
    last = ""
    done: set[str] = set()
    while time.time() < end:
        # P0.6: the inputs the data plane and the CSI volumes wait on, each once it can
        # be applied: the CA bundle (after cert-manager issued the root), OpenBao (init,
        # unseal, configuration), the role passwords (after the CNPG primaries run).
        if "trust" not in done and trust_bundle():
            done.add("trust")
        openbao(c)
        if "roles" not in done and role_passwords(c):
            done.add("roles")
            print("database role passwords set", flush=True)
        fix_forward("anvilkit-platform")
        apps = json.loads(kubectl("-n", "anvilkit-platform", "get", "applications.argoproj.io", "-o", "json"))["items"]
        rows = sorted((a["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", "0"), a["metadata"]["name"],
                       a.get("status", {}).get("sync", {}).get("status", "-"), a.get("status", {}).get("health", {}).get("status", "-")) for a in apps)
        line = "; ".join(f"{n}={s}/{h}" for _, n, s, h in rows)
        if line != last:
            print(time.strftime("%H:%M:%S"), line, flush=True)
            last = line
        # Converged means the root synced its current target: right after a new root
        # revision is applied the root still reads Synced/Healthy against the previous one.
        root = next((a for a in apps if "anvilkit.io/layer" not in a["metadata"].get("labels", {})), None)
        st = (root or {}).get("status", {})
        target = (root or {}).get("spec", {}).get("source", {}).get("targetRevision")
        root_done = (st.get("sync", {}).get("revision") == target and st.get("operationState", {}).get("phase") == "Succeeded"
                     and st.get("operationState", {}).get("syncResult", {}).get("revision") == target)
        if rows and root_done and all(s == "Synced" and h == "Healthy" for _, _, s, h in rows) and len(rows) > 1:
            return True
        time.sleep(15)
    return False


def admission_check(run: str) -> bool:
    """P0.5: the Job-boundary admission the combination installed, checked
    live after the sync: deploy/policies/check.sh --cluster against this
    cluster with its administrator context, the launcher's own identity (a
    one-hour token of the Workflow's ServiceAccount, never kept) and the
    combination's registry values. Results under outputs/qualification/<run>/admission."""
    out = ROOT / "outputs/qualification" / run / "admission"
    out.mkdir(parents=True, exist_ok=True)
    combination = yaml.safe_load((ROOT / "deploy/gitops/environments/qualification.yaml").read_text())
    app = next(a for a in combination["applications"] if a["name"] == "anvilkit-job-admission")
    values = out / "values.yaml"
    values.write_text(yaml.safe_dump(app.get("values", {})))
    cfg = yaml.safe_load(KUBECONFIG.read_text())
    cluster = next(c for c in cfg["clusters"] if c["name"] == next(x for x in cfg["contexts"] if x["name"] == cfg["current-context"])["context"]["cluster"])
    def identity(sa: str, namespace: str, path: pathlib.Path) -> None:
        tok = kubectl("-n", "anvilkit-apps", "create", "token", sa, "--duration=1h").strip()
        path.write_text(yaml.safe_dump({"apiVersion": "v1", "kind": "Config", "clusters": [cluster], "users": [{"name": "launcher", "user": {"token": tok}}],
                                        "contexts": [{"name": "launcher", "context": {"cluster": cluster["name"], "user": "launcher", "namespace": namespace}}],
                                        "current-context": "launcher"}))
        path.chmod(0o600)

    # The Workflow's launcher for anvilkit-components, Knowledge's for anvilkit-parsing (P0.7).
    launcher, parser_launcher = out / "launcher.kubeconfig", out / "parser-launcher.kubeconfig"
    identity("anvilkit-agent-workflow", "anvilkit-components", launcher)
    identity("anvilkit-agent-knowledge", "anvilkit-parsing", parser_launcher)
    try:
        env = dict(os.environ, KUBECONFIG=str(KUBECONFIG), ANVILKIT_DEV_KUBE_CONTEXT=cfg["current-context"],
                   ANVILKIT_LAUNCHER_KUBECONFIG=str(launcher), ANVILKIT_PARSER_LAUNCHER_KUBECONFIG=str(parser_launcher),
                   ANVILKIT_POLICY_VALUES=str(values))
        p = subprocess.run(["sh", str(ROOT / "deploy/policies/check.sh"), "--cluster"], env=env, capture_output=True, text=True)
    finally:
        launcher.unlink()
        parser_launcher.unlink()
    (out / "check-cluster.log").write_text(p.stdout + p.stderr)
    print(p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "admission check produced no output", flush=True)
    return p.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--wait", type=int, default=1800)
    a = ap.parse_args()
    root_app = ROOT / "outputs/qualification" / a.run / "gitops/root-application.yaml"
    for need in (KUBECONFIG, STATE / "dr.env", root_app, ROOT / ".local/release/cosign.pub"):
        if not need.exists():
            print(f"UNEXECUTED: {need.relative_to(ROOT)} is missing")
            return 2
    c = credentials()
    for node in kubectl("get", "nodes", "-l", "!node-role.kubernetes.io/control-plane", "-o", "name").split():
        kubectl("label", "--overwrite", node, "anvilkit.io/pool=components")
    for ns in NAMESPACES:
        kubectl("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "Namespace",
                                                      "metadata": {"name": ns, "labels": {"app.kubernetes.io/part-of": "anvilkit"}}}))
    retire_bootstrap_secrets()
    nats_auth(c)
    print("earlier bootstrap Secrets retired; NATS users published (public keys)", flush=True)
    dr_store(c)
    print("DR store buckets and users ready", flush=True)
    argocd()
    print("Argo CD installed", flush=True)
    kubectl("apply", "-f", str(root_app))
    fix_forward("anvilkit-platform")
    ok = wait(a.wait, c)
    print("environment converged" if ok else "environment did NOT converge within the wait")
    if ok:
        ok = admission_check(a.run)
        print("Job-boundary admission verified live" if ok else "Job-boundary admission check FAILED", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
