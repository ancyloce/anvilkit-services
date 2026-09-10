#!/usr/bin/env python3
"""Documentation, link, schema and fixture checks for anvilkit-services.

Usage: python3 tools/check-docs.py [--root PATH]

Checks (all local, no network, no services):
  1. Markdown: fenced blocks balanced, table rows have the header's column count,
     every relative link resolves to a file and every fragment resolves to an explicit
     `<a id>` or a generated heading anchor.
  2. JSON under contracts/: parses with duplicate keys rejected; every JSON Schema
     passes the Draft 2020-12 metaschema; URN $refs resolve through a local registry.
  3. Fixtures: definition examples, the job-envelope and job-result examples and the
     telemetry fixtures validate against their schemas; fixture-specific semantic rules
     hold; a set of deliberately malformed variants is rejected.
  4. The log event catalog in docs/architecture/plans/operations.md and the schema
     enum agree; the job-failure class/code table in docs/design/dd-03-execution.md and
     the result-manifest schema enums agree.
  5. Shared-log content containment: the log-record schema defines no free-text field a
     candidate can reach, the hostile-output fixture's declared content tokens really do
     occur in its inputs, none of that content survives into any field of the expected
     projection, and every relocation of it into message/error.message/attributes/body is
     rejected. No field is exempt from the scan: timestamp is pinned to a UTC Z pattern in
     common-v1 rather than excluded from it.

Exit status 1 on any failure. `jsonschema` (with `referencing`) is required for the
schema checks; without it those checks are reported as skipped and the run fails.
These are document checks only; they do not qualify any runtime behavior.
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import re
import sys

FAILURES: list[str] = []
WARNINGS: list[str] = []
COUNTS: dict[str, int] = {}


def fail(msg: str) -> None:
    FAILURES.append(msg)


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def count(key: str, n: int = 1) -> None:
    COUNTS[key] = COUNTS.get(key, 0) + n


# ----------------------------------------------------------------------------- markdown

FENCE_RE = re.compile(r"^\s*(```|~~~)")
EXPLICIT_ANCHOR_RE = re.compile(r'<a\s+id="([^"]+)"')
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
CODE_SPAN_RE = re.compile(r"`[^`\n]*`")


def strip_fences(text: str) -> str:
    out, inside = [], False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            inside = not inside
            out.append("")
            continue
        out.append("" if inside else line)
    return "\n".join(out)


def slugify(heading: str) -> str:
    h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links -> text
    h = h.replace("`", "").replace("*", "").replace("_", "")
    h = h.strip().lower()
    h = re.sub(r"[^\w\s-]", "", h, flags=re.UNICODE)
    h = re.sub(r"\s", "-", h)
    return h


def anchors_for(text: str) -> set[str]:
    anchors: set[str] = set(EXPLICIT_ANCHOR_RE.findall(text))
    seen: dict[str, int] = {}
    for line in strip_fences(text).splitlines():
        m = HEADING_RE.match(line)
        if not m:
            continue
        slug = slugify(m.group(2))
        if slug in seen:
            seen[slug] += 1
            anchors.add(f"{slug}-{seen[slug]}")
        else:
            seen[slug] = 0
            anchors.add(slug)
    return anchors


def check_markdown(root: pathlib.Path, files: list[pathlib.Path]) -> None:
    texts = {p: p.read_text(encoding="utf-8") for p in files}
    anchors = {p: anchors_for(t) for p, t in texts.items()}
    explicit_all = {}
    for p, t in texts.items():
        ids = EXPLICIT_ANCHOR_RE.findall(t)
        dupes = {i for i in ids if ids.count(i) > 1}
        for d in sorted(dupes):
            fail(f"{p.relative_to(root)}: duplicate explicit anchor id {d!r}")
        explicit_all[p] = ids
    for p, t in texts.items():
        rel = p.relative_to(root)
        fences = sum(1 for line in t.splitlines() if FENCE_RE.match(line))
        if fences % 2:
            fail(f"{rel}: unbalanced code fences ({fences})")
        for ln, line in enumerate(t.splitlines(), 1):
            if line.rstrip() != line:
                warn(f"{rel}:{ln}: trailing whitespace")
        # tables
        body = strip_fences(t).splitlines()
        i = 0
        while i < len(body):
            if body[i].lstrip().startswith("|"):
                block = []
                while i < len(body) and body[i].lstrip().startswith("|"):
                    block.append((i + 1, body[i]))
                    i += 1
                if len(block) >= 2:
                    count("tables")
                    widths = []
                    for ln, row in block:
                        cells = CODE_SPAN_RE.sub("code", row.strip()).strip("|").split("|")
                        widths.append((ln, len(cells)))
                    header = widths[0][1]
                    for ln, w in widths[1:]:
                        if w != header:
                            fail(f"{rel}:{ln}: table row has {w} cells, header has {header}")
            else:
                i += 1
        # links
        for m in LINK_RE.finditer(strip_fences(CODE_SPAN_RE.sub("", t))):
            target = m.group(2)
            count("links")
            if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                count("external_links")
                continue
            path_part, _, frag = target.partition("#")
            if path_part:
                dest = (p.parent / path_part).resolve()
                if not dest.exists():
                    fail(f"{rel}: broken link target {target!r}")
                    continue
                if frag:
                    if dest.suffix.lower() == ".md" and dest in anchors:
                        if frag not in anchors[dest]:
                            fail(f"{rel}: missing anchor {frag!r} in {dest.relative_to(root)}")
                    elif dest.suffix.lower() == ".md":
                        other = dest.read_text(encoding="utf-8")
                        if frag not in anchors_for(other):
                            fail(f"{rel}: missing anchor {frag!r} in {dest.relative_to(root)}")
            elif frag:
                if frag not in anchors[p]:
                    fail(f"{rel}: missing in-file anchor {frag!r}")


# ----------------------------------------------------------------------------- json

def no_duplicates(pairs):
    seen = set()
    out = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"duplicate key {k!r}")
        seen.add(k)
        out[k] = v
    return out


def load_json(p: pathlib.Path):
    return json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)


TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")


def check_contracts(root: pathlib.Path) -> None:
    contracts = root / "contracts"
    docs = {}
    for p in sorted(contracts.rglob("*.json")):
        try:
            docs[p] = load_json(p)
            count("json_files")
        except Exception as e:  # noqa: BLE001
            fail(f"{p.relative_to(root)}: JSON parse failed: {e}")
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except Exception as e:  # noqa: BLE001
        fail(f"jsonschema/referencing unavailable ({e}); schema checks skipped")
        return
    schemas = {p: d for p, d in docs.items() if isinstance(d, dict) and "$id" in d and "$schema" in d}
    registry = Registry().with_resources(
        [(d["$id"], Resource.from_contents(d, default_specification=DRAFT202012)) for d in schemas.values()]
    )
    by_id = {d["$id"]: d for d in schemas.values()}
    for p, d in schemas.items():
        try:
            Draft202012Validator.check_schema(d)
            count("schemas")
        except Exception as e:  # noqa: BLE001
            fail(f"{p.relative_to(root)}: metaschema check failed: {e}")

    def validator(schema_id: str) -> Draft202012Validator:
        return Draft202012Validator(by_id[schema_id], registry=registry)

    def expect_valid(schema_id: str, instance, label: str) -> None:
        errors = sorted(validator(schema_id).iter_errors(instance), key=lambda e: list(e.path))
        if errors:
            for e in errors[:5]:
                fail(f"{label}: {'/'.join(str(x) for x in e.path) or '<root>'}: {e.message[:200]}")
        else:
            count("fixtures_valid")

    def expect_invalid(schema_id: str, instance, label: str) -> None:
        if validator(schema_id).is_valid(instance):
            fail(f"{label}: malformed variant was accepted")
        else:
            count("negative_fixtures_rejected")

    def get(relpath: str):
        p = contracts / relpath
        if p not in docs:
            fail(f"missing contract file {relpath}")
            return None
        return docs[p]

    # definitions
    for name in ("definitions/component-generation.example.json", "definitions/component-release.example.json"):
        d = get(name)
        if d is not None:
            expect_valid("urn:anvilkit:definition:v1", d, name)

    # job envelope
    env = get("jobs/job-envelope-v1.example.json")
    if env is not None:
        expect_valid("urn:anvilkit:job-envelope:v1", env, "jobs/job-envelope-v1.example.json")
        legacy = copy.deepcopy(env)
        legacy.pop("traceContext", None)
        expect_valid("urn:anvilkit:job-envelope:v1", legacy, "job envelope without traceContext (backward compatibility)")
        bad = copy.deepcopy(env)
        bad["traceContext"]["traceparent"] = "00-notahex-0000-01"
        expect_invalid("urn:anvilkit:job-envelope:v1", bad, "job envelope with malformed traceparent")
        bad = copy.deepcopy(env)
        bad["traceContext"]["baggage"] = "tenant=team-1"
        expect_invalid("urn:anvilkit:job-envelope:v1", bad, "job envelope with baggage inside traceContext")
        bad = copy.deepcopy(env)
        bad["instanceId"] = "inst-forged"
        expect_invalid("urn:anvilkit:job-envelope:v1", bad, "job envelope carrying candidate physical identity")
        legacy = copy.deepcopy(env)
        legacy.pop("resultContract", None)
        expect_valid("urn:anvilkit:job-envelope:v1", legacy, "job envelope without resultContract (backward compatibility)")
        bad = copy.deepcopy(env)
        bad["resultContract"]["codeTableDigest"] = "sha256:not-a-digest"
        expect_invalid("urn:anvilkit:job-envelope:v1", bad, "job envelope with a malformed code-table digest")
        bad = copy.deepcopy(env)
        bad["resultContract"]["supportedCodes"] = ["CANDIDATE_BUILD_FAILED"]
        expect_invalid("urn:anvilkit:job-envelope:v1", bad, "job envelope inlining a code list instead of its digest")
        bad = copy.deepcopy(env)
        bad["resultContract"].pop("schemaDigest")
        expect_invalid("urn:anvilkit:job-envelope:v1", bad, "job envelope resultContract without a schema digest")

    # reference kinds: the two shared shapes partition the registered kinds, and every
    # kind used by a descriptor port or a definition input slot is registered in one of them
    values = get("values/common-v1.schema.json")
    if values is not None:
        art = set(values["$defs"]["artifactRef"]["properties"]["kind"]["enum"])
        api = set(values["$defs"]["apiOperationRef"]["properties"]["kind"]["enum"])
        overlap = art & api
        if overlap:
            fail(f"reference kind registered in both shapes: {sorted(overlap)}")
        if "contentDigest" in values["$defs"]["apiOperationRef"]["properties"]:
            fail("apiOperationRef must not carry contentDigest")
        known = art | api
        for name in ("actions/component-validate.example.json", "actions/component-publication-status.example.json"):
            d = get(name)
            if d is None:
                continue
            used = {port["kind"] for group in ("inputs", "outputs") for port in d.get(group, {}).values() if "kind" in port}
            for k in sorted(used - known):
                fail(f"{name}: port reference kind {k!r} is not registered in common-v1")
            if used and used <= known:
                count("reference_kinds_checked")
        for name in ("definitions/component-generation.example.json", "definitions/component-release.example.json"):
            d = get(name)
            if d is None:
                continue
            used = set(d.get("inputSlots", {}).values())
            for k in sorted(used - known):
                fail(f"{name}: inputSlot reference kind {k!r} is not registered in common-v1")
            if used and used <= known:
                count("reference_kinds_checked")
        def ref_validator(defname: str) -> Draft202012Validator:
            return Draft202012Validator(
                {"$ref": f"urn:anvilkit:values:v1#/$defs/{defname}"}, registry=registry
            )

        v = ref_validator("apiOperationRef")
        sample = {"authority": "pagix", "kind": "publication", "refId": "pub-7f3c2a",
                  "subjectDigest": "sha256:3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855e"}
        if v.is_valid(sample):
            count("fixtures_valid")
        else:
            fail("apiOperationRef sample rejected by its own schema")
        if v.is_valid(dict(sample, contentDigest="sha256:" + "0" * 64)):
            fail("apiOperationRef accepted a fabricated contentDigest")
        else:
            count("negative_fixtures_rejected")
        v = ref_validator("timestamp")
        for good in ("2026-09-08T10:15:00.052Z", "2026-09-08T10:17:20Z",
                     "2026-09-08T10:15:00.123456789Z"):
            if v.is_valid(good):
                count("fixtures_valid")
            else:
                fail(f"timestamp rejected a well-formed UTC instant {good!r}")
        for bad in ("export function Button(){}", "2026-09-08T10:15:00.000",
                    "2026-09-08T10:15:00+01:00", "2026-13-08T10:15:00Z",
                    "2026-09-08T25:15:00Z", "2026-09-08 10:15:00Z",
                    "2026-09-08T10:15:00Z ", ""):
            if v.is_valid(bad):
                fail(f"timestamp accepted {bad!r}; the UTC Z contract is not enforced")
            else:
                count("negative_fixtures_rejected")
        if ref_validator("artifactRef").is_valid(
            {"kind": "publication", "refId": "pub-7f3c2a", "subjectDigest": "sha256:" + "a" * 64,
             "contentDigest": "sha256:" + "b" * 64, "sizeBytes": "1", "objectVersion": "v1"}
        ):
            fail("artifactRef accepted an API-operation reference kind")
        else:
            count("negative_fixtures_rejected")

    # job result manifest
    rm_id = "urn:anvilkit:result-manifest:v1"
    ready = get("jobs/result-manifest-v1.ready.example.json")
    prof = get("jobs/result-manifest-v1.profile-failure.example.json")
    if ready is not None:
        expect_valid(rm_id, ready, "jobs/result-manifest-v1.ready.example.json")
        bad = copy.deepcopy(ready)
        bad["failure"] = {"class": "candidate-code", "code": "CANDIDATE_BUILD_FAILED", "retryable": False}
        expect_invalid(rm_id, bad, "successful result carrying a failure block")
        bad = copy.deepcopy(ready)
        bad["outputs"]["Source"] = bad["outputs"].pop("source")
        expect_invalid(rm_id, bad, "output port name outside the registered pattern")
    if prof is not None:
        expect_valid(rm_id, prof, "jobs/result-manifest-v1.profile-failure.example.json")
        bad = copy.deepcopy(prof)
        bad.pop("failure")
        expect_invalid(rm_id, bad, "failed result without a typed failure")
        bad = copy.deepcopy(prof)
        bad["failure"]["code"] = "CANDIDATE_BUILD_FAILED"
        expect_invalid(rm_id, bad, "failure code outside its declared class")
        bad = copy.deepcopy(prof)
        bad["failure"]["code"] = "NOT_REGISTERED"
        expect_invalid(rm_id, bad, "unregistered failure code")
        if ready is not None:
            bad = copy.deepcopy(prof)
            bad["outputs"] = copy.deepcopy(ready["outputs"])
            expect_invalid(rm_id, bad, "failed result fabricating a successful output")
        bad = copy.deepcopy(prof)
        bad["outcome"] = "canceled"
        expect_valid(rm_id, bad, "canceled result with a typed failure")
        # rollout compatibility cases (docs/design/dd-03-execution.md#result-contract-rollout).
        # A code that a receiver does not register is rejected at intake, before any class
        # fallback: this is the case the removed backward-compatibility claim got wrong.
        bad = copy.deepcopy(prof)
        bad["failure"]["code"] = "TOOLCHAIN_STORE_UNAVAILABLE"
        expect_invalid(rm_id, bad, "future code inside a known class reaching an older receiver")
        bad = copy.deepcopy(prof)
        bad["failure"]["class"] = "toolchain"
        expect_invalid(rm_id, bad, "unknown failure class")
        bad = copy.deepcopy(prof)
        bad["failure"]["class"] = "environment"
        bad["failure"]["code"] = "PROFILE_QUALIFICATION_FAILED"
        expect_invalid(rm_id, bad, "unknown class carrying a registered code")
        bad = copy.deepcopy(prof)
        bad["outcome"] = "certified"
        expect_invalid(rm_id, bad, "invalid failure result presented as certified")
    # the failure classification stays closed: no test may pass by relaxing an enum
    if rm_id in by_id:
        fdefs = by_id[rm_id]["$defs"]["failure"]
        for field in ("class", "code"):
            spec = fdefs["properties"][field]
            if set(spec) - {"enum"} or not spec.get("enum"):
                fail(f"result-manifest failure.{field} is no longer a closed enum: {sorted(spec)}")
            else:
                count("closed_enum_checks")
        for branch in fdefs["allOf"]:
            per = branch["then"]["properties"]["code"]
            if set(per) - {"enum"} or not per.get("enum"):
                fail(f"result-manifest per-class code constraint is no longer a closed enum: {sorted(per)}")
        count("closed_enum_checks")

    # the DD-03 failure-class table and the result-manifest schema agree
    dd03 = root / "docs" / "design" / "dd-03-execution.md"
    if dd03.exists() and rm_id in by_id:
        table_classes: dict[str, set[str]] = {}
        for line in dd03.read_text(encoding="utf-8").splitlines():
            if not line.startswith("| `"):
                continue
            cells = line.strip().strip("|").split("|")
            if len(cells) != 3:
                continue
            names = re.findall(r"`([a-z][a-z-]*)`", cells[0])
            codes = set(re.findall(r"`([A-Z][A-Z_]*)`", cells[2]))
            if len(names) == 1 and codes:
                table_classes[names[0]] = codes
        fdefs = by_id[rm_id]["$defs"]["failure"]
        schema_classes = set(fdefs["properties"]["class"]["enum"])
        if set(table_classes) != schema_classes:
            fail(
                "dd-03-execution.md failure-class table and result-manifest schema disagree on classes: "
                f"{sorted(set(table_classes) ^ schema_classes)}"
            )
        all_codes = set(fdefs["properties"]["code"]["enum"])
        union = set().union(*table_classes.values()) if table_classes else set()
        if union != all_codes:
            fail(f"failure codes documented and registered differ: {sorted(union ^ all_codes)}")
        for branch in fdefs["allOf"]:
            cls = branch["if"]["properties"]["class"]["const"]
            per = set(branch["then"]["properties"]["code"]["enum"])
            if table_classes.get(cls) != per:
                fail(f"failure class {cls!r}: table codes {sorted(table_classes.get(cls, []))} != schema {sorted(per)}")
        if table_classes and set(table_classes) == schema_classes and union == all_codes:
            count("failure_class_agreement")

    # telemetry: operation trace fixture
    trace = get("telemetry/operation-log-trace.example.json")
    log_id = "urn:anvilkit:log-record:v1"
    if trace is not None:
        records = trace.get("records", [])
        for i, r in enumerate(records):
            expect_valid(log_id, r, f"operation-log-trace record {i} ({r.get('eventName')})")
            if not TIMESTAMP_RE.match(str(r.get("timestamp"))):
                fail(f"operation-log-trace record {i}: timestamp not UTC Z: {r.get('timestamp')}")
            if r.get("operationId") != trace.get("operationId"):
                fail(f"operation-log-trace record {i}: operationId differs from fixture operationId")
        # transport attempts of the same command are distinct
        attempts: dict[tuple, set] = {}
        for r in records:
            if r.get("eventName") == "rpc.client.completed" and "commandId" in r:
                key = (r["service.name"], r["commandId"], r["routeTemplate"])
                s = attempts.setdefault(key, set())
                if r["transportAttempt"] in s:
                    fail(f"operation-log-trace: duplicate transportAttempt for {key}")
                s.add(r["transportAttempt"])
        # every model call start has a terminal record; interrupted has a reconciliation
        starts = {r["callId"] for r in records if r.get("eventName") == "model.call.started"}
        ends = {r["callId"]: r["eventName"] for r in records if r.get("eventName") in ("model.call.completed", "model.call.interrupted", "model.call.canceled")}
        reconciled = {r["callId"] for r in records if r.get("eventName") == "model.call.reconciled"}
        for c in sorted(starts):
            if c not in ends:
                fail(f"operation-log-trace: model call {c} has no terminal record")
            elif ends[c] == "model.call.interrupted" and c not in reconciled:
                fail(f"operation-log-trace: interrupted call {c} has no reconciliation record")
        for c in sorted(ends):
            if c not in starts:
                fail(f"operation-log-trace: model call {c} ends without a start record")
        # every started long call has a completion by (service, requestId, routeTemplate)
        for kind in ("client", "server"):
            st = {(r["service.name"], r.get("requestId"), r["routeTemplate"]) for r in records if r.get("eventName") == f"rpc.{kind}.started"}
            done = {(r["service.name"], r.get("requestId"), r["routeTemplate"]) for r in records if r.get("eventName") == f"rpc.{kind}.completed"}
            for k in sorted(st, key=str):
                if k not in done:
                    fail(f"operation-log-trace: rpc.{kind}.started without completion for {k}")
        # a failed transport attempt is followed by a later successful attempt of the same command
        for key, s in attempts.items():
            if len(s) > 1:
                count("retried_commands")
        # no record anywhere carries a candidate body, and none claims the removed origin
        for i, r in enumerate(records):
            if "body" in r:
                fail(f"operation-log-trace record {i}: a record carries a body field")
            if r.get("origin") == "candidate":
                fail(f"operation-log-trace record {i}: origin=candidate is no longer a representation")
        # negative variants derived from real records
        svc = next(r for r in records if r.get("eventName") == "rpc.server.completed")
        bad = dict(svc); bad["body"] = "text"
        expect_invalid(log_id, bad, "service record with candidate body")
        bad = dict(svc); bad["eventName"] = "rpc.server.finished"
        expect_invalid(log_id, bad, "unregistered eventName")
        bad = dict(svc); bad["extra"] = 1
        expect_invalid(log_id, bad, "unknown top-level key")
        bad = dict(svc); bad["outcome"] = "error"; bad.pop("error.code", None); bad.pop("error.type", None)
        expect_invalid(log_id, bad, "failure outcome without error.code")
        bad = dict(svc); bad["traceId"] = "not-hex"
        expect_invalid(log_id, bad, "malformed traceId")
        bad = dict(svc); bad["timestamp"] = "2026-09-08T10:15:00+02:00"
        if TIMESTAMP_RE.match(bad["timestamp"]):
            fail("timestamp regex accepted a non-UTC offset")
        else:
            count("negative_fixtures_rejected")
        cli = next(r for r in records if r.get("eventName") == "rpc.client.completed")
        bad = dict(cli); bad.pop("transportAttempt")
        expect_invalid(log_id, bad, "client completion without transportAttempt")
        bad = dict(cli); bad["trace.source"] = "linked"
        expect_invalid(log_id, bad, "linked trace source without link.traceId")
        cand = next(r for r in records if r.get("eventName") == "candidate.output.observed")
        for k, v in (("tenantId", "team-1"), ("traceId", "4bf92f3577b34da6a3ce929d0e0e4736"), ("callId", "call-forged"), ("requestId", "req-forged"), ("message", "x")):
            bad = dict(cand); bad[k] = v
            expect_invalid(log_id, bad, f"candidate-output summary with {k}")
        bad = dict(cand); bad["severity"] = "DEBUG"
        expect_invalid(log_id, bad, "candidate-output summary with severity DEBUG")
        bad = dict(cand); bad["eventName"] = "admission.decided"
        expect_invalid(log_id, bad, "candidate-output summary impersonating a trusted event")
        bad = dict(cand); bad["service.name"] = "anvilkit-agent-control"
        expect_invalid(log_id, bad, "candidate-output summary claiming a service identity")
        bad = dict(cand); bad["origin"] = "candidate"
        expect_invalid(log_id, bad, "record claiming the removed candidate origin")
        bad = dict(cand); bad["body"] = "A" * 64
        expect_invalid(log_id, bad, "candidate-output summary reintroducing a body")
        svc2 = dict(svc); svc2["attributes"] = {f"k{i}": i for i in range(33)}
        expect_invalid(log_id, svc2, "attributes over 32 keys")
        svc2 = dict(svc); svc2["attributes"] = {"nested": {"a": 1}}
        expect_invalid(log_id, svc2, "nested attributes")

    # telemetry: hostile candidate output fixture. The property under test is containment:
    # no shared-log record may carry candidate content in any field, and no relocation of
    # that content into another field may be accepted. The checks below are structural --
    # they never try to recognise "source" or "a prompt" by pattern.
    cand_doc = get("telemetry/candidate-output.example.json")
    if cand_doc is not None and log_id in by_id:
        schema = by_id[log_id]
        # 1. the contract offers no free-text field a candidate can reach
        if "body" in schema["properties"]:
            fail("log-record schema still defines a body field")
        else:
            count("containment_checks")
        if "candidate" in schema["properties"]["origin"]["enum"]:
            fail("log-record schema still offers origin=candidate")
        else:
            count("containment_checks")
        if "candidate.output" in schema["$defs"]["eventName"]["enum"]:
            fail("log-record schema still registers the per-line candidate.output event")
        else:
            count("containment_checks")

        lines = cand_doc.get("hostileLines", [])
        expected = cand_doc.get("expectedRecords", [])
        forbidden = cand_doc.get("forbiddenRecords", [])
        tokens = cand_doc.get("contentTokens", [])
        cap = int(cand_doc.get("artifactLineCapBytes", 2048))
        labels = cand_doc.get("podLabels", {})

        # 2. the inputs are actually hostile: the fixture cannot be made to pass by deleting them
        if len(lines) < 6:
            fail(f"candidate-output: only {len(lines)} hostile lines; the fixture must keep its inputs")
        raw = [l.get("line", "") for l in lines]
        if not any(len(x.encode("utf-8")) > cap for x in raw):
            fail("candidate-output: no hostile line exceeds the artifact line cap")
        if not any(chr(27) in x for x in raw):
            fail("candidate-output: no hostile line carries control bytes")
        if not any("://" in x for x in raw):
            fail("candidate-output: no hostile line carries a capability-shaped URL")
        if not any(x.lstrip().startswith("{") for x in raw):
            fail("candidate-output: no hostile line forges a structured record")
        for tok in tokens:
            if not any(tok in x for x in raw):
                fail(f"candidate-output: declared content token {tok!r} occurs in no hostile line")
        if tokens and lines:
            count("containment_checks")

        # 3. the expected projection validates and is derived from the input, not carrying it
        for i, rec in enumerate(expected):
            expect_valid(log_id, rec, f"candidate-output expected record {i}")
            if rec.get("operationId") != labels.get("anvilkit.dev/operation-id") or \
               rec.get("attemptId") != labels.get("anvilkit.dev/attempt-id") or \
               rec.get("instanceId") != labels.get("anvilkit.dev/instance-id") or \
               rec.get("jobKind") != labels.get("anvilkit.dev/job-kind"):
                fail(f"candidate-output record {i}: correlation fields differ from trusted Pod labels")
        by_stream: dict[str, list[str]] = {}
        for l in lines:
            by_stream.setdefault(l.get("stream", "stdout"), []).append(l.get("line", ""))
        for i, rec in enumerate(expected):
            if rec.get("eventName") != "candidate.output.observed":
                continue
            a = rec.get("attributes", {})
            src = by_stream.get(a.get("stream"), [])
            sizes = [len(x.encode("utf-8")) for x in src]
            if a.get("lines") != len(src) or a.get("bytes") != sum(sizes):
                fail(f"candidate-output record {i}: summary counts do not match the observed {a.get('stream')} stream")
            if "maxLineBytes" in a and a["maxLineBytes"] != (max(sizes) if sizes else 0):
                fail(f"candidate-output record {i}: maxLineBytes does not match the observed stream")
        total_lines, total_bytes = len(raw), sum(len(x.encode("utf-8")) for x in raw)
        for i, rec in enumerate(expected):
            if rec.get("eventName") != "candidate.diagnostics.stored":
                continue
            a = rec.get("attributes", {})
            if a.get("lines") != total_lines or a.get("bytes") != total_bytes:
                fail(f"candidate-output record {i}: diagnostics counts do not match the whole observed output")
            ref = rec.get("diagnosticsRef", {})
            extra = set(ref) - {"artifactClass", "refId", "sizeBytes", "contentDigest"}
            if extra:
                fail(f"candidate-output record {i}: diagnosticsRef carries {sorted(extra)}")

        # 4. containment: no 16-byte window of any hostile line survives into the projection,
        #    and no declared content token appears anywhere in it
        windows = set()
        for x in raw:
            for j in range(0, max(0, len(x) - 15)):
                windows.add(x[j:j + 16])
        def strings(node, key=None):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield from strings(v, k)
            elif isinstance(node, list):
                for v in node:
                    yield from strings(v, key)
            elif isinstance(node, str):
                yield key, node
        leaked = 0
        for i, rec in enumerate(expected):
            for key, s in strings(rec):
                for tok in tokens:
                    if tok in s:
                        fail(f"candidate-output record {i}: field {key!r} carries content token {tok!r}")
                        leaked += 1
                for j in range(0, max(0, len(s) - 15)):
                    if s[j:j + 16] in windows:
                        fail(f"candidate-output record {i}: field {key!r} carries a 16-byte window of candidate output")
                        leaked += 1
                        break
        if not leaked:
            count("containment_checks")

        # 5. every relocation of the content into another field is rejected by the contract
        if len(forbidden) < 10:
            fail(f"candidate-output: only {len(forbidden)} forbidden variants; the negative set must stay complete")
        for entry in forbidden:
            expect_invalid(log_id, entry.get("record"), f"candidate-output forbidden: {entry.get('label')}")

    # event catalog in the operations document agrees with the schema enum
    ops = root / "docs" / "architecture" / "plans" / "operations.md"
    if ops.exists() and log_id in by_id:
        text = ops.read_text(encoding="utf-8")
        start = text.find("### Severity rules and event catalog")
        end = text.find("\n### ", start + 10)
        section = text[start:end] if start >= 0 else ""
        names: set[str] = set()
        for line in section.splitlines():
            if line.startswith("| `") and "|" in line[2:]:
                first = line.split("|")[1]
                names.update(re.findall(r"`([a-z][a-z0-9.]*)`", first))
        names = {n for n in names if "." in n}
        enum = set(by_id[log_id]["$defs"]["eventName"]["enum"])
        for n in sorted(names - enum):
            fail(f"operations.md event catalog names {n!r} which the log-record schema does not register")
        for n in sorted(enum - names):
            fail(f"log-record schema registers {n!r} which the operations.md catalog does not list")
        if names == enum:
            count("catalog_agreement")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    args = ap.parse_args()
    root = pathlib.Path(args.root).resolve()
    md = [root / n for n in ("README.md", "AGENTS.md", "CLAUDE.md") if (root / n).exists()]
    md += sorted((root / "docs").rglob("*.md"))
    check_markdown(root, md)
    check_contracts(root)
    for w in WARNINGS:
        print("WARN", w)
    for f in FAILURES:
        print("FAIL", f)
    print("counts:", json.dumps(COUNTS, sort_keys=True))
    print(f"{len(md)} markdown files, {len(FAILURES)} failures, {len(WARNINGS)} warnings")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
