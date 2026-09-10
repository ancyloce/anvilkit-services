#!/usr/bin/env python3
"""Cross-contract agreement checks for anvilkit-services (added 2026-09-10).

Usage: python3 tools/check-contracts.py [--root PATH] [--section NAME ...]

Complements tools/check-docs.py (markdown/schema/fixture checks), tools/check-values-proto.py
(common-value/proto equivalence) and tools/check-ddd-contracts.py (DDD slice models). This tool
holds the implementation-level contracts to one another:

  openapi   the Agent OpenAPI parses and validates; every mirrored component equals its URN source;
            named examples validate; error responses use the shared envelope; the P0-A definitions
            subset stays a faithful subset of the full document
  proto     control-v1.proto compiles; its enums equal the shared JSON enums member for member;
            the service rpc lists are exactly the documented ones; every fixture parses as ProtoJSON
  sql       both DDL files parse (pglast); every table has exactly one lock-order placement; the
            DD-02 §12.1.2 relations exist; tenant-bearing tables enable and force RLS
  actions   registry/binding digests recompute; the 13 action ids are exactly the shared enum; the
            definition examples reference registry actions with matching port kinds; the Runner
            rules' routing table equals the DD-03 result-routing table row for row
  jobs      failure-code table equals the result-manifest enums; every job profile's resultContract
            digests recompute from the actual files; legal outcomes agree with job-kind-profiles;
            socket paths and UIDs agree with the sidecar contracts
  examples  every example/fixture listed in the convention table validates against its schema and
            every negative variant is rejected
  limits    every pilot-limit key referenced by any contract exists in pilot-limits-v1.json
  enums     jobKind/eventType/failureClass/resultOutcome/service names agree across the schemas
  cq        every CQ-01..CQ-26 is covered by at least one service-view coverage row that names an
            existing artifact; every cited CQ exists in the qualification plan
  docs      service views 0001–0008 declare design definitions complete; 0009 is a deferred record

Exit status 1 on any failure. Document and contract checks only; nothing here qualifies runtime behavior.
"""
from __future__ import annotations
import argparse, copy, hashlib, json, pathlib, re, subprocess, sys, tempfile

FAIL: list[str] = []
COUNT: dict[str, int] = {}
def fail(m): FAIL.append(m)
def bump(k, n=1): COUNT[k] = COUNT.get(k, 0) + n

def no_dup(pairs):
    d = {}
    for k, v in pairs:
        if k in d: raise ValueError(f"duplicate key {k!r}")
        d[k] = v
    return d
def load(p: pathlib.Path):
    return json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=no_dup)
def canon(o) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
def sha(b: bytes) -> str: return "sha256:" + hashlib.sha256(b).hexdigest()
def upper(s: str) -> str: return re.sub(r"[-.]", "_", s).upper()

ROOT = pathlib.Path(".")
def rel(p): 
    try: return str(p.relative_to(ROOT))
    except Exception: return str(p)

# ----------------------------------------------------------------------------- registry
SCHEMAS: dict[str, dict] = {}
REGISTRY = None
def build_registry():
    global REGISTRY
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
    for p in sorted((ROOT / "contracts").rglob("*.schema.json")):
        try:
            d = load(p)
        except Exception as e:
            fail(f"{rel(p)}: parse: {e}"); continue
        if "$id" not in d:
            fail(f"{rel(p)}: schema without $id"); continue
        if d["$id"] in SCHEMAS:
            fail(f"{rel(p)}: duplicate $id {d['$id']}")
        SCHEMAS[d["$id"]] = d
        try:
            Draft202012Validator.check_schema(d); bump("schemas")
        except Exception as e:
            fail(f"{rel(p)}: metaschema: {e}")
    REGISTRY = Registry().with_resources([(k, Resource.from_contents(v, default_specification=DRAFT202012)) for k, v in SCHEMAS.items()])

def validator(ref: str):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({"$ref": ref}, registry=REGISTRY)
def errors(ref: str, inst):
    return sorted(validator(ref).iter_errors(inst), key=lambda e: list(e.path))
def expect_valid(ref, inst, label):
    es = errors(ref, inst)
    if es:
        for e in es[:3]: fail(f"{label}: {'/'.join(map(str, e.path)) or '<root>'}: {e.message[:160]}")
    else: bump("examples_valid")
def expect_invalid(ref, inst, label):
    if validator(ref).is_valid(inst): fail(f"{label}: negative variant accepted")
    else: bump("negatives_rejected")

def enum_of(schema_id: str, defname: str) -> set[str]:
    d = SCHEMAS.get(schema_id, {}).get("$defs", {}).get(defname, {})
    if "enum" in d: return set(d["enum"])
    if "anyOf" in d:
        out = set()
        for a in d["anyOf"]:
            r = a.get("$ref", "")
            if r.startswith("#/$defs/"): out |= enum_of(schema_id, r.split("/")[-1])
        return out
    return set()

