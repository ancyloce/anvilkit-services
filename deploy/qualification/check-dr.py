#!/usr/bin/env python3
"""DR store permission and immutability probes (P3.7 AC3).

  .local/verification-venv/bin/python deploy/qualification/check-dr.py --self-test
  .local/verification-venv/bin/python deploy/qualification/check-dr.py --local [--out DIR]
  .local/verification-venv/bin/python deploy/qualification/check-dr.py --run RUN

The DR store's users and buckets come from deploy/qualification/bootstrap.py
(DR_USERS, DR_BUCKETS, dr_script). The probes, each as the named user:

  inventory   creates an obligation object; deleting it is refused (AccessDenied);
              an overwrite leaves the original version readable
  removals    likewise create-only
  snapshots   uploads a snapshot; deleting it is refused
  backup      (CloudNativePG's barman user, which keeps DeleteObject for its own
              retention) cannot delete the locked version of a fresh backup:
              backups survive a prune attempt by the backup writer
  prune       can neither read nor write; deleting a locked version within its
              retention is refused

--local applies dr_script() to a disposable MinIO container (the foundation's
image, from the local image cache) and runs every probe there: it proves the
policies and the lock settings, not the qualification host's DR store. --run
probes the qualification DR store (anvilkit-qualification-dr) with the
environment's credentials and writes outputs/qualification/<RUN>/dr/dr.json;
it writes probe objects under p37-probe/ that the lock keeps until the
retention ends, so run it with that environment operator's authorization.
--self-test checks the policy model. Exit 0 PASS, 1 FAIL, 2 UNEXECUTED.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import secrets
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bootstrap  # noqa: E402  (DR_USERS, DR_BUCKETS, dr_script, dr_policy)

ROOT = pathlib.Path(__file__).resolve().parents[2]
MINIO = "minio/minio:RELEASE.2025-09-07T16-13-09Z"
MC = "minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727"

# Each probe: (user, shell, expectation). The shell runs in an mc container with
# the alias "as" for that user and "root" for the store's root; it prints one
# line "RESULT <name> ok|denied|<detail>" per step.
PROBES = r"""
set -u
key=p37-probe/$(date -u +%s)-$$
# The mc image has no grep or sed: classify with case, parse with expansions.
step() { if eval "$2" >/dev/null 2>/tmp/err; then echo "RESULT $1 ok"; else
  case "$(cat /tmp/err)" in
    *"Access Denied"*|*"Insufficient permissions"*|*"not allowed"*|*"WORM"*|*"locked"*|*"retention"*|*"protected"*) echo "RESULT $1 denied" ;;
    *) echo "RESULT $1 error $(tr '\n' ' ' </tmp/err | cut -c1-160)" ;;
  esac; fi; }
