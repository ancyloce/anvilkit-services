#!/usr/bin/env python3
"""Refresh contracts/definitions/p0-local-bundle-v1.json after contract changes (added 2026-09-10).

The retained bundle binds every file under contracts/ (except itself) to its exact SHA-256 so that
tools/check-ddd-contracts.py can prove byte closure. Adding or revising a contract therefore requires
regenerating the bundle. This tool keeps existing owner/role annotations, adds new files with an
owner derived from their directory, drops entries whose file no longer exists, recomputes digests
and stamps a new bundle id. It grants nothing: status stays draft, ownerReview stays pending and
enabledEffectPermissions stays empty.

Usage: python3 tools/refresh-p0-bundle.py [--check]   (--check: exit 1 if the bundle is stale)
"""
from __future__ import annotations
import argparse, datetime, hashlib, json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "contracts/definitions/p0-local-bundle-v1.json"
OWNER_BY_DIR = {"values": "DD-02", "events": "DD-02", "proto": "DD-02", "sql": "DD-02", "profiles": "DD-02", "openapi": "DD-02",
                "actions": "DD-01", "definitions": "DD-01", "jobs": "DD-03", "model-proxy": "DD-03", "sidecar": "DD-03",
                "components": "DD-04", "preview": "DD-04", "catalog": "DD-05", "telemetry": "operations", "qualification": "operations"}
def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--check", action="store_true"); a = ap.parse_args()
    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    existing = {e["path"]: e for e in bundle["artifacts"]}
    files = sorted(p for p in (ROOT / "contracts").rglob("*") if p.is_file() and p.name != BUNDLE.name)
    out = []
    for p in files:
        relp = str(p.relative_to(ROOT)); digest = hashlib.sha256(p.read_bytes()).hexdigest()
        e = existing.get(relp)
        if e is None:
            top = relp.split("/")[1]
            role = "external-evidence-read-only" if p.name == "pagix-cloud-open-api.json" else "draft-contract-or-fixture"
            e = {"path": relp, "sha256": digest, "owner": OWNER_BY_DIR.get(top, "DD-02"), "role": role}
        out.append({"path": relp, "sha256": digest, "owner": e["owner"], "role": e["role"]})
    stale = out != bundle["artifacts"]
    if a.check:
        print("stale" if stale else "current"); return 1 if stale else 0
    if stale:
        bundle["artifacts"] = out
        bundle["id"] = "ddd-p0-local-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bundle["status"] = "draft"; bundle["ownerReview"] = "pending"; bundle["enabledEffectPermissions"] = []
        BUNDLE.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"refreshed {len(out)} artifacts as {bundle['id']}")
    else: print("bundle current")
    return 0
if __name__ == "__main__": sys.exit(main())