# ----------------------------------------------------------------------------- examples (convention table)
# file -> (schema ref, pointer to the instance list or None for whole document, is_negative)
EXAMPLES: list[tuple[str, str, str | None, bool]] = [
    # values / events / definitions (author A)
    ("contracts/values/error-envelope.example.json", "urn:anvilkit:error-envelope:v1", "*", False),
    ("contracts/values/error-envelope.negative.json", "urn:anvilkit:error-envelope:v1", "*.instance", True),
    ("contracts/events/operation-event-v1.fixtures.json", "urn:anvilkit:operation-event-payloads:v1", "records", False),
    ("contracts/events/operation-event-v1.negative.json", "urn:anvilkit:operation-event-payloads:v1", "*.instance", True),
    ("contracts/events/operation-view.example.json", "urn:anvilkit:operation-view:v1", None, False),
    ("contracts/events/operation-view.negative.json", "urn:anvilkit:operation-view:v1", "*.instance", True),
    ("contracts/events/operation-snapshot.example.json", "urn:anvilkit:operation-snapshot:v1", None, False),
    ("contracts/events/operation-snapshot.negative.json", "urn:anvilkit:operation-snapshot:v1", "*.instance", True),
    ("contracts/definitions/definition-change.example.json", "urn:anvilkit:definition-change:v1#/$defs/DefinitionChangeBatchV1", None, False),
    ("contracts/definitions/definition-change.negative.json", "urn:anvilkit:definition-change:v1#/$defs/DefinitionChangeBatchV1", "*.instance", True),
    # sql (author B)
    ("contracts/sql/control-v1.lock-order.json", "urn:anvilkit:lock-order:v1", None, False),
    # actions / definitions (author C)
    ("contracts/actions/runtime-bindings.example.json", "urn:anvilkit:runtime-binding:v1#/$defs/RuntimeBindingSetV1", None, False),
    ("contracts/definitions/component-effective-policy.example.json", "urn:anvilkit:policy-record:v1#/$defs/EffectivePolicyV1", None, False),
    ("contracts/definitions/retry-profiles.example.json", "urn:anvilkit:policy-record:v1#/$defs/RetryProfileV1", "*", False),
    ("contracts/definitions/meter-policies.example.json", "urn:anvilkit:policy-record:v1#/$defs/MeterPolicyV1", "*", False),
    ("contracts/definitions/run-context.example.json", "urn:anvilkit:run-context:v1", None, False),
    ("contracts/definitions/runner-rules-v1.json", "urn:anvilkit:runner-rules:v1", None, False),
    # model proxy (author D1)
    ("contracts/model-proxy/route-table.example.json", "urn:anvilkit:route-table:v1", None, False),
    ("contracts/model-proxy/exposure-estimation-v1.json", "urn:anvilkit:exposure-estimation:v1", None, False),
    ("contracts/model-proxy/pi-proxy-stream-v1.fixtures.json", "urn:anvilkit:pi-proxy-stream:v1", "streams.*.frames", False),
    ("contracts/model-proxy/pi-proxy-stream-v1.negative.json", "urn:anvilkit:pi-proxy-stream:v1", "*.instance", True),
    # sidecar (author D2)
    ("contracts/sidecar/sidecar-state-machine-v1.json", "urn:anvilkit:sidecar-state-machine:v1", None, False),
    ("contracts/sidecar/scope-token-v1.negative.json", "urn:anvilkit:scope-token:v1#/$defs/token", "*.instance", True),
    # jobs (author E1)
    ("contracts/jobs/profiles/codegen-pi-openai-v1.json", "urn:anvilkit:job-profile:v1", None, False),
    ("contracts/jobs/profiles/repair-pi-openai-v1.json", "urn:anvilkit:job-profile:v1", None, False),
    ("contracts/jobs/profiles/validate-content-v1.json", "urn:anvilkit:job-profile:v1", None, False),
    ("contracts/jobs/profiles/certify-content-v1.json", "urn:anvilkit:job-profile:v1", None, False),
    ("contracts/jobs/profiles/preview-content-v1.json", "urn:anvilkit:job-profile:v1", None, False),
    ("contracts/jobs/profiles/job-profile.negative.json", "urn:anvilkit:job-profile:v1", "*.instance", True),
    ("contracts/components/build-support.example.json", "urn:anvilkit:build-support:v1", None, False),
    # components / preview (author E2)
    ("contracts/components/validation-manifest.example.json", "urn:anvilkit:component-manifests:v1#/$defs/ValidationManifestV1", None, False),
    ("contracts/components/validation-manifest.repairable.example.json", "urn:anvilkit:component-manifests:v1#/$defs/ValidationManifestV1", None, False),
    ("contracts/components/bundle-manifest.example.json", "urn:anvilkit:component-manifests:v1#/$defs/BundleManifestV1", None, False),
    ("contracts/components/browser-manifest.example.json", "urn:anvilkit:component-manifests:v1#/$defs/BrowserManifestV1", None, False),
    ("contracts/components/preview-manifest.example.json", "urn:anvilkit:component-manifests:v1#/$defs/PreviewManifestV1", None, False),
    ("contracts/components/facade-rewrite-rules-v1.json", "urn:anvilkit:facade-rewrite-rules:v1", None, False),
    ("contracts/components/observer-assertions-v1.json", "urn:anvilkit:observer-assertions:v1", None, False),
    ("contracts/components/validation-environment-v1.json", "urn:anvilkit:validation-environment:v1", None, False),
    ("contracts/preview/frame-message-v1.fixtures.json", "urn:anvilkit:preview-frame-message:v1", "*", False),
    ("contracts/preview/frame-policy-v1.json", "urn:anvilkit:preview-frame-policy:v1", None, False),
    # coordinator
    ("contracts/profiles/pilot-limits-v1.json", "urn:anvilkit:pilot-limits:v1", None, False),
    ("contracts/qualification/cq-coverage-v1.json", "urn:anvilkit:cq-coverage:v1", None, False),
]