version() { line=$(mc ls --versions --json "$1" 2>/dev/null | tail -1); v=${line#*\"versionId\":\"}; [ "$v" != "$line" ] && echo "${v%%\"*}"; }
echo original > /tmp/original; echo tampered > /tmp/tampered
# inventory: create-only, the original survives an overwrite
step inventory.put "mc cp /tmp/original inventory/anvilkit-inventory/$key"
v1=$(version "root/anvilkit-inventory/$key")
step inventory.delete "mc rm inventory/anvilkit-inventory/$key"
step inventory.overwrite "mc cp /tmp/tampered inventory/anvilkit-inventory/$key"
if [ -n "$v1" ] && [ "$(mc cat --version-id "$v1" "root/anvilkit-inventory/$key" 2>/dev/null)" = original ]; then echo "RESULT inventory.originalReadable ok"; else echo "RESULT inventory.originalReadable missing"; fi
step inventory.deleteVersion "mc rm --version-id '$v1' inventory/anvilkit-inventory/$key"
# removals: create-only
step removals.put "mc cp /tmp/original removals/anvilkit-memory-removals/$key"
step removals.delete "mc rm removals/anvilkit-memory-removals/$key"
# snapshots: write without delete
step snapshots.put "mc cp /tmp/original snapshots/anvilkit-snapshots/$key"
step snapshots.delete "mc rm snapshots/anvilkit-snapshots/$key"
step snapshots.foreignBucket "mc cp /tmp/original snapshots/anvilkit-inventory/$key-x"
# backup (CloudNativePG): its delete of a fresh locked version is refused
step backup.put "mc cp /tmp/original backup/anvilkit-backups/$key"
b1=$(version "root/anvilkit-backups/$key")
step backup.deleteLockedVersion "mc rm --version-id '$b1' backup/anvilkit-backups/$key"
# prune: no read or write; a locked version within its retention stays
s1=$(version "root/anvilkit-snapshots/$key")
step prune.read "mc cat prune/anvilkit-snapshots/$key"
step prune.write "mc cp /tmp/original prune/anvilkit-snapshots/$key-p"
step prune.deleteLockedVersion "mc rm --version-id '$s1' prune/anvilkit-snapshots/$key"
"""
EXPECTED = {
    "inventory.put": "ok", "inventory.delete": "denied", "inventory.overwrite": "ok", "inventory.originalReadable": "ok",
    "inventory.deleteVersion": "denied",
    "removals.put": "ok", "removals.delete": "denied",
    "snapshots.put": "ok", "snapshots.delete": "denied", "snapshots.foreignBucket": "denied",
    "backup.put": "ok", "backup.deleteLockedVersion": "denied",
    "prune.read": "denied", "prune.write": "denied", "prune.deleteLockedVersion": "denied",
}


def parse(out: str) -> dict[str, str]:
    res = {}
    for line in out.splitlines():
        if line.startswith("RESULT "):
            _, name, verdict = (line.split(" ", 2) + [""])[:3]
            res[name] = verdict
    return res


def judge(results: dict[str, str]) -> dict:
    checks = {name: results.get(name) == want for name, want in EXPECTED.items()}
    return {"results": results, "checks": checks, "status": "PASS" if all(checks.values()) else "FAIL"}


def mc_probe(network: str, endpoint: str, root: tuple[str, str], users: dict[str, tuple[str, str]]) -> str:
    aliases = [f'mc alias set root {endpoint} "$ROOT_ID" "$ROOT_SECRET" >/dev/null']
    env = {"ROOT_ID": root[0], "ROOT_SECRET": root[1]}
    for u, (i, s) in users.items():
        aliases.append(f'mc alias set {u} {endpoint} "$U_{u.upper()}_ID" "$U_{u.upper()}_SECRET" >/dev/null')
        env |= {f"U_{u.upper()}_ID": i, f"U_{u.upper()}_SECRET": s}
    cmd = ["docker", "run", "--rm", "-i", "--network", network, "-e", "HOME=/tmp", "-e", "MC_CONFIG_DIR=/tmp/.mc"]
    for k in env:
        cmd += ["-e", k]
    p = subprocess.run(cmd + ["--entrypoint", "sh", MC], input="\n".join(aliases) + "\n" + PROBES, capture_output=True, text=True,
                       env={**__import__("os").environ, **env}, timeout=300)
    return p.stdout + p.stderr


def local(out_dir: pathlib.Path) -> int:
    """dr_script() and the probes against a disposable MinIO container."""
    run = lambda *a: subprocess.run(list(a), capture_output=True, text=True)  # noqa: E731
    if run("docker", "image", "inspect", MINIO).returncode != 0 or run("docker", "image", "inspect", MC).returncode != 0:
        print(f"UNEXECUTED: {MINIO} or {MC} is not in the local image cache")
        return 2
    tag = secrets.token_hex(4)
    net, name = f"p37-dr-{tag}", f"p37-dr-{tag}"
    root = ("p37root", secrets.token_hex(16))
    users = {u: (f"anvilkit-dr-{u}", secrets.token_hex(16)) for u in bootstrap.DR_USERS}
    try:
        run("docker", "network", "create", net)
        p = run("docker", "run", "-d", "--name", name, "--network", net, "-e", f"MINIO_ROOT_USER={root[0]}", "-e", f"MINIO_ROOT_PASSWORD={root[1]}",
                MINIO, "server", "/data")
        if p.returncode != 0:
            print("UNEXECUTED: the MinIO container did not start:", p.stderr.strip()[-200:])
            return 2
        endpoint = f"http://{name}:9000"
        script = [line.replace("http://anvilkit-qualification-dr:9000", endpoint) for line in bootstrap.dr_script()]
        env = {"MINIO_ROOT_USER": root[0], "MINIO_ROOT_PASSWORD": root[1]}
        env |= {f"DR_{u.upper()}_ID": v[0] for u, v in users.items()} | {f"DR_{u.upper()}_SECRET": v[1] for u, v in users.items()}
        cmd = ["docker", "run", "--rm", "-i", "--network", net, "-e", "HOME=/tmp", "-e", "MC_CONFIG_DIR=/tmp/.mc"]
        for k in env:
            cmd += ["-e", k]
        for _ in range(30):
            ready = subprocess.run(cmd + ["--entrypoint", "sh", MC], input=f'mc alias set dr {endpoint} "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc ready dr\n',
                                   capture_output=True, text=True, env={**__import__("os").environ, **env})
            if ready.returncode == 0:
                break
            time.sleep(1)
        setup = subprocess.run(cmd + ["--entrypoint", "sh", MC], input="\n".join(script) + "\n", capture_output=True, text=True,
                               env={**__import__("os").environ, **env})
        if setup.returncode != 0:
            print("FAIL: dr_script() did not apply:", setup.stderr.strip()[-400:])
            return 1
        raw = mc_probe(net, endpoint, root, users)
    finally:
        run("docker", "rm", "-f", name)
        run("docker", "network", "rm", net)
    record = judge(parse(raw)) | {"placement": "a disposable MinIO container (the foundation's image) with bootstrap.dr_script() applied",
                                  "setupWarnings": [l for l in setup.stderr.splitlines() if l.startswith("WARNING")]}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dr-local.json").write_text(json.dumps(record, indent=2) + "\n")
    for name_, ok in record["checks"].items():
        print(f"{'PASS' if ok else 'FAIL'}  {name_}: {record['results'].get(name_)} (expected {EXPECTED[name_]})")
    print(f"dr probes (local): {record['status']}; {out_dir / 'dr-local.json'}")
    return 0 if record["status"] == "PASS" else 1


def live(run_id: str) -> int:
    creds_file = bootstrap.STATE / "credentials.json"
    dr_env = bootstrap.STATE / "dr.env"
    if not creds_file.exists() or not dr_env.exists():
        print("UNEXECUTED: no qualification credentials (.local/qualification/credentials.json, dr.env)")
        return 2
    c = json.loads(creds_file.read_text())
    env = dict(line.split("=", 1) for line in dr_env.read_text().split())
    users = {u: (v["id"], v["secret"]) for u, v in c["dr"].items() if u in bootstrap.DR_USERS}
    if set(users) != set(bootstrap.DR_USERS):
        print(f"UNEXECUTED: the environment's DR users {sorted(users)} predate {sorted(bootstrap.DR_USERS)} (run bootstrap.py)")
        return 2
    raw = mc_probe("kind", "http://anvilkit-qualification-dr:9000", (env["MINIO_ROOT_USER"], env["MINIO_ROOT_PASSWORD"]), users)
    record = judge(parse(raw)) | {"run": run_id, "placement": "DEVELOPMENT_ONLY: the DR MinIO container on the qualification host"}
    out = ROOT / "outputs/qualification" / run_id / "dr"
    out.mkdir(parents=True, exist_ok=True)
    (out / "dr.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"checks": record["checks"], "status": record["status"]}, indent=1))
    return 0 if record["status"] == "PASS" else 1


def self_test() -> int:
    u, b = bootstrap.DR_USERS, bootstrap.DR_BUCKETS
    cases = [
        ("every user's buckets are locked DR buckets with a retention", all(x in b and b[x] > 0 for v in u.values() for x in v["buckets"])),
        ("the inventory and removal users are create-only", all("s3:DeleteObject" not in u[k]["objects"] and "s3:DeleteObjectVersion" not in u[k]["objects"]
                                                              and "s3:PutObject" in u[k]["objects"] for k in ("inventory", "removals"))),
        ("the snapshot writer cannot delete", not any("Delete" in a for a in u["snapshots"]["objects"])),
        ("the prune identity cannot read or write", set(u["prune"]["objects"]) <= {"s3:DeleteObject", "s3:DeleteObjectVersion"}),
        ("prune covers exactly the snapshot writer's buckets", set(u["prune"]["buckets"]) == set(u["snapshots"]["buckets"])),
        ("no wildcard action", not any(a.endswith("*") for v in u.values() for a in v["bucket"] + v["objects"])),
        ("each policy names only its own buckets", all(all(any(r.startswith(f"arn:aws:s3:::{x}") for x in u[k]["buckets"]) for st in bootstrap.dr_policy(k)["Statement"]
                                                          for r in st["Resource"]) for k in u)),
        ("every bucket is created with object lock and a default retention", all(any(f"--with-lock dr/{x}" in l for l in bootstrap.dr_script())
                                                                                and any(f"GOVERNANCE {d}d dr/{x}" in l for l in bootstrap.dr_script()) for x, d in b.items())),
        ("the judgement fails a denied create", judge({**EXPECTED, "inventory.put": "denied"})["status"] == "FAIL"),
        ("the judgement fails an allowed delete", judge({**EXPECTED, "inventory.delete": "ok"})["status"] == "FAIL"),
        ("the judgement passes the expected outcomes", judge(dict(EXPECTED))["status"] == "PASS"),
        ("a missing probe is a failure", judge({k: v for k, v in EXPECTED.items() if k != "prune.read"})["status"] == "FAIL"),
    ]
    for name, ok in cases:
        print(f"{'PASS' if ok else 'FAIL'}  self-test: {name}")
    failed = [n for n, ok in cases if not ok]
    print(f"check-dr self-test: {len(cases) - len(failed)}/{len(cases)} PASS")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--local", action="store_true")
    mode.add_argument("--run")
    ap.add_argument("--out", default=str(ROOT / "outputs/p3-verification" / "dr"))
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.local:
        return local(pathlib.Path(a.out))
    return live(a.run)


if __name__ == "__main__":
    sys.exit(main())
