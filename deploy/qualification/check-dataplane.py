"""P0.6 AC3 data-plane probes of a live qualification environment (DEVELOPMENT_ONLY).

  .local/verification-venv/bin/python deploy/qualification/check-dataplane.py --run RUN

Port-forwards the data-plane Services of anvilkit-data (NATS, the Valkey master proxy,
the Valkey servers' TLS port, the Sentinel, the business PostgreSQL primary) to local
ports, writes the qualification root's public certificate and the credentials the
probes present (two NKey seeds, the queue password) to files in a private temporary
directory, and runs each subtest of TestQualificationDataPlane (tests/integration, built
once) through its own fresh port-forward to the one endpoint it probes:
  * NATS: a plaintext client, a TLS client without credentials and an NKey the server
    does not list are refused; the MCP forwarder's credential is accepted and reaches
    its own stream's API, but its publication to an anvilkit.knowledge subject is a
    permissions violation and nothing reaches the Knowledge stream on that subject;
  * Valkey, at each of the three endpoints: a plaintext client, a client without
    credentials and a wrong password are refused; the deployed password is accepted;
    the ACL users the server holds are read (read-only);
  * PostgreSQL (supplementary, not AC3): whether pg_hba refuses a connection without
    TLS before authentication (a wrong password is presented, never a real one).
No probe writes to a stream or a queue key. The credentials never reach a command
line, an environment variable or the evidence; the directory is removed afterwards.
Writes outputs/qualification/<RUN>/dataplane/ac3.json. Exit 1 when an AC3 case
failed; the supplementary PostgreSQL result is recorded and printed, not gating.
A connection through kubectl port-forward reaches each server from inside its Pod:
the probes show what the servers accept, not what the network admits (P0.4).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / ".local/qualification"
KUBECTL = str(ROOT / ".local/bin/kubectl")
NAMESPACE = "anvilkit-data"
# (name, Service, port, the name its server certificate carries)
FORWARDS = [
    ("nats", "anvilkit-nats", 4222, "anvilkit-nats.anvilkit-data.svc"),
    ("master", "anvilkit-valkey-queue-master", 6379, "anvilkit-valkey-queue-master.anvilkit-data.svc.cluster.local"),
    ("server", "anvilkit-valkey-queue", 6380, "anvilkit-valkey-queue.anvilkit-data.svc.cluster.local"),
    ("sentinel", "anvilkit-valkey-queue-sentinel", 26379, "anvilkit-valkey-queue-sentinel.anvilkit-data.svc.cluster.local"),
    ("postgres", "anvilkit-business-rw", 5432, "anvilkit-business-rw.anvilkit-data.svc"),
]
SEEDS = ("mcp-forwarder", "streams")
AC3_CASES = ["nats/plaintext-refused", "nats/anonymous-refused", "nats/unlisted-nkey-refused", "nats/out-of-scope-publish-refused",
             *(f"valkey/{n}/{c}" for n in ("master", "server", "sentinel")
               for c in ("plaintext-refused", "no-credential-refused", "wrong-password-refused", "credential-accepted")),
             "valkey/acl-users"]
POSTGRES = {"database": "anvilkit_control", "role": "anvilkit_control_app"}
POSTGRES_CASES = ["postgres/plaintext-refused-before-authentication"]


def kubectl(kubeconfig: str, *args: str) -> str:
    p = subprocess.run([KUBECTL, "--kubeconfig", kubeconfig, *args], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:4])}: {p.stderr.strip()[-300:]}")
    return p.stdout


def forward(kubeconfig: str, service: str, port: int) -> tuple[subprocess.Popen, str]:
    """One port-forward to a Service on a free local port; returns it with its address."""
    p = subprocess.Popen([KUBECTL, "--kubeconfig", kubeconfig, "-n", NAMESPACE, "port-forward", "--address", "127.0.0.1", f"svc/{service}", f":{port}"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 60
    assert p.stdout is not None
    seen = []
    while time.time() < deadline:
        line = p.stdout.readline()
        if not line:
            break
        seen.append(line.strip())
        m = re.search(r"Forwarding from 127\.0\.0\.1:(\d+) ->", line)
        if m:
            return p, f"127.0.0.1:{m.group(1)}"
    p.kill()
    raise RuntimeError(f"port-forward to svc/{service}:{port} did not start: {' '.join(seen)[-300:]}")


def private(directory: pathlib.Path, name: str, content: str) -> str:
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return str(path)


def endpoint(case: str) -> str:
    """The one port-forward a case needs."""
    kind, _, rest = case.partition("/")
    if kind == "valkey":
        return "server" if rest == "acl-users" else rest.partition("/")[0]
    return kind


def run_case(binary: str, config: str, case: str) -> list[dict]:
    """One subtest of TestQualificationDataPlane through test2json; its rows (result and log lines)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANVILKIT_")}
    env["ANVILKIT_QUALIFICATION_DATAPLANE"] = config
    pattern = "^TestQualificationDataPlane$/" + "/".join(f"^{re.escape(part)}$" for part in case.split("/"))
    p = subprocess.run(["go", "tool", "test2json", "-t", "-p", "integration", binary, "-test.v=test2json", "-test.count=1", "-test.run", pattern],
                       cwd=ROOT / "tests/integration", env=env, capture_output=True, text=True, timeout=300)
    rows: dict[str, dict] = {}
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = ev.get("Test", "").partition("/")[2]
        if name != case:
            continue
        row = rows.setdefault(name, {"case": name, "result": "UNEXECUTED", "log": []})
        if ev.get("Action") == "output":
            text = ev.get("Output", "").rstrip()
            if text.strip() and not text.lstrip().startswith(("=== RUN", "--- PASS", "--- FAIL", "--- SKIP", "=== PAUSE", "=== CONT")):
                row["log"].append(text.strip())
        elif ev.get("Action") in ("pass", "fail", "skip"):
            row["result"] = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[ev["Action"]]
    return list(rows.values()) or [{"case": case, "result": "UNEXECUTED", "log": [p.stderr.strip()[-300:]]}]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--kubeconfig", default=str(STATE / "kubeconfig"))
    a = ap.parse_args()
    creds = json.loads((STATE / "credentials.json").read_text())
    ca = json.loads(kubectl(a.kubeconfig, "-n", NAMESPACE, "get", "configmap", "anvilkit-trust", "-o", "json"))["data"]["ca.crt"]
    hba = json.loads(kubectl(a.kubeconfig, "-n", NAMESPACE, "get", "clusters.postgresql.cnpg.io", "anvilkit-business", "-o", "json"))["spec"].get("postgresql", {}).get("pg_hba", [])
    services = {name: (service, port, server) for name, service, port, server in FORWARDS}
    work = pathlib.Path(tempfile.mkdtemp(prefix="anvilkit-dataplane-"))
    os.chmod(work, 0o700)
    rows: list[dict] = []
    try:
        binary = str(work / "dataplane.test")
        build = subprocess.run(["go", "test", "-c", "-tags", "integration", "-o", binary, "."], cwd=ROOT / "tests/integration",
                               capture_output=True, text=True, timeout=900)
        if build.returncode != 0:
            raise RuntimeError(f"go test -c: {build.stderr.strip()[-600:]}")
        password = private(work, "queue", creds["queue"])
        base = {"ca": private(work, "ca.crt", ca),
                "nats": {"seeds": {u: private(work, f"{u}.nk", creds["nats"][u]["seed"]) for u in SEEDS}}}
        started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        # One fresh port-forward per case: a server that closes or resets a refused
        # connection can end a kubectl port-forward, which must not decide a later case.
        for i, case in enumerate([*AC3_CASES, *POSTGRES_CASES]):
            name = endpoint(case)
            service, port, server = services[name]
            p, address = forward(a.kubeconfig, service, port)
            try:
                config = json.loads(json.dumps(base))
                if name == "nats":
                    config["nats"].update(address=address, serverName=server)
                elif name == "postgres":
                    config["postgres"] = {"address": address, "serverName": server, **POSTGRES}
                else:
                    config["valkey"] = [{"name": name, "address": address, "serverName": server, "passwordFile": password}]
                rows += run_case(binary, private(work, f"case-{i}.json", json.dumps(config)), case)
            finally:
                p.kill()
                p.wait()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    ac3 = [r for r in rows if r["case"].startswith(("nats/", "valkey/"))]
    supplementary = [r for r in rows if r["case"].startswith("postgres/")]
    # Every expected case ran and passed: a probe that stopped early is a FAIL, not a smaller PASS.
    results = {r["case"]: r["result"] for r in ac3}
    ac3_pass = all(results.get(c) == "PASS" for c in AC3_CASES)
    ac3 += [{"case": c, "result": "UNEXECUTED", "log": []} for c in AC3_CASES if c not in results]
    acl = next((line for r in rows if r["case"] == "valkey/acl-users" for line in r["log"] if "ACL users:" in line), "")
    record = {
        "check": "P0.6 AC3 data-plane probes", "run": a.run, "started": started,
        "placement": "DEVELOPMENT_ONLY: kind qualification cluster; probes through kubectl port-forward reach each server from inside its Pod",
        "ac3": "PASS" if ac3_pass else "FAIL",
        "cases": ac3,
        "valkeyAclUsers": acl.partition("ACL users:")[2].strip().split(",") if acl else None,
        "supplementary": {"postgresPlaintext": supplementary, "businessClusterPgHba": hba},
    }
    out = ROOT / "outputs/qualification" / a.run / "dataplane"
    out.mkdir(parents=True, exist_ok=True)
    (out / "ac3.json").write_text(json.dumps(record, indent=2) + "\n")
    for r in rows:
        print(f"{r['result']:10} {r['case']}")
    print(f"AC3 {record['ac3']}; Valkey ACL users: {record['valkeyAclUsers']}; PostgreSQL plaintext before authentication: "
          f"{supplementary[0]['result'] if supplementary else 'UNEXECUTED'} (pg_hba entries: {hba})")
    print(f"evidence: {out / 'ac3.json'}")
    return 0 if ac3_pass else 1


if __name__ == "__main__":
    sys.exit(main())