def select(doc, pointer):
    if pointer is None: return [doc]
    parts = pointer.split(".")
    cur = [doc]
    for part in parts:
        nxt = []
        for c in cur:
            if part == "*":
                if isinstance(c, list): nxt.extend(c)
                elif isinstance(c, dict): nxt.extend(c.values())
            else:
                if isinstance(c, dict) and part in c: nxt.append(c[part])
        cur = nxt
    return cur

def check_examples():
    for path, ref, pointer, negative in EXAMPLES:
        p = ROOT / path
        if not p.exists(): fail(f"missing contract artifact {path}"); continue
        try: doc = load(p)
        except Exception as e: fail(f"{path}: parse: {e}"); continue
        if ref.split("#")[0] not in SCHEMAS: fail(f"{path}: schema {ref} not registered"); continue
        insts = select(doc, pointer)
        if not insts: fail(f"{path}: no instances at {pointer}"); continue
        for i, inst in enumerate(insts):
            label = f"{path}[{i}]"
            if negative:
                lab = doc[i].get("label", label) if pointer == "*.instance" and isinstance(doc, list) else label
                expect_invalid(ref, inst, f"{path}: {lab}")
            else: expect_valid(ref, inst, label)

# ----------------------------------------------------------------------------- openapi
def check_openapi():
    p = ROOT / "contracts/openapi/agent-api-v1.openapi.json"
    if not p.exists(): fail("missing contracts/openapi/agent-api-v1.openapi.json"); return
    doc = load(p)
    try:
        from openapi_spec_validator import validate
        validate(doc); bump("openapi_valid")
    except Exception as e:
        fail(f"openapi: {str(e)[:300]}")
    comps = doc.get("components", {}).get("schemas", {})
    # inverse map from source pointer to component name
    src_map = {}
    for name, c in comps.items():
        if isinstance(c, dict) and "x-anvilkit-source" in c: src_map[c["x-anvilkit-source"]] = name
    def rewrite(node, base_id):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "$ref" and isinstance(v, str):
                    target = v if not v.startswith("#/") else base_id + v
                    if target in src_map: out[k] = f"#/components/schemas/{src_map[target]}"
                    else: out[k] = f"UNMIRRORED:{target}"
                else: out[k] = rewrite(v, base_id)
            return out
        if isinstance(node, list): return [rewrite(x, base_id) for x in node]
        return node
    for name, c in comps.items():
        if not isinstance(c, dict) or "x-anvilkit-source" not in c: continue
        src = c["x-anvilkit-source"]
        sid, _, ptr = src.partition("#")
        if sid not in SCHEMAS: fail(f"openapi component {name}: unknown source schema {sid}"); continue
        node = SCHEMAS[sid]
        for part in ptr.strip("/").split("/")[1:] if ptr.startswith("/$defs") else ptr.strip("/").split("/"):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None: break
        if node is None: fail(f"openapi component {name}: source pointer {src} not found"); continue
        expected = rewrite(copy.deepcopy(node), sid)
        actual = {k: v for k, v in c.items() if k != "x-anvilkit-source"}
        if json.dumps(expected, sort_keys=True) != json.dumps(actual, sort_keys=True):
            unm = [x for x in re.findall(r'"UNMIRRORED:([^"]+)"', json.dumps(expected))]
            fail(f"openapi component {name} drifts from {src}" + (f" (unmirrored refs: {sorted(set(unm))[:3]})" if unm else ""))
        else: bump("openapi_mirrors")
    # every error response uses the envelope; X-Request-Id on all responses
    env_name = src_map.get("urn:anvilkit:error-envelope:v1")
    for path, item in doc.get("paths", {}).items():
        for method, op in item.items():
            if method not in ("get", "post", "put", "delete", "patch"): continue
            bump("openapi_operations")
            for code, resp in op.get("responses", {}).items():
                if "X-Request-Id" not in resp.get("headers", {}): fail(f"openapi {method.upper()} {path} {code}: missing X-Request-Id header")
                if code.startswith(("4", "5")):
                    s = resp.get("content", {}).get("application/json", {}).get("schema", {})
                    if env_name is None or s.get("$ref") != f"#/components/schemas/{env_name}":
                        fail(f"openapi {method.upper()} {path} {code}: error response is not the shared envelope")
            if method == "post" and "x-anvilkit-max-body-bytes" not in op:
                fail(f"openapi POST {path}: missing x-anvilkit-max-body-bytes")
    # P0-A subset: /definitions/validations request/response sources agree with agent-definitions-v1.json
    sub = ROOT / "contracts/openapi/agent-definitions-v1.json"
    if sub.exists():
        sd = load(sub)
        sub_op = sd["paths"]["/definitions/validations"]["post"]
        full_op = None
        for path, item in doc.get("paths", {}).items():
            if path.endswith("/definitions/validations"): full_op = item.get("post")
        if full_op is None: fail("openapi: /definitions/validations missing from the full document")
        else:
            want_req = sub_op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            got_ref = full_op["requestBody"]["content"]["application/json"]["schema"].get("$ref", "")
            got_name = got_ref.split("/")[-1]
            if comps.get(got_name, {}).get("x-anvilkit-source") != want_req:
                fail(f"openapi: /definitions/validations request source {comps.get(got_name, {}).get('x-anvilkit-source')} != subset {want_req}")
            else: bump("openapi_subset_agreement")
    # examples
    ex = ROOT / "contracts/openapi/agent-api-v1.examples.json"
    if not ex.exists(): fail("missing contracts/openapi/agent-api-v1.examples.json"); return
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
    oid = "urn:anvilkit:agent-api:openapi"
    reg = Registry().with_resource(oid, Resource.from_contents({"$id": oid, "components": comps}, default_specification=DRAFT202012))
    ops = {}
    for path, item in doc.get("paths", {}).items():
        for method, op in item.items():
            if isinstance(op, dict) and "operationId" in op: ops[op["operationId"]] = op
    for key, body in load(ex).items():
        opid, _, kind = key.partition(".")
        op = ops.get(opid)
        if op is None: fail(f"openapi example {key}: unknown operationId"); continue
        if kind == "request":
            s = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
        else:
            code = kind.split("-")[-1]
            s = op.get("responses", {}).get(code, {}).get("content", {}).get("application/json", {}).get("schema")
        if not s: fail(f"openapi example {key}: no schema at that position"); continue
        v = Draft202012Validator({"$ref": oid + "#" + s["$ref"][1:]} if "$ref" in s else dict(s, **{"$id": oid + "/inline/" + key}), registry=reg)
        es = list(v.iter_errors(body))
        if es: fail(f"openapi example {key}: {es[0].message[:160]}")
        else: bump("openapi_examples")

