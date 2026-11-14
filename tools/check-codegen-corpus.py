#!/usr/bin/env python3
"""Codegen task corpus check (RB-A15): tests/evals/codegen/cases.json.

  .local/verification-venv/bin/python tools/check-codegen-corpus.py [--self-test]

delivery.md ("Agent and RAG quality corpus") requires each codegen case to carry a
taskId, an input digest, versioned expectations, a budget, the allowed tools and its
scoring rules, and the corpus to be stratified by component category, dependency
complexity, Chinese-language requirements, answerability, malicious source material,
authorization boundaries and tool effects. This check holds the corpus to that:

  * every case has those fields; taskIds are unique and well formed;
  * the input digest is sha256 of the brief's canonical JSON (sorted keys, no
    whitespace, UTF-8) and the brief carries the brief fields the team reads;
  * every stratum dimension is declared, every case names a declared value for each,
    and every declared value is used by some case;
  * the allowed tools are a subset of the team's tool enum (read from
    jobs/codegen/team/src/round.ts, so the corpus cannot drift from the team);
  * the budget is positive and within the codegen-team-v1 deadline and the team's
    aggregate bounds (contracts/jobs/profiles.json, jobs/codegen/team/team.yaml);
  * the expectation is versioned, its outcome is `certified` or `refused:<CODE>`,
    and the primary score is independent acceptance (a self-review never certifies).

The corpus is DEVELOPMENT_ONLY until the business corpus (ENV-05) exists; trials need
the codegen Job and a model route and are NOT_RUN here. --self-test applies known
defects to a copy and requires each to be reported. Exit 0 PASS, 1 findings.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pyenv_check  # noqa: E402

pyenv_check.require("yaml")
import yaml  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = ROOT / "tests/evals/codegen/cases.json"
ROUND = ROOT / "jobs/codegen/team/src/round.ts"
TEAM = ROOT / "jobs/codegen/team/team.yaml"
PROFILES = ROOT / "contracts/jobs/profiles.json"
PROFILE = "codegen-team-v1"
DIMENSIONS = ("componentCategory", "dependencyComplexity", "language", "answerability", "sourceMaterial", "authorization", "toolEffects")
BRIEF_FIELDS = ("schemaVersion", "componentId", "puckType", "packageName", "version", "requirements", "sourceRevision")


def canonical_digest(obj) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def team_tools() -> set[str]:
    m = re.search(r"tools:\s*\{[^}]*enum:\s*\[([^\]]*)\]", ROUND.read_text(encoding="utf-8"))
    if not m:
        # Exit 2 (UNEXECUTED, the chain's contract), not SystemExit(str), which exits 1 (FAIL).
        print(f"UNEXECUTED: the tool enum is not found in {ROUND.relative_to(ROOT)}", file=sys.stderr)
        sys.exit(2)
    return set(re.findall(r'"([a-z]+)"', m[1]))


def bounds() -> dict:
    team = yaml.safe_load(TEAM.read_text(encoding="utf-8"))
    profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
    items = profiles["profiles"] if isinstance(profiles, dict) else profiles
    profile = next(p for p in items if p.get("profileId") == PROFILE)
    return {"wallSeconds": profile["deadlineSeconds"], "modelCalls": team["aggregate"]["maxCalls"], "exposureMicros": int(team["aggregate"]["exposure"])}


def check(doc: dict, briefs: dict[str, dict], tools: set[str], limits: dict) -> list[str]:
    findings: list[str] = []
    f = findings.append
    if doc.get("schemaVersion") != 1:
        f("schemaVersion is not 1")
    if doc.get("status") != "DEVELOPMENT_ONLY":
        f("status is not DEVELOPMENT_ONLY (the business corpus is ENV-05)")
    if not (isinstance(doc.get("k"), int) and doc["k"] >= 2):
        f("k (trials per case) is not an integer of at least 2: pass^k needs repeats")
    strata = doc.get("strata") or {}
    for d in DIMENSIONS:
        if not (isinstance(strata.get(d), list) and strata[d]):
            f(f"stratum dimension {d} is not declared")
    for d in sorted(set(strata) - set(DIMENSIONS)):
        f(f"stratum dimension {d} is not one of delivery.md's")
    used = {d: set() for d in DIMENSIONS}
    seen = set()
    for c in doc.get("cases") or []:
        tid = c.get("taskId", "")
        where = f"case {tid or '?'}"
        if not re.fullmatch(r"cg-[a-z0-9]+(?:-[a-z0-9]+)*", tid):
            f(f"{where}: taskId is not cg-<words>")
        if tid in seen:
            f(f"{where}: taskId is not unique")
        seen.add(tid)
        st = c.get("stratum") or {}
        for d in DIMENSIONS:
            v = st.get(d)
            if v not in (strata.get(d) or []):
                f(f"{where}: stratum {d} {v!r} is not a declared value")
            else:
                used[d].add(v)
        inp = c.get("input") or {}
        brief = briefs.get(inp.get("brief", ""))
        if brief is None:
            f(f"{where}: brief {inp.get('brief')!r} is missing")
        else:
            missing = [k for k in BRIEF_FIELDS if k not in brief]
            if missing:
                f(f"{where}: the brief lacks {missing}")
            if inp.get("digest") != canonical_digest(brief):
                f(f"{where}: input digest {inp.get('digest')} is not the brief's ({canonical_digest(brief)})")
        exp = c.get("expectations") or {}
        if not (isinstance(exp.get("version"), str) and exp["version"]):
            f(f"{where}: expectations carry no version")
        if not re.fullmatch(r"certified|refused:[A-Z][A-Z_]+", str(exp.get("outcome", ""))):
            f(f"{where}: outcome {exp.get('outcome')!r} is not certified or refused:<CODE>")
        if not (isinstance(exp.get("behavioral"), list) and exp["behavioral"]):
            f(f"{where}: no behavioral expectations")
        b = c.get("budget") or {}
        for key, limit in limits.items():
            v = b.get(key)
            if not (isinstance(v, int) and 0 < v <= limit):
                f(f"{where}: budget {key} {v!r} is not a positive integer within {limit}")
        allowed = c.get("allowedTools")
        if not (isinstance(allowed, list) and allowed):
            f(f"{where}: no allowed tools")
        elif set(allowed) - tools:
            f(f"{where}: allowed tools {sorted(set(allowed) - tools)} are not the team's tools {sorted(tools)}")
        sc = c.get("scoring") or {}
        if sc.get("primary") != "independent-acceptance":
            f(f"{where}: the primary score is {sc.get('primary')!r}, not independent-acceptance")
        if not (isinstance(sc.get("rules"), list) and sc["rules"]):
            f(f"{where}: no scoring rules")
    if not seen:
        f("the corpus has no case")
    for d in DIMENSIONS:
        for v in sorted(set(strata.get(d) or []) - used[d]):
            f(f"stratum {d} value {v!r} is declared but no case covers it")
    return findings


def load() -> tuple[dict, dict[str, dict]]:
    doc = json.loads(CORPUS.read_text(encoding="utf-8"))
    briefs = {}
    for c in doc.get("cases") or []:
        rel = (c.get("input") or {}).get("brief", "")
        p = CORPUS.parent / rel
        if rel and p.is_file():
            briefs[rel] = json.loads(p.read_text(encoding="utf-8"))
    return doc, briefs


def self_test(doc: dict, briefs: dict, tools: set[str], limits: dict) -> list[str]:
    def first(d):
        return d["cases"][0]

    probes = {
        "a case without a taskId pattern": lambda d, b: first(d).update(taskId="Hero"),
        "a duplicate taskId": lambda d, b: d["cases"][1].update(taskId=first(d)["taskId"]),
        "a changed brief under the same digest": lambda d, b: b[first(d)["input"]["brief"]].update(requirements="something else"),
        "a stratum value not declared": lambda d, b: first(d)["stratum"].update(language="fr"),
        "a declared value no case covers": lambda d, b: d["strata"]["language"].append("ja"),
        "a dimension missing": lambda d, b: d["strata"].pop("toolEffects"),
        "a tool the team does not have": lambda d, b: first(d)["allowedTools"].append("bash"),
        "a budget above the team's aggregate": lambda d, b: first(d)["budget"].update(modelCalls=limits["modelCalls"] + 1),
        "an unversioned expectation": lambda d, b: first(d)["expectations"].pop("version"),
        "an outcome outside the vocabulary": lambda d, b: first(d)["expectations"].update(outcome="looks good"),
        "self-review as the primary score": lambda d, b: first(d)["scoring"].update(primary="code-reviewer-approval"),
        "a single trial per case": lambda d, b: d.update(k=1),
        "status claims more than development": lambda d, b: d.update(status="QUALIFIED"),
    }
    failures = []
    base = check(doc, briefs, tools, limits)
    print(f"  baseline {'passes' if not base else 'FAILS: ' + base[0]}")
    if base:
        failures.append("baseline")
    for label, mutate in probes.items():
        d, b = copy.deepcopy(doc), copy.deepcopy(briefs)
        mutate(d, b)
        found = check(d, b, tools, limits)
        print(f"  probe {label:<40} {'detected: ' + found[0] if found else 'NOT DETECTED'}")
        if not found:
            failures.append(label)
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    doc, briefs = load()
    tools, limits = team_tools(), bounds()
    findings = check(doc, briefs, tools, limits)
    cases = doc.get("cases") or []
    print(f"codegen corpus ({CORPUS.relative_to(ROOT)}, {doc.get('status')}): {len(cases)} cases, k={doc.get('k')}; "
          f"tools {sorted(tools)}; budget bounds {limits}")
    for d in DIMENSIONS:
        print(f"  {d:<21} {', '.join(doc.get('strata', {}).get(d) or [])}")
    if a.self_test:
        print("self-test:")
        if self_test(doc, briefs, tools, limits):
            findings.append("self-test: a known defect was not detected")
    if findings:
        print("FINDINGS:")
        print("\n".join(f"  {x}" for x in findings))
        return 1
    print("codegen corpus satisfies its schema (DEVELOPMENT_ONLY; trials NOT_RUN: they need the codegen Job and a model route)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
