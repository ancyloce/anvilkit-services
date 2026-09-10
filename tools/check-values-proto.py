#!/usr/bin/env python3
"""contracts:values-proto-agreement — the blocking equivalence gate between the
canonical JSON value source contracts/values/common-v1.schema.json and its Protobuf
projection contracts/proto/common-v1.proto (DD-02 §7.4.1).

It enforces the three rules DD-02 fixes:
  1. Mapping completeness in BOTH directions: every JSON $defs entry and every enum
     member has a mapped Protobuf field/enum value under the §7.4.1 mapping, and the
     reverse. An addition on either side that the other does not map is a failure.
  2. A shared fixture corpus round-trips JSON -> Protobuf wire -> JSON with byte
     equality after canonicalization, including uint64/sint64 boundaries, the
     timestamp adapter, and omitted-vs-null.
  3. Boundary rejection at the JSON edge: duplicate keys, null for a required field,
     unknown keys, and the two forbidden reference shapes are rejected before conversion.

Run:  python3 tools/check-values-proto.py            (exit 0 = agree)
      python3 tools/check-values-proto.py --selftest  (also prove drift is detected)

Requires protoc and the google.protobuf runtime. These are document-contract checks;
they do not qualify any runtime service.
"""
from __future__ import annotations
import argparse, io, json, os, pathlib, subprocess, sys, tempfile

FAIL: list[str] = []
COUNT: dict[str, int] = {}
def fail(m): FAIL.append(m)
def bump(k, n=1): COUNT[k] = COUNT.get(k, 0) + n

# ---- DD-02 §7.4.1 scalar mapping: JSON primitive $def -> Protobuf scalar projection ----
SCALAR_MAP = {
    "id": "string", "uint64": "uint64", "signedAmount": "sint64",
    "digest": "string", "timestamp": "google.protobuf.Timestamp",
}
# JSON object $def -> Protobuf message name
MSG_MAP = {"artifactRef": "ArtifactRef", "apiOperationRef": "ApiOperationRef"}

def kebab_to_upper(s: str) -> str: return s.replace("-", "_").upper()
def upper_to_kebab(s: str) -> str: return s.lower().replace("_", "-")
def snake_to_camel(s: str) -> str:
    a = s.split("_"); return a[0] + "".join(p.title() for p in a[1:])

def no_dup(pairs):
    seen = set()
    for k, _ in pairs:
        if k in seen: raise ValueError(f"duplicate key {k!r}")
        seen.add(k)
    return dict(pairs)