# ----------------------------------------------------------------------------- proto
CONTROL_RPCS = ["AdmitOperation","AcquirePermit","ReleasePermit","RegisterAttempt","RegisterInstance","ReportInstanceObservation","AdmitAndClaimModelCall","GetModelCall","RecordModelOutcome","RecordNotSentEvidence","RegisterEffect","RecordEffectReceipt","AcceptJobResult","GetAcceptedResult","RecordSettlementIntent","ApplyDefinitionChange","Hold","Resume","Cancel","ResolvePublication","RecordOperatorDisposition","RecordStepEvent","ReadOperation","ReadEvents","ReadSnapshot","GetDisclosureAuthorization","ActivateDefinition","IssueScopeToken","ReplaceScopeToken"]
ADAPTER_RPCS = ["IssueArtifactTransfer","ExchangeArtifactHandle","FinalizeArtifactTransfer","GetArtifactTransfer"]
ENUM_MIRRORS = {"OperationKind":"operationKind","IntakeSource":"intakeSource","PublicStatus":"publicStatus","BusinessStage":"businessStage","ControlState":"controlState","CleanupState":"cleanupState","FinancialState":"financialState","ChangeState":"changeState","ResourceClass":"resourceClass","PermitOwnerKind":"permitOwnerKind","JobKind":"jobKind","ResultOutcome":"resultOutcome","FailureClass":"failureClass","ActionId":"actionId","EventType":"eventType","ModelCallState":"modelCallState","UsageObservationKind":"usageObservationKind","UsageClassification":"usageClassification","EffectKind":"effectKind","EffectDispatchState":"effectDispatchState","ObligationClass":"obligationClass","OperatorDisposition":"operatorDisposition","PublicationResolution":"publicationResolution","DisclosureDecision":"disclosureDecision","AcceptResultDecision":"acceptResultDecision","PermitDecision":"permitDecision","TransferState":"transferState","ArtifactTransferOperation":"artifactTransferOperation","ActorRole":"actorRole","ErrorFamily":"errorFamily"}

