#!/usr/bin/env python3
"""Build contracts/openapi/agent-api-v1.openapi.json from contracts/openapi/agent-api-v1.source.json (added 2026-09-10).

The source document is the authored Agent public API: paths, operations, security and small inline schemas,
with every shared shape referenced by its URN pointer (`urn:anvilkit:<schema>:v1#/$defs/<name>`, or the URN
alone for a schema whose root is the shape). This tool inlines each referenced definition as an OpenAPI
component named `<schemaShort>.<def>` carrying `x-anvilkit-source`, rewrites nested references, and writes a
self-contained OpenAPI 3.1 document. tools/check-contracts.py proves each mirrored component still equals its
source, so the JSON Schemas stay the single authority and the OpenAPI document is a checked projection.

Usage: python3 tools/build-agent-openapi.py [--check]
"""
from __future__ import annotations
import argparse, copy, json, pathlib, re, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "contracts/openapi/agent-api-v1.source.json"
OUT = ROOT / "contracts/openapi/agent-api-v1.openapi.json"

def load(p):
    def no_dup(pairs):
        d = {}
        for k, v in pairs:
            if k in d: raise ValueError(f"duplicate key {k!r} in {p}")
            d[k] = v
        return d
    return json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=no_dup)

SCHEMAS = {}
for p in sorted((ROOT / "contracts").rglob("*.schema.json")):
    d = load(p); SCHEMAS[d["$id"]] = d

def short(sid: str) -> str:
    name = sid.split(":")[2]
    return re.sub(r"-([a-z])", lambda m: m.group(1).upper(), name)

def comp_name(sid: str, ptr: str) -> str:
    return short(sid) if not ptr else f"{short(sid)}.{ptr.split('/')[-1]}"

def source_node(sid: str, ptr: str):
    node = SCHEMAS[sid]
    if not ptr:
        return {k: v for k, v in node.items() if k not in ("$schema", "$id", "$defs")}
    for part in ptr.strip("/").split("/"):
        node = node[part]
    return node

def build(src: dict) -> dict:
    components: dict[str, dict] = {}
    pending: list[tuple[str, str]] = []
    def want(sid, ptr):
        name = comp_name(sid, ptr)
        if name not in components and (sid, ptr) not in pending:
            pending.append((sid, ptr))
        return name
    def rewrite(node, base_id):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "$ref" and isinstance(v, str) and v.startswith("#/components/"):
                    out[k] = v; continue
                if k == "$ref" and isinstance(v, str):
                    target = base_id + v if v.startswith("#") else v
                    sid, _, ptr = target.partition("#")
                    if sid not in SCHEMAS: raise SystemExit(f"unknown schema {sid} referenced from {base_id}")
                    out[k] = f"#/components/schemas/{want(sid, ptr)}"
                else:
                    out[k] = rewrite(v, base_id)
            return out
        if isinstance(node, list): return [rewrite(x, base_id) for x in node]
        return node
    doc = copy.deepcopy(src)
    doc["paths"] = rewrite(doc["paths"], "urn:anvilkit:agent-api-source")
    doc.setdefault("components", {})
    doc["components"]["schemas"] = rewrite(doc["components"].get("schemas", {}), "urn:anvilkit:agent-api-source")
    while pending:
        sid, ptr = pending.pop(0)
        name = comp_name(sid, ptr)
        if name in components: continue
        node = copy.deepcopy(source_node(sid, ptr))
        if not ptr:
            for defname in SCHEMAS[sid].get("$defs", {}): want(sid, f"/$defs/{defname}")
        comp = rewrite(node, sid)
        comp["x-anvilkit-source"] = f"{sid}#{ptr}" if ptr else sid
        components[name] = comp
    for k in sorted(components): doc["components"]["schemas"][k] = components[k]
    return doc

def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--check", action="store_true"); a = ap.parse_args()
    out = json.dumps(build(load(SRC)), indent=2, ensure_ascii=False) + "\n"
    if a.check:
        cur = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        print("current" if cur == out else "stale"); return 0 if cur == out else 1
    OUT.write_text(out, encoding="utf-8"); print(f"wrote {OUT.relative_to(ROOT)}"); return 0
if __name__ == "__main__": sys.exit(main())
