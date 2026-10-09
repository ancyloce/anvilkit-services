#!/usr/bin/env python3
"""P24b: the handover and retirement procedure of the legacy anvilkit-local scope.

  PY=.local/verification-venv/bin/python
  $PY deploy/handover/handover.py plan     --run RUN
  $PY deploy/handover/handover.py rehearse --run RUN
  $PY deploy/handover/handover.py execute  --run RUN --scope anvilkit-local --authorized-by WHO --authorization REF

RUN is the rehearsal run of deploy/handover/rehearse.py (outputs/handover/<RUN>/: its
inputs, legacy inventory and handover-rehearsal record); the procedure reads the explicit
retirement inventory deploy/handover/retirement.yaml.

The procedure, in order (delivery.md, P24 Work/Exit/Recovery):
  1. gates       P23 qualified (its newest record), P24a rehearsed (handover-rehearsal.json),
                 the agreed scope, and an explicit execution authorization for execute.
  2. close-old   the old scope admits nothing: its API is down and none of its containers
                 can restart (execute: restart policy no).
  3. capture     the final deltas: every legacy database exported from a disposable copy of
                 its volume (pg_dump, never from the scope itself), the intake and artifact
                 volumes archived; each restored into a second disposable server and every
                 table's row count and digest compared with the legacy inventory (rehearse:
                 the export is removed after verification; execute: it is kept under
                 .local/legacy/data-<RUN>/, 0700).
  4. reconcile   the final reconciliation of old obligations on the captured state: every
                 operation terminal, no open business execution, no cost/effect/send/permit/
                 publication/receipt, no page lock (the inventory's assessment rules).
  5. transfer    one entry, clients and write authority: the new API answers and accepts a
                 probe, the Studio holds no legacy client, nothing new names the old scope.
  6. retire      the retirement inventory's retire items (execute only; rehearse lists each
                 target, verifies it exists as recorded and prints the exact action).
  7. final       one active implementation (no legacy container running, legacy ports free),
                 no duplicate operation per command in the new Control, no legacy effect, no
                 silent page upgrade (every Studio page byte-identical to the frozen inputs).

Exit 0 when every step that ran passed, 1 when a check failed, 2 when a gate refuses
(execute) or an input is missing. Nothing the old scope holds is written by plan or
rehearse; execute is a release action and never runs without its gates.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("rehearse", ROOT / "deploy/handover/rehearse.py")
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)  # the shared helpers: evidence, docker, psql, the installed environment
yaml = rh.yaml

INVENTORY = ROOT / "deploy/handover/retirement.yaml"
LEGACY_PORTS = [18080, 18081, 18082, 15432, 17233]


def load(run_id: str, name: str) -> dict | None:
    p = rh.OUT / run_id / name
    return json.loads(p.read_text()) if p.exists() else None


# ---------------------------------------------------------------- 1. gates

def gates(a: argparse.Namespace) -> dict:
    records = sorted(rh.QUAL.glob("*/qualification-record.json"), key=lambda p: p.parent.name)
    p23 = json.loads(records[-1].read_text()) if records else {}
    rehearsal = load(a.run, "handover-rehearsal.json") or {}
    inv = yaml.safe_load(INVENTORY.read_text())
    g = {
        "p23Qualified": {"ok": str(p23.get("status", "")).startswith("QUALIFIED"), "record": rh.rel(records[-1]) if records else None,
                         "status": p23.get("status")},
        "p24aRehearsed": {"ok": str(rehearsal.get("p24a", {}).get("status", "")).startswith("REHEARSED"),
                          "status": rehearsal.get("p24a", {}).get("status")},
        "agreedScope": {"ok": getattr(a, "scope", None) == inv["scope"]["name"], "scope": inv["scope"]["name"]},
        "executionAuthorization": {"ok": bool(getattr(a, "authorized_by", None) and getattr(a, "authorization", None)),
                                   "authorizedBy": getattr(a, "authorized_by", None), "reference": getattr(a, "authorization", None)},
    }
    return g


# ---------------------------------------------------------------- 2. close-old

def close_old(execute: bool) -> dict:
    env = rh.legacy_environment()
    running = [c["name"] for c in env["containers"] if c["state"] == "running"]
    if execute:
        for c in env["containers"]:
            rh.run(["docker", "update", "--restart=no", c["name"]])
        env = rh.legacy_environment()
    listening = [p for p in LEGACY_PORTS if port_open(p)]
    return {"containers": [{"name": c["name"], "state": c["state"], "restartPolicy": c["restartPolicy"]} for c in env["containers"]],
            "running": running, "listeningPorts": listening,
            "admissionClosed": not running and not listening,
            "restartPrevented": all(c["restartPolicy"] == "no" for c in env["containers"]),
            "note": None if execute else "rehearsal: restart policies left as they are (execute sets them to no)"}


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ---------------------------------------------------------------- 3. capture

def capture(a: argparse.Namespace, execute: bool) -> dict:
    inventory = load(a.run, "legacy-inventory.json")
    if not inventory:
        return {"result": "UNEXECUTED", "reason": "legacy-inventory.json is missing (rehearse.py legacy)"}
    tag = a.run.lower()
    src, copy, restored = f"anvilkit-handover-capture-{tag}", f"anvilkit-handover-capture-{tag}-pg", f"anvilkit-handover-restore-{tag}"
    dest = (ROOT / ".local/legacy" / f"data-{a.run}") if execute else pathlib.Path(tempfile.mkdtemp(prefix="anvilkit-handover-capture-"))
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    hba = pathlib.Path(tempfile.mkdtemp(prefix="anvilkit-handover-hba-"))
    os.chmod(hba, 0o755)
    (hba / "pg_hba.conf").write_text("local all all trust\n")
    os.chmod(hba / "pg_hba.conf", 0o644)
    doc: dict = {"destination": rh.rel(dest) if execute else "temporary (removed after verification)", "databases": {}, "volumes": {}}
    label = ["--label", f"anvilkit.io/handover-rehearsal={a.run}"]
    try:
        rh.run(["docker", "volume", "create", *label, copy])
        rh.run(["docker", "run", "--rm", "--network", "none", "-v", f"{rh.LEGACY_VOLUMES['postgres']}:/from:ro", "-v", f"{copy}:/to",
                rh.BUSYBOX, "cp", "-a", "/from/.", "/to/"])
        rh.run(["docker", "run", "-d", "--name", src, "--network", "none", *label, "-v", f"{copy}:/var/lib/postgresql", "-v", f"{hba}:/hba:ro",
                rh.LEGACY_PG_IMAGE, "postgres", "-c", "listen_addresses=", "-c", "hba_file=/hba/pg_hba.conf", "-c", "default_transaction_read_only=on"])
        rh.run(["docker", "run", "-d", "--name", restored, "--network", "none", *label, "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                rh.LEGACY_PG_IMAGE])
        for c in (src, restored):
            for _ in range(120):
                if rh.run(["docker", "exec", c, "pg_isready", "-U", "postgres"], check=False).returncode == 0 and \
                        rh.run(["docker", "exec", c, "psql", "-U", "postgres", "-Atc", "SELECT 1"], check=False).returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f"{c} did not become ready")
        for db in inventory["databases"]:
            dump = dest / f"{db}.dump"
            with dump.open("wb") as f:
                p = subprocess.run(["docker", "exec", src, "pg_dump", "-U", "postgres", "-Fc", "--no-owner", "--no-privileges", "-d", db],
                                   stdout=f, stderr=subprocess.PIPE)
            if p.returncode != 0:
                raise RuntimeError(f"pg_dump {db}: {p.stderr.decode()[-300:]}")
            os.chmod(dump, 0o600)
            rh.run(["docker", "exec", restored, "createdb", "-U", "postgres", db])
            with dump.open("rb") as f:
                r = subprocess.run(["docker", "exec", "-i", restored, "pg_restore", "-U", "postgres", "--no-owner", "--no-privileges", "-d", db],
                                   stdin=f, capture_output=True)
            mismatched = []
            for table, want in inventory["databases"][db]["tables"].items():
                schema, name = table.split(".", 1)
                q = f"{rh.ident(schema)}.{rh.ident(name)}"
                count, digest = rh.psql(restored, db, f"SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) FROM {q} t;")[0]
                if int(count) != want["rows"] or digest != want["md5"]:
                    mismatched.append({"table": table, "inventory": [want["rows"], want["md5"]], "restored": [int(count), digest]})
            doc["databases"][db] = {"dumpSha256": rh.sha(dump), "bytes": dump.stat().st_size, "tables": len(inventory["databases"][db]["tables"]),
                                    "restoreExit": r.returncode,
                                    # pg_restore's own error lines name objects (roles, extensions), never row content.
                                    "restoreErrors": [l for l in r.stderr.decode(errors="replace").splitlines() if "error:" in l][:10],
                                    "mismatched": mismatched}
        for key, vol in rh.LEGACY_VOLUMES.items():
            if key == "postgres":
                continue
            archive = dest / f"{key}.tar"
            with archive.open("wb") as f:
                p = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{vol}:/v:ro", rh.BUSYBOX, "tar", "-C", "/v", "-cf", "-", "."],
                                   stdout=f, stderr=subprocess.PIPE)
            if p.returncode != 0:
                raise RuntimeError(f"archive {vol}: {p.stderr.decode()[-300:]}")
            os.chmod(archive, 0o600)
            import hashlib
            import tarfile
            got = {}
            with tarfile.open(archive) as t:
                for m in t.getmembers():
                    if m.isfile():
                        got[m.name.removeprefix("./")] = "sha256:" + hashlib.sha256(t.extractfile(m).read()).hexdigest()
            want = {f["path"]: f["sha256"] for f in inventory["volumes"][key]["files"]}
            doc["volumes"][key] = {"archiveSha256": rh.sha(archive), "files": len(got), "matchesInventory": got == want}
    finally:
        for c in (src, restored):
            rh.run(["docker", "rm", "-f", c], check=False)
        rh.run(["docker", "volume", "rm", copy], check=False)
        shutil.rmtree(hba, ignore_errors=True)
        if not execute:
            shutil.rmtree(dest, ignore_errors=True)
    # The legacy login roles are not exported (their credentials are retired, never carried):
    # a restore error that only names a missing role is expected; any other fails the capture.
    import re
    for d in doc["databases"].values():
        d["unexpectedRestoreErrors"] = [e for e in d["restoreErrors"] if not re.search(r'role "[^"]+" does not exist', e)]
    doc["rolesNotExported"] = "the legacy login roles and their passwords are not part of the capture"
    doc["verified"] = (all(not d["mismatched"] and not d["unexpectedRestoreErrors"] for d in doc["databases"].values())
                       and all(v["matchesInventory"] for v in doc["volumes"].values()))
    return doc


# ---------------------------------------------------------------- 4. reconcile

def reconcile(a: argparse.Namespace) -> dict:
    inventory = load(a.run, "legacy-inventory.json") or {}
    a_ = inventory.get("assessment") or {}
    return {"rules": a_.get("rules"), "openBusinessExecutions": a_.get("openBusinessExecutions"),
            "unresolvedObligations": not all((a_.get("rules") or {"x": False}).values()),
            "decision": a_.get("decision"), "ownerConfirmation": a_.get("ownerConfirmation")}


# ---------------------------------------------------------------- 5. transfer

def transfer(a: argparse.Namespace) -> dict:
    out: dict = {}
    if not (rh.QSTATE / "kubeconfig").exists():
        return {"result": "UNEXECUTED", "reason": "no installed environment (rehearse.py install)"}
    rh.api_service()
    try:
        out["newEntryReady"] = rh.wait_api(120)
        body = json.dumps({"commandId": f"p24-transfer-{a.run.lower()}", "kind": "local_check",
                           "subject": {"profileId": "local-check-v1", "subjectDigest": rh.PROBE_SUBJECT}}).encode()
        status, _, parsed = rh.call(rh.credentials()["principals"]["probe"], "POST", "/api/v1/operations", body)
        out["newEntryAcceptsProbe"] = status in (200, 202) and bool((parsed or {}).get("operationId"))
    finally:
        rh.api_service_down()
    studio = rh.STUDIO / "apps/studio"
    legacy_refs = []
    for p in (studio / "lib").rglob("*.ts"):
        if "node_modules" in p.parts:
            continue
        text = p.read_text(errors="replace")
        for marker in ("127.0.0.1:18080", "/v1/local-checks", "/v1/operations/preparations", "caller-a.headers"):
            if marker in text:
                legacy_refs.append(f"{p.relative_to(rh.STUDIO)}: {marker}")
    out["studioLegacyClientReferences"] = legacy_refs
    check = load(a.run, "install-check.json") or {}
    out["newConfigurationNamesOldScope"] = (check.get("configurationNamingLegacy") or {}).get("matches")
    out["ok"] = bool(out["newEntryReady"] and out["newEntryAcceptsProbe"] and not legacy_refs and out["newConfigurationNamesOldScope"] == [])
    return out


# ---------------------------------------------------------------- 6. retire

def retire_plan() -> list[dict]:
    inv = yaml.safe_load(INVENTORY.read_text())
    env = rh.legacy_environment()
    present = {
        "legacy-containers": sorted(c["name"] for c in env["containers"]),
        "legacy-images": sorted(i["reference"] for i in env["images"]),
        "legacy-volumes": sorted(v["name"] for v in env["volumes"]),
        "legacy-credentials": [str((ROOT / ".local/compose").exists())],
    }
    rows = []
    for item in inv["items"]:
        row = {"id": item["id"], "decision": item["decision"], "action": " ".join(str(item["action"]).split()), "authorization": item["authorization"]}
        ident = item["identity"]
        if item["id"] == "legacy-containers":
            row["exists"] = sorted(ident["names"]) == present["legacy-containers"]
        elif item["id"] == "legacy-images":
            row["exists"] = all(x["reference"] in present["legacy-images"] for x in ident)
        elif item["id"] == "legacy-volumes":
            row["exists"] = sorted(ident) == present["legacy-volumes"]
        elif isinstance(ident, dict) and "path" in ident:
            row["exists"] = (ROOT / ident["path"]).exists()
        elif item["id"] == "legacy-submodule-arrangement":
            # Each submodule's recorded gitlink against the preserved legacy HEADs: an
            # arrangement is legacy only while it still records a legacy commit.
            legacy_heads = {(d / "HEAD").read_text().strip() for d in (ROOT / rh.LEGACY_SNAPSHOTS).glob("*") if (d / "HEAD").exists()}
            links = {}
            for path in ident:
                out = rh.run(["git", "ls-tree", "HEAD", path], cwd=ROOT).stdout.split()
                links[path] = out[2] if len(out) >= 3 else None
            row["gitlinks"] = links
            row["legacyGitlinks"] = sorted(pth for pth, c in links.items() if c in legacy_heads)
            row["exists"] = bool(row["legacyGitlinks"]) if item["decision"] == "retire" else not row["legacyGitlinks"]
        else:
            row["exists"] = None  # an arrangement or authority, checked by the steps above
        rows.append(row)
    return rows


def retire(execute: bool, a: argparse.Namespace) -> dict:
    rows = retire_plan()
    if not execute:
        return {"executed": False, "targets": rows, "allRecordedTargetsPresent": all(r["exists"] is not False for r in rows)}
    stamp = a.run
    images_dir = ROOT / ".local/legacy" / f"images-{stamp}"
    images_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(images_dir, 0o700)
    env = rh.legacy_environment()
    done = []
    for img in env["images"]:
        if img["reference"].endswith(":latest"):
            continue
        out = images_dir / (img["reference"].replace("/", "_").replace(":", "_") + ".tar")
        rh.run(["docker", "save", "-o", str(out), img["reference"]])
        done.append(f"saved {img['reference']} -> {rh.rel(out)} {rh.sha(out)}")
    for c in env["containers"]:
        rh.run(["docker", "rm", c["name"]])
        done.append(f"removed container {c['name']}")
    for img in env["images"]:
        rh.run(["docker", "rmi", img["reference"]])
        done.append(f"removed image tag {img['reference']}")
    evidence = ROOT / ".local/legacy" / f"evidence-{stamp}"
    evidence.mkdir(parents=True, exist_ok=True)
    if (ROOT / ".local/compose/acceptance-results.json").exists():
        shutil.copy2(ROOT / ".local/compose/acceptance-results.json", evidence / "acceptance-results.json")
    shutil.rmtree(ROOT / ".local/compose")
    done.append("destroyed .local/compose (keys, passwords, caller token); kept acceptance-results.json")
    shutil.copytree(ROOT / "deploy/local", ROOT / ".local/legacy" / f"deploy-local-{stamp}")
    for path in ("deploy/local", "compose.yaml", "tools/prepare-local-compose.py", "tools/component/hero-fixture.mjs"):
        p = ROOT / path
        shutil.rmtree(p) if p.is_dir() else p.unlink()
        done.append(f"deleted {path} from the working tree (the user commits the removal)")
    return {"executed": True, "actions": done}


# ---------------------------------------------------------------- 7. final

def final(a: argparse.Namespace) -> dict:
    env = rh.legacy_environment()
    inputs = load(a.run, "inputs.json") or {}
    frozen = {p["id"]: p["sha256"] for p in inputs.get("studioPages", {}).get("rows", [])}
    pages = rh.studio_pages()
    now_pages = {p["id"]: p["sha256"] for p in pages.get("rows", [])}
    out = {"legacyRunning": [c["name"] for c in env["containers"] if c["state"] == "running"],
           "legacyPortsListening": [p for p in LEGACY_PORTS if port_open(p)],
           "studioPagesUnchanged": frozen == now_pages,
           "studioPagesWithLocks": pages.get("pagesWithRemoteComponentLock")}
    if (rh.QSTATE / "kubeconfig").exists():
        dup = rh.control_sql("SELECT tenant_id, command_id, count(*) FROM operations GROUP BY 1, 2 HAVING count(*) > 1;")
        out["newDuplicateOperationsPerCommand"] = [r[:2] for r in dup]
        out["newOperationsByTenant"] = rh.operations_by_tenant()
    legacy = (load(a.run, "legacy-inventory.json") or {}).get("assessment", {}).get("rules", {})
    out["legacyEffectsCostsOrReceipts"] = not legacy.get("noCostEffectSendPermitPublicationOrReceipt", False)
    out["ok"] = (not out["legacyRunning"] and not out["legacyPortsListening"] and out["studioPagesUnchanged"]
                 and not out.get("newDuplicateOperationsPerCommand") and not out["legacyEffectsCostsOrReceipts"])
    return out


# ---------------------------------------------------------------- modes

def procedure(a: argparse.Namespace, mode: str) -> int:
    g = gates(a)
    doc = {"schemaVersion": 1, "mode": mode, "run": a.run, "recordedAt": rh.now(), "inventory": {"path": rh.rel(INVENTORY), "sha256": rh.sha(INVENTORY)},
           "gates": g}
    blockers = [k for k, v in g.items() if not v["ok"]]
    if mode == "execute" and blockers:
        doc["result"] = "REFUSED"
        doc["refusedBy"] = blockers
        rh.write(a.run, "handover-execute.json", doc)
        print("refused: P24b runs only after a successful P23 and with explicit authorization; unmet: " + ", ".join(blockers))
        for k in blockers:
            print(f"  {k}: {json.dumps(g[k])}")
        return 2
    execute = mode == "execute"
    steps = {}
    steps["closeOld"] = close_old(execute)
    if mode in ("rehearse", "execute"):
        steps["capture"] = capture(a, execute)
        steps["reconcile"] = reconcile(a)
        steps["transfer"] = transfer(a)
    steps["retire"] = retire(execute, a)
    if mode in ("rehearse", "execute"):
        steps["final"] = final(a)
    doc["steps"] = steps
    checks = {"admissionClosed": steps["closeOld"]["admissionClosed"], "retireTargetsAsRecorded": steps["retire"].get("allRecordedTargetsPresent", True)}
    if mode in ("rehearse", "execute"):
        checks |= {"captureVerified": steps["capture"].get("verified") is True,
                   "noUnresolvedOldObligation": steps["reconcile"]["unresolvedObligations"] is False,
                   "transferReady": steps["transfer"].get("ok") is True,
                   "finalChecks": steps["final"]["ok"]}
    doc["checks"] = checks
    doc["gatesForExecute"] = {"unmet": blockers}
    doc["result"] = "PASS" if all(checks.values()) else "FAIL"
    name = {"plan": "handover-plan.json", "rehearse": "handover-procedure-rehearsal.json", "execute": "handover-execute.json"}[mode]
    out = rh.write(a.run, name, doc)
    print(f"{mode} {doc['result']}: {json.dumps(checks)}")
    print(f"gates for execute: {'all met' if not blockers else 'unmet ' + ', '.join(blockers)}")
    for r in steps["retire"].get("targets", []):
        print(f"  {r['decision']:<8} {r['id']:<30} exists={r['exists']}  {r['action'][:110]}")
    print(f"written {rh.rel(out)}")
    return 0 if doc["result"] == "PASS" else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    for mode in ("plan", "rehearse", "execute"):
        p = sub.add_parser(mode)
        p.add_argument("--run", required=True)
        if mode == "execute":
            p.add_argument("--scope", required=True)
            p.add_argument("--authorized-by", required=True)
            p.add_argument("--authorization", required=True, help="the reference of the explicit execution authorization")
    a = ap.parse_args()
    if not shutil.which("docker"):
        print("UNEXECUTED: docker is required")
        return 2
    return procedure(a, a.mode)


if __name__ == "__main__":
    sys.exit(main())