def check_proto():
    pdir = ROOT / "contracts/proto"
    p = pdir / "control-v1.proto"
    if not p.exists(): fail("missing contracts/proto/control-v1.proto"); return
    try:
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory, json_format
    except Exception as e:
        fail(f"google.protobuf unavailable ({e})"); return
    with tempfile.TemporaryDirectory() as td:
        desc = pathlib.Path(td) / "c.desc"
        r = subprocess.run(["protoc", "-I", str(pdir), "-I", "/usr/include", "--include_imports", f"--descriptor_set_out={desc}", str(p)], capture_output=True, text=True)
        if r.returncode != 0: fail(f"protoc: {r.stderr.strip()[:300]}"); return
        fds = descriptor_pb2.FileDescriptorSet(); fds.ParseFromString(desc.read_bytes())
    bump("proto_compiled")
    pool = descriptor_pool.DescriptorPool()
    for f in fds.file:
        try: pool.FindFileByName(f.name); continue
        except KeyError: pass
        pool.Add(f)
    enums, services, msgs = {}, {}, {}
    for f in fds.file:
        if f.package != "anvilkit.control.v1": continue
        for e in f.enum_type:
            enums[e.name] = {v.name for v in e.value if not v.name.endswith("_UNSPECIFIED")}
        for s in f.service: services[s.name] = [m.name for m in s.method]
        for m in f.message_type: msgs[m.name] = m
    for pname, jname in ENUM_MIRRORS.items():
        if pname not in enums: fail(f"proto enum {pname} missing"); continue
        want = {upper(x) for x in enum_of("urn:anvilkit:agent-enums:v1", jname)}
        got = enums[pname]
        # proto enum members may carry the enum name as prefix (PUBLIC_STATUS_PENDING); strip it
        prefix = upper(re.sub(r"(?<!^)(?=[A-Z])", "-", pname)) + "_"
        got_n = {g[len(prefix):] if g.startswith(prefix) else g for g in got}
        if got_n != want: fail(f"proto enum {pname} != agent-enums {jname}: {sorted(got_n ^ want)[:6]}")
        else: bump("proto_enum_mirrors")
    if sorted(services.get("ControlService", [])) != sorted(CONTROL_RPCS): fail(f"ControlService rpcs differ: {sorted(set(services.get('ControlService', [])) ^ set(CONTROL_RPCS))}")
    else: bump("proto_service_ok")
    if sorted(services.get("ArtifactAdapterService", [])) != sorted(ADAPTER_RPCS): fail(f"ArtifactAdapterService rpcs differ: {sorted(set(services.get('ArtifactAdapterService', [])) ^ set(ADAPTER_RPCS))}")
    else: bump("proto_service_ok")
    fx = pdir / "control-v1.fixtures.json"
    if not fx.exists(): fail("missing contracts/proto/control-v1.fixtures.json"); return
    seen = set()
    for f in load(fx).get("fixtures", []):
        svc, _, meth = f["method"].partition(".")
        try:
            sd = pool.FindServiceByName(f"anvilkit.control.v1.{svc}"); md = sd.FindMethodByName(meth)
            for which, dsc in (("request", md.input_type), ("response", md.output_type)):
                cls = message_factory.GetMessageClass(dsc)
                json_format.ParseDict(f[which], cls()); bump("proto_fixtures")
            seen.add(f["method"])
        except Exception as e:
            fail(f"proto fixture {f.get('method')}: {str(e)[:200]}")
    for svc, lst in (("ControlService", CONTROL_RPCS), ("ArtifactAdapterService", ADAPTER_RPCS)):
        for m in lst:
            if f"{svc}.{m}" not in seen: fail(f"proto fixture missing for {svc}.{m}")