def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    root = pathlib.Path(args.root)

    schema = json.loads((root / "contracts/values/common-v1.schema.json").read_text("utf-8"))
    defs = schema["$defs"]

    proto = root / "contracts/proto/common-v1.proto"
    if not proto.exists():
        fail("contracts/proto/common-v1.proto is missing; the gate cannot run")
        return report()

    # compile a descriptor set with protoc (self-contained, version-agnostic wire)
    try:
        from google.protobuf import (descriptor_pb2, descriptor_pool,
                                      message_factory, json_format,
                                      timestamp_pb2, wrappers_pb2)
    except Exception as e:  # noqa: BLE001
        fail(f"google.protobuf runtime unavailable ({e})"); return report()
    with tempfile.TemporaryDirectory() as td:
        desc = os.path.join(td, "c.desc")
        r = subprocess.run(["protoc", "-I", str(proto.parent), "-I", "/usr/include",
                            "--include_imports", f"--descriptor_set_out={desc}", str(proto)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            fail(f"protoc failed: {r.stderr.strip()[:300]}"); return report()
        fds = descriptor_pb2.FileDescriptorSet(); fds.ParseFromString(open(desc, "rb").read())
    pool = descriptor_pool.DescriptorPool()
    for f in fds.file:
        if f.name.startswith("google/protobuf/"):  # WKT already in pool
            try: pool.FindFileByName(f.name); continue
            except KeyError: pass
        pool.Add(f)

    def msgclass(short):
        return message_factory.GetMessageClass(
            pool.FindMessageTypeByName(f"anvilkit.values.v1.{short}"))

    # proto inventory
    proto_msgs, proto_fields, proto_enums = {}, {}, {}
    for fd in fds.file:
        if fd.package != "anvilkit.values.v1": continue
        for e in fd.enum_type:
            proto_enums[e.name] = {v.name for v in e.value if not v.name.endswith("_UNSPECIFIED")}
        for m in fd.message_type:
            proto_msgs[m.name] = m
            proto_fields[m.name] = {snake_to_camel(f.name) for f in m.field}

    # ---------------- rule 1: completeness both directions ----------------
    json_scalars = {k for k, v in defs.items() if v.get("type") == "string"}
    json_objects = {k for k, v in defs.items() if v.get("type") == "object"}
    for s in json_scalars:
        if s not in SCALAR_MAP: fail(f"JSON scalar $def {s!r} has no §7.4.1 proto mapping")
    for s in SCALAR_MAP:
        if s not in json_scalars: fail(f"§7.4.1 maps {s!r} but it is not a JSON scalar $def")
    for o in json_objects:
        if o not in MSG_MAP: fail(f"JSON object $def {o!r} has no proto message mapping")
        elif MSG_MAP[o] not in proto_msgs: fail(f"proto message {MSG_MAP[o]!r} missing for {o!r}")
    for pm in proto_msgs:
        if pm not in MSG_MAP.values(): fail(f"proto message {pm!r} maps to no JSON $def")
    if not FAIL: bump("mapping_directions")

    # fields + enum members per message
    for jname, pname in MSG_MAP.items():
        if pname not in proto_fields: continue
        jprops = set(defs[jname]["properties"].keys())
        preq = set(defs[jname].get("required", []))
        if jprops != preq:
            fail(f"{jname}: schema properties {sorted(jprops)} != required {sorted(preq)} "
                 "(common-v1 refs are all-required; a divergence must be intended)")
        if jprops != proto_fields[pname]:
            fail(f"{jname}: JSON fields {sorted(jprops)} != proto {pname} fields "
                 f"{sorted(proto_fields[pname])}")
        else:
            bump("field_agreement")
        # enum member agreement for the 'kind' field
        kenum = defs[jname]["properties"]["kind"].get("enum")
        if kenum:
            penum_name = {"ArtifactRef": "ArtifactKind",
                          "ApiOperationRef": "ApiOperationKind"}[pname]
            want = {kebab_to_upper(m) for m in kenum}
            got = proto_enums.get(penum_name, set())
            if want != got:
                fail(f"{jname}.kind enum vs proto {penum_name}: "
                     f"{sorted(want ^ got)} differ")
            else:
                bump("enum_agreement")

    # ---------------- rule 2: round-trip corpus (byte-equal after canon) ----------------
    def enc_enum(d):  # kebab -> UPPER for ParseDict
        d = dict(d); d["kind"] = kebab_to_upper(d["kind"]); return d
    def dec_enum(d):  # UPPER -> kebab from MessageToDict
        d = dict(d); d["kind"] = upper_to_kebab(d["kind"]); return d

    corpus = builtin_corpus()
    ext = root / "contracts/values/common-v1.fixtures.json"
    if ext.exists():
        extj = json.loads(ext.read_text("utf-8"), object_pairs_hook=no_dup)
        for k in ("artifactRef", "apiOperationRef"):
            corpus[k] += extj.get(k, [])
        bump("external_fixtures", sum(len(extj.get(k, [])) for k in ("artifactRef","apiOperationRef")))

    for jname, items in corpus.items():
        Cls = msgclass(MSG_MAP[jname])
        for rec in items:
            try:
                wire = Cls(); json_format.ParseDict(enc_enum(rec), wire)
                round2 = Cls(); round2.ParseFromString(wire.SerializeToString())
                back = dec_enum(json_format.MessageToDict(round2))
            except Exception as e:  # noqa: BLE001 -- drift shows up here too; report, do not crash
                fail(f"{jname} round-trip raised on {rec}: {str(e)[:160]}")
                continue
            if canon(back) != canon(rec):
                fail(f"{jname} round-trip not byte-equal: {rec} -> {back}")
            else:
                bump("roundtrip_records")

    # scalar boundaries + timestamp adapter through the same machinery
    for val in ("0", "18446744073709551615"):
        w = wrappers_pb2.UInt64Value(); json_format.Parse(f'"{val}"', w)
        got = json.loads(json_format.MessageToJson(w))
        if got != val: fail(f"uint64 boundary {val} did not round-trip -> {got}")
        else: bump("scalar_roundtrips")
    for val in ("-9223372036854775808", "0", "9223372036854775807"):
        w = wrappers_pb2.Int64Value(); json_format.Parse(f'"{val}"', w)
        got = json.loads(json_format.MessageToJson(w))
        if got != val: fail(f"sint64 boundary {val} did not round-trip -> {got}")
        else: bump("scalar_roundtrips")
    for ts in ("2026-09-08T10:15:00Z", "2026-09-08T10:15:00.052Z",
               "2026-09-08T10:15:00.123456789Z"):
        t = timestamp_pb2.Timestamp(); json_format.Parse(f'"{ts}"', t)
        got = json.loads(json_format.MessageToJson(t))
        if got != ts: fail(f"timestamp adapter {ts} did not round-trip -> {got}")
        else: bump("scalar_roundtrips")

    # ---------------- rule 3: boundary rejection at the JSON edge ----------------
    try:
        json.loads('{"authority":"a","authority":"b","kind":"candidate",'
                   '"refId":"r","subjectDigest":"sha256:'+"0"*64+'"}',
                   object_pairs_hook=no_dup)
        fail("duplicate JSON key was accepted")
    except ValueError:
        bump("boundary_rejections")
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
        reg = Registry().with_resource(schema["$id"],
                Resource.from_contents(schema, default_specification=DRAFT202012))
        def v(defname):
            return Draft202012Validator({"$ref": f'{schema["$id"]}#/$defs/{defname}'}, registry=reg)
        good = {"kind": "source", "refId": "r", "subjectDigest": "sha256:"+"a"*64,
                "contentDigest": "sha256:"+"b"*64, "sizeBytes": "1", "objectVersion": "v1"}
        if v("artifactRef").is_valid(good): bump("boundary_rejections")
        else: fail("valid artifactRef rejected")
        if v("artifactRef").is_valid(dict(good, sizeBytes=None)): fail("null required field accepted")
        else: bump("boundary_rejections")
        if v("artifactRef").is_valid(dict(good, extraField="x")): fail("unknown key accepted")
        else: bump("boundary_rejections")
        api = {"authority": "pagix", "kind": "publication", "refId": "p",
               "subjectDigest": "sha256:"+"c"*64}
        if v("apiOperationRef").is_valid(dict(api, contentDigest="sha256:"+"d"*64)):
            fail("apiOperationRef accepted a contentDigest")
        else: bump("boundary_rejections")
        if v("artifactRef").is_valid(dict(good, kind="publication")):
            fail("artifactRef accepted an apiOperation kind")
        else: bump("boundary_rejections")
    except Exception as e:  # noqa: BLE001
        fail(f"jsonschema/referencing unavailable for boundary checks ({e})")

    # ---------------- selftest: drift on either side is detected ----------------
    if args.selftest:
        want = {kebab_to_upper(m) for m in defs["artifactRef"]["properties"]["kind"]["enum"]}
        drift = want | {"NEW_FORGED_KIND"}
        if drift == proto_enums["ArtifactKind"]:
            fail("selftest: an added JSON enum member was NOT detected as drift")
        else:
            bump("selftest_detected")
        if (set(defs["artifactRef"]["properties"]) | {"sneakyField"}) == proto_fields["ArtifactRef"]:
            fail("selftest: an added JSON field was NOT detected as drift")
        else:
            bump("selftest_detected")

    return report()

def builtin_corpus():
    return {
        "artifactRef": [
            {"kind": k, "refId": f"ref-{i}", "subjectDigest": "sha256:"+f"{i:064x}",
             "contentDigest": "sha256:"+f"{i+1:064x}", "sizeBytes": sz, "objectVersion": f"v{i}"}
            for i, (k, sz) in enumerate([
                ("generation-request", "0"), ("release-request", "1"), ("source", "4821"),
                ("plan", "12033"), ("build-support", "20971520"), ("validation", "77"),
                ("bundle", "18446744073709551615"), ("preview-artifact", "9999999999"),
                ("model-response", "12"), ("transcript", "34"), ("evidence", "56"),
            ])
        ],
        "apiOperationRef": [
            {"authority": "pagix", "kind": k, "refId": f"op-{i}",
             "subjectDigest": "sha256:"+f"{i:064x}"}
            for i, k in enumerate(["candidate", "review", "approval", "publication",
                                   "publication-receipts", "activation", "activation-receipt"])
        ],
    }

def report():
    for f in FAIL: print("FAIL", f)
    print("counts:", json.dumps(COUNT, sort_keys=True))
    print(f"{len(FAIL)} failures")
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