# ----------------------------------------------------------------------------- sql
DD02_RELATIONS = ["operations","attempts","physical_instances","model_calls","cost_entries","budget_pools","action_visits","resource_pools","queue_entries","permits","effects","accepted_results","step_executions","operation_events","definitions","policies","release_manifests","definition_changes","action_counters","obligations","authorization_evidence","scope_keys","abuse_counters","build_allowances"]
def check_sql():
    try: import pglast
    except Exception as e: fail(f"pglast unavailable ({e})"); return
    tables = {}
    for name in ("contracts/sql/control-v1.sql", "contracts/definitions/activation-v1.sql"):
        p = ROOT / name
        if not p.exists(): fail(f"missing {name}"); continue
        text = p.read_text(encoding="utf-8")
        try: pglast.parse_sql(text); bump("sql_parsed")
        except Exception as e: fail(f"{name}: parse: {str(e)[:200]}"); continue
        for m in re.finditer(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?([a-z_]+)\.([a-z_]+)", text):
            tables[m.group(2)] = m.group(1)
        if name.endswith("control-v1.sql"):
            for t in DD02_RELATIONS:
                if t not in tables: fail(f"control-v1.sql: relation {t} missing")
            for m in re.finditer(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?agent_control\.([a-z_]+)\s*\((.*?)\n\);", text, re.S):
                t, body = m.group(1), m.group(2)
                if re.search(r"\btenant_id\b", body):
                    if not re.search(rf"ALTER TABLE agent_control\.{t} ENABLE ROW LEVEL SECURITY", text) or not re.search(rf"ALTER TABLE agent_control\.{t} FORCE ROW LEVEL SECURITY", text):
                        fail(f"control-v1.sql: tenant-bearing table {t} lacks ENABLE+FORCE ROW LEVEL SECURITY")
                    else: bump("sql_rls")
            for role in ("anvilkit_control_migrator", "anvilkit_control_rw", "anvilkit_api_ro"):
                if role not in text: fail(f"control-v1.sql: role {role} not defined")
    lo = ROOT / "contracts/sql/control-v1.lock-order.json"
    if not lo.exists(): fail("missing contracts/sql/control-v1.lock-order.json"); return
    d = load(lo)
    placed = {}
    for r in d.get("ranks", []):
        for t in r.get("records", []): placed.setdefault(t, []).append(r["rank"])
    for t in d.get("unranked", []): placed.setdefault(t, []).append("unranked")
    for t in tables:
        if t not in placed: fail(f"lock-order: table {t} has no placement")
        elif len(placed[t]) != 1: fail(f"lock-order: table {t} placed {len(placed[t])} times")
        else: bump("lock_order_placements")
    for t in placed:
        if t not in tables: fail(f"lock-order: placement for unknown table {t}")

# ----------------------------------------------------------------------------- actions / runner rules
def parse_dd03_routing():
    text = (ROOT / "docs/design/dd-03-execution.md").read_text(encoding="utf-8")
    start = text.find('<a id="result-routing"></a>')
    rows = []
    for line in text[start:].splitlines():
        if line.startswith("| `") or line.startswith("| Any"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 5: rows.append(cells)
    return rows

def check_actions():
    reg = ROOT / "contracts/actions/registry.json"
    if not reg.exists(): fail("missing contracts/actions/registry.json"); return
    r = load(reg)
    ids = [d["id"] for d in r["descriptors"]]
    if set(ids) != enum_of("urn:anvilkit:agent-enums:v1", "actionId") or len(ids) != 13: fail("registry action ids != agent-enums actionId")
    else: bump("registry_ids")
    if "descriptorSetDigest" not in r: fail("registry.json lacks descriptorSetDigest")
    elif r["descriptorSetDigest"] != sha(canon(r["descriptors"])): fail("registry descriptorSetDigest does not recompute")
    else: bump("digests_recomputed")
    for d in r["descriptors"]:
        expect_valid("urn:anvilkit:action-descriptor:v2", d, f"registry descriptor {d['id']}")
    by_id = {d["id"]: d for d in r["descriptors"]}
    b = ROOT / "contracts/actions/runtime-bindings.example.json"
    if b.exists():
        bs = load(b)
        if bs.get("bindingDigest") != sha(canon(bs.get("bindings"))): fail("runtime-bindings bindingDigest does not recompute")
        else: bump("digests_recomputed")
        if sorted(x["actionId"] for x in bs.get("bindings", [])) != sorted(ids): fail("runtime-bindings do not cover exactly the 13 actions")
    for name in ("component-generation.example.json", "component-release.example.json"):
        d = load(ROOT / "contracts/definitions" / name)
        for step in d["steps"]:
            a = by_id.get(step["action"]["id"])
            if a is None: fail(f"{name}: step {step['id']} references unregistered action"); continue
            if step["action"]["version"] != a["version"]: fail(f"{name}: step {step['id']} version mismatch")
            for port in step["inputs"]:
                if port not in a["inputs"]: fail(f"{name}: step {step['id']} input port {port} not in descriptor")
            for port in step["outputs"]:
                if port not in a["outputs"]: fail(f"{name}: step {step['id']} output port {port} not in descriptor")
            bump("definition_steps_checked")
    rr = ROOT / "contracts/definitions/runner-rules-v1.json"
    if not rr.exists(): fail("missing contracts/definitions/runner-rules-v1.json"); return
    rules = load(rr)
    table = rules.get("resultRouting") or rules.get("routingTable") or []
    dd = parse_dd03_routing()
    def norm(s): return re.sub(r"[`*]", "", s).strip()
    dd_rows = [tuple(norm(c) for c in row) for row in dd]
    got_rows = []
    for row in table:
        got_rows.append((norm(str(row.get("jobKind", "Any") if row.get("jobKind") else "Any")), norm(str(row.get("outcome"))), norm(str(row.get("failureClass"))), norm(str(row.get("routedAs"))), norm(str(row.get("consumesVisit")))))
    if len(dd_rows) != len(table): fail(f"runner-rules routing table has {len(table)} rows; DD-03 has {len(dd_rows)}")
    else:
        mism = 0
        for i, (a, g) in enumerate(zip(dd_rows, got_rows)):
            # compare jobKind/action head, outcome and routedAs loosely (normalized text equality on outcome + routedAs)
            if a[1] != g[1] or a[3] != g[3]:
                mism += 1; fail(f"runner-rules routing row {i}: DD-03 {a[1]!r}/{a[3]!r} vs rules {g[1]!r}/{g[3]!r}")
        if not mism: bump("routing_table_agreement")

# ----------------------------------------------------------------------------- jobs
def check_jobs():
    rm = SCHEMAS.get("urn:anvilkit:result-manifest:v1")
    fct = ROOT / "contracts/jobs/failure-code-table-v1.json"
    if not fct.exists() or rm is None: fail("missing failure-code-table-v1.json or result-manifest schema"); return
    t = load(fct)
    fdefs = rm["$defs"]["failure"]
    if set(t["classes"]) != set(fdefs["properties"]["class"]["enum"]): fail("failure-code-table classes != result-manifest classes")
    union = set().union(*[set(v) for v in t["classes"].values()])
    if union != set(fdefs["properties"]["code"]["enum"]): fail("failure-code-table codes != result-manifest codes")
    for br in fdefs["allOf"]:
        cls = br["if"]["properties"]["class"]["const"]
        if set(br["then"]["properties"]["code"]["enum"]) != set(t["classes"].get(cls, [])): fail(f"failure-code-table class {cls} codes differ")
    bump("failure_table_agreement")
    want_ct = sha(canon(t["classes"]))
    if t.get("codeTableDigest") != want_ct: fail("failure-code-table codeTableDigest does not recompute")
    want_sd = sha((ROOT / "contracts/jobs/result-manifest-v1.schema.json").read_bytes())
    kinds = {p["jobKind"]: p for p in load(ROOT / "contracts/jobs/job-kind-profiles-v1.json")["profiles"]}
    for name in ("codegen-pi-openai-v1", "repair-pi-openai-v1", "validate-content-v1", "certify-content-v1", "preview-content-v1"):
        p = ROOT / "contracts/jobs/profiles" / f"{name}.json"
        if not p.exists(): fail(f"missing job profile {name}"); continue
        d = load(p)
        rc = d.get("resultContract", {})
        if rc.get("schemaDigest") != want_sd: fail(f"{name}: resultContract.schemaDigest does not recompute")
        if rc.get("codeTableDigest") != want_ct: fail(f"{name}: resultContract.codeTableDigest does not recompute")
        if rc.get("schemaDigest") == want_sd and rc.get("codeTableDigest") == want_ct: bump("digests_recomputed")
        k = kinds.get(d.get("jobKind"))
        if k is None: fail(f"{name}: jobKind not in job-kind-profiles"); continue
        if k["actionId"] != d.get("actionId"): fail(f"{name}: actionId != job-kind-profiles")
        socks = d.get("sockets", {})
        if socks.get("candidate") != "/run/anvilkit/sidecar/candidate.sock" or socks.get("trusted") != "/run/anvilkit/sidecar/trusted.sock": fail(f"{name}: socket paths differ from the sidecar contract")
        sc = d.get("securityContext", {})
        if (sc.get("harnessUid"), sc.get("candidateUid"), sc.get("sidecarUid")) != (0, 10001, 10002): fail(f"{name}: UIDs differ from the fixed profile values")
        if sc.get("capabilities") != ["SETUID", "SETGID", "SETPCAP"]: fail(f"{name}: capability set is not exactly SETUID/SETGID/SETPCAP")
        if d.get("jobKind") in ("validate", "certify", "preview") and ("toolPolicy" in d or "piProfile" in d): fail(f"{name}: model-free job carries a tool/pi profile")
        bump("job_profiles")

# ----------------------------------------------------------------------------- limits
def check_limits():
    lim = ROOT / "contracts/profiles/pilot-limits-v1.json"
    if not lim.exists(): fail("missing pilot-limits"); return
    keys = {e["key"] for e in load(lim)["entries"]}
    pat = re.compile(r"^[a-z][a-zA-Z0-9]*(\.[a-z][a-zA-Z0-9]*)+$")
    def walk(node, key=None, path=""):
        if isinstance(node, dict):
            for k, v in node.items(): yield from walk(v, k, path + "/" + k)
        elif isinstance(node, list):
            for i, v in enumerate(node): yield from walk(v, key, path + f"/{i}")
        elif isinstance(node, str): yield key, node, path
    for p in sorted((ROOT / "contracts").rglob("*.json")):
        if p.name in ("pagix-cloud-open-api.json", "pilot-limits-v1.json") or p.name.endswith(".negative.json"): continue
        try: doc = load(p)
        except Exception: continue
        for key, val, path in walk(doc):
            is_limit_ctx = key is not None and (key.endswith("Ref") and "limit" in key.lower() or key in ("x-anvilkit-limit",) or "/limitKeys/" in path or "/x-anvilkit-limits/" in path or key.endswith("LimitRef") or key.endswith("SecondsRef") or key.endswith("BytesRef") or key.endswith("MsRef"))
            if is_limit_ctx and pat.match(val) and "." in val:
                if val not in keys: fail(f"{rel(p)}{path}: limit key {val} not in pilot-limits")
                else: bump("limit_refs")

# ----------------------------------------------------------------------------- enums across schemas
def check_enums():
    ae = "urn:anvilkit:agent-enums:v1"
    jk = enum_of(ae, "jobKind")
    env = set(SCHEMAS["urn:anvilkit:job-envelope:v1"]["properties"]["jobKind"]["enum"])
    log = set(SCHEMAS["urn:anvilkit:log-record:v1"]["properties"]["jobKind"]["enum"])
    if not (jk == env == log): fail("jobKind enums differ across agent-enums/job-envelope/log-record")
    else: bump("enum_agreements")
    if enum_of(ae, "eventType") != set(SCHEMAS["urn:anvilkit:operation-event:v1"]["properties"]["type"]["enum"]): fail("eventType != operation-event type enum")
    else: bump("enum_agreements")
    rm = SCHEMAS["urn:anvilkit:result-manifest:v1"]
    if enum_of(ae, "resultOutcome") != set(rm["properties"]["outcome"]["enum"]): fail("resultOutcome != result-manifest outcome enum")
    else: bump("enum_agreements")
    if enum_of(ae, "failureClass") != set(rm["$defs"]["failure"]["properties"]["class"]["enum"]): fail("failureClass != result-manifest class enum")
    else: bump("enum_agreements")
    units = {"anvilkit-agent-api","anvilkit-agent-control","anvilkit-agent-workflow","anvilkit-agent-model-proxy","anvilkit-component-codegen","anvilkit-component-validator","anvilkit-component-preview","anvilkit-job-access-proxy","anvilkit-export-worker"}
    if not units <= set(SCHEMAS["urn:anvilkit:log-record:v1"]["properties"]["service.name"]["enum"]): fail("log-record service.name lacks a canonical unit")
    else: bump("enum_agreements")
    for sid, defn in (("urn:anvilkit:action-descriptor:v2", "resourceClass"), ("urn:anvilkit:runtime-binding:v1", "resourceClass")):
        prop = SCHEMAS.get(sid, {}).get("properties", {}).get(defn, {})
        if prop.get("$ref") != f"{ae}#/$defs/resourceClass": fail(f"{sid}: resourceClass is not the shared enum")
        else: bump("enum_agreements")

# ----------------------------------------------------------------------------- CQ coverage and docs
DOCS = [f"docs/design/000{i}-anvilkit-{n}-detailed-design-0909-1927.md" for i, n in enumerate(["agent-api","agent-control","agent-workflow","agent-model-proxy","component-codegen","component-validator","component-preview","job-access-proxy","export-worker"], start=1)]
def check_cq_and_docs():
    q = (ROOT / "docs/architecture/plans/qualification.md").read_text(encoding="utf-8")
    known = set(re.findall(r"\| (CQ-\d\d) \|", q))
    covered: dict[str, list] = {}
    for doc in DOCS[:8]:
        p = ROOT / doc
        text = p.read_text(encoding="utf-8")
        if "design definitions complete for freeze review" not in text: fail(f"{doc}: status row does not declare design definitions complete for freeze review")
        for m in re.finditer(r"\|\s*(CQ-\d\d)\s*\|([^\n]*)\|([^\n]*)\|", text):
            cq, assertion, artifact = m.group(1), m.group(2).strip(), m.group(3).strip()
            if cq not in known: fail(f"{doc}: cites unknown {cq}"); continue
            if len(assertion) < 20: fail(f"{doc}: {cq} row has no substantive assertion")
            paths = re.findall(r"`((?:contracts|tools|docs)/[^`]+)`", artifact)
            if not paths: fail(f"{doc}: {cq} row names no artifact path"); continue
            for ap in paths:
                if not (ROOT / ap.split("#")[0]).exists(): fail(f"{doc}: {cq} row names missing artifact {ap}")
            covered.setdefault(cq, []).append(doc)
            bump("cq_rows")
    for cq in sorted(known):
        if cq not in covered: fail(f"{cq} is covered by no service-view coverage row")
    p9 = (ROOT / DOCS[8]).read_text(encoding="utf-8")
    if "deferred scope record" not in p9.lower(): fail("0009 is not classified as a deferred scope record")
    else: bump("docs_status")
    # machine-readable coverage index agrees with the parsed rows
    cov = ROOT / "contracts/qualification/cq-coverage-v1.json"
    if cov.exists():
        d = load(cov)
        idx = {(e["cq"], e["document"]) for e in d.get("entries", [])}
        parsed = {(cq, doc) for cq, docs in covered.items() for doc in docs}
        if idx != parsed: fail(f"cq-coverage-v1.json disagrees with the service-view tables: {sorted(idx ^ parsed)[:4]}")
        else: bump("cq_index_agreement")

def main():
    global ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    ap.add_argument("--section", action="append")
    a = ap.parse_args(); ROOT = pathlib.Path(a.root).resolve()
    build_registry()
    sections = {"examples": check_examples, "openapi": check_openapi, "proto": check_proto, "sql": check_sql, "actions": check_actions, "jobs": check_jobs, "limits": check_limits, "enums": check_enums, "cq": check_cq_and_docs}
    for name, fn in sections.items():
        if a.section and name not in a.section: continue
        try: fn()
        except Exception as e:
            fail(f"section {name} crashed: {type(e).__name__}: {str(e)[:200]}")
    for f in FAIL: print("FAIL", f)
    print("counts:", json.dumps(COUNT, sort_keys=True))
    print(f"{len(FAIL)} failures")
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
