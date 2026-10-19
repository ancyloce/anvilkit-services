#!/usr/bin/env python3
"""Generated status blocks of the root documents (P3.8, plan 0004).

  .local/verification-venv/bin/python tools/docs-status.py            # rewrite the blocks
  .local/verification-venv/bin/python tools/docs-status.py --check    # exit 1 on drift
  .local/verification-venv/bin/python tools/docs-status.py --self-test

Each block sits between `<!-- generated:NAME -->` and `<!-- /generated:NAME -->` and
is derived from the checkout, never written by hand:

  units        README.md: every unit of delivery.md's "Unit status records" with the
               leading status of its record (IMPLEMENTED, VERIFIED locally, ...)
  repositories README.md and AGENTS.md: the parent's submodules (.gitmodules) with the
               gitlink the parent's HEAD records, and the contracts version the
               consumers require
  registry     delivery.md: the evaluation registry's revision and counts
  contracts    each component README whose manifest pins the contracts (go.mod's
               require, package.json's @anvilkit/generated-clients commit): that pin

tools/check-docs.py runs --check and fails on drift; a component README that cites
another contracts version than its manifest pins also fails there (contract_citations).
The documents under docs/ are local-only: without docs/architecture/delivery.md (a clone)
the units and registry blocks, which derive from or live in it, are left as they are and
not checked; the repositories and contracts blocks still are.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACTS_GO = "github.com/ancyloce/anvilkit-agent-contracts/go"
BLOCK = re.compile(r"(<!-- generated:(?P<name>[a-z]+) -->\n)(?P<body>.*?)(<!-- /generated:(?P=name) -->)", re.S)
UNIT = re.compile(r"^\| (P\d\d[ab]?|P\d\.\d|P24) \| (.+?) \|")
# Components whose README cites the contracts they pin (the parent's submodules and modules).
COMPONENTS = ["services/agent/api", "services/agent/control", "services/agent/workflow", "services/agent/mcp", "services/agent/knowledge",
              "services/agent/model-proxy", "services/agent/background-worker", "services/agent/inference", "jobs/parser", "jobs/validator",
              "jobs/codegen/supervisor", "jobs/codegen/team", "jobs/shared/access-sidecar"]


def git(*args: str, cwd: pathlib.Path = ROOT) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def metadata() -> dict | None:
    """The Git facts of a checkout without Git metadata (the disposable export of
    tools/check-source-export.py): {"gitlinks": {path: commit}, "contractsTags":
    {commit: [tag, ...]}}, recorded from the source repositories; None in a checkout."""
    path = os.environ.get("ANVILKIT_DOCS_STATUS_METADATA")
    return json.loads(pathlib.Path(path).read_text()) if path else None


def status_of(cell: str) -> str:
    """The leading status of a unit record: up to the first parenthesis, semicolon,
    colon or sentence end, without markdown emphasis."""
    head = re.split(r" \(|; |: |\. ", cell.replace("**", ""), maxsplit=1)[0].strip()
    return head[:80]


def units(delivery: str) -> list[tuple[str, str]]:
    start = delivery.find('<a id="unit-status-records"></a>')
    if start < 0:
        return []
    out, seen = [], set()
    for line in delivery[start:].splitlines()[2:]:
        if line.startswith("#") and out:
            break
        m = UNIT.match(line)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(1), status_of(m.group(2))))
    return out


def contracts_tag(commit: str) -> str:
    """The go/v* tag at a contracts commit (the contracts checkout's tags), or ''."""
    meta = metadata()
    if meta is not None:
        tags = next((t for c, t in meta["contractsTags"].items() if c.startswith(commit) or commit.startswith(c)), [])
    else:
        tags = git("tag", "--points-at", commit, cwd=ROOT / "contracts").split()
    return sorted(t for t in tags if t.startswith("go/v"))[-1] if any(t.startswith("go/v") for t in tags) else ""


def pin(component: str) -> dict | None:
    """What the component's manifest pins of the contracts: the Go module version
    or the TypeScript package's commit."""
    d = ROOT / component
    gomod = d / "go.mod"
    if gomod.exists():
        m = re.search(re.escape(CONTRACTS_GO) + r" (v\S+)", gomod.read_text())
        if m:
            return {"kind": "go", "version": m.group(1), "manifest": "go.mod"}
    pkg = d / "package.json"
    if pkg.exists():
        spec = json.loads(pkg.read_text()).get("dependencies", {}).get("@anvilkit/generated-clients", "")
        m = re.search(r"#([0-9a-f]{7,40})", spec)
        if m:
            return {"kind": "ts", "commit": m.group(1)[:7], "tag": contracts_tag(m.group(1)), "manifest": "package.json"}
    return None


def contracts_line(p: dict) -> str:
    if p["kind"] == "go":
        tag = "go/" + p["version"] if re.fullmatch(r"v\d+\.\d+\.\d+", p["version"]) else ""
        return f"Contracts: this build requires `{CONTRACTS_GO} {p['version']}` (`{p['manifest']}`{'; tag `' + tag + '`' if tag else ''}).\n"
    return (f"Contracts: this build pins `@anvilkit/generated-clients` to contracts commit `{p['commit']}` "
            f"(`{p['manifest']}`{'; tag `' + p['tag'] + '`' if p['tag'] else ''}).\n")


def submodules() -> list[tuple[str, str, str]]:
    raw = git("config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.(path|url)$")
    paths, urls = {}, {}
    for line in raw.splitlines():
        key, value = line.split(None, 1)
        name = key[len("submodule."):].rsplit(".", 1)[0]
        (paths if key.endswith(".path") else urls)[name] = value
    rows = []
    meta = metadata()
    for name, path in sorted(paths.items(), key=lambda kv: kv[1]):
        if meta is not None:
            sha = meta["gitlinks"].get(path, "")[:7] or "(not in HEAD)"
        else:
            entry = git("ls-tree", "HEAD", path).split()
            sha = entry[2][:7] if len(entry) >= 3 and entry[1] == "commit" else "(not in HEAD)"
        repo = urls.get(name, "").rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
        rows.append((path, repo, sha))
    return rows


def consumer_versions() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for c in COMPONENTS:
        p = pin(c)
        if p:
            if p["kind"] == "go":
                key = "go/" + p["version"] if re.fullmatch(r"v\d+\.\d+\.\d+", p["version"]) else p["version"]
            else:
                key = p["tag"] or "commit " + p["commit"]
            out.setdefault(key, []).append(c)
    return out


def blocks() -> dict[str, dict[str, str]]:
    """{file: {block name: body}} for every generated block (without the local-only
    delivery.md: only the blocks that do not derive from it)."""
    delivery_path = ROOT / "docs/architecture/delivery.md"
    local_only = not delivery_path.is_file()
    units_body = ""
    if not local_only:
        unit_rows = units(delivery_path.read_text())
        t = ["| Unit | Status of its record |", "| --- | --- |"] + [f"| {u} | {s} |" for u, s in unit_rows]
        units_body = ("Generated by `tools/docs-status.py` from the [unit status records](docs/architecture/delivery.md#unit-status-records); "
                      "the record is the authority, this is its first clause.\n\n" + "\n".join(t) + "\n")
    subs = submodules()
    versions = consumer_versions()
    repo_rows = ["| Path | Repository | Gitlink in the parent's `HEAD` |", "| --- | --- | --- |"] + [f"| `{p}` | `{r}` | `{s}` |" for p, r, s in subs]
    consumers = "; ".join(f"`{v}` ({', '.join(f'`{c}`' for c in cs)})" for v, cs in sorted(versions.items()))
    repos_body = ("Generated by `tools/docs-status.py` from `.gitmodules` and the parent's committed tree; the working tree may be ahead "
                  "(`git status --short` and each repository's own `git log` are the authority).\n\n" + "\n".join(repo_rows)
                  + f"\n\nContract consumers pin: {consumers}.\n")
    reg = json.loads((ROOT / "tests/evals/registry.json").read_text())
    named = sum(len(v) for v in (reg.get("assertions") or {}).values())
    registry_body = (f"Evaluation registry (generated by `tools/docs-status.py` from `tests/evals/registry.json`): revision "
                     f"{reg['registryRevision']}, {len(reg['suites'])} suites, {len(reg['cases'])} cases, {len(reg.get('notRun', []))} NOT_RUN entries, "
                     f"{named} named assertions over {len(reg.get('assertions') or {})} requirements.\n")
    out: dict[str, dict[str, str]] = {"README.md": {"repositories": repos_body},
                                       "AGENTS.md": {"repositories": repos_body}}
    if not local_only:
        out["README.md"]["units"] = units_body
        out["docs/architecture/delivery.md"] = {"registry": registry_body}
    for c in COMPONENTS:
        p = pin(c)
        if p and (ROOT / c / "README.md").exists():
            out[f"{c}/README.md"] = {"contracts": contracts_line(p)}
    return out


def apply(text: str, wanted: dict[str, str]) -> tuple[str, list[str]]:
    """Replace each named block's body; names a block that is missing."""
    missing = [n for n in wanted if f"<!-- generated:{n} -->" not in text]

    def sub(m: re.Match) -> str:
        name = m.group("name")
        return m.group(1) + wanted[name] + m.group(4) if name in wanted else m.group(0)
    return BLOCK.sub(sub, text), missing


def contract_citations(component: str, readme: str) -> list[str]:
    """Contracts versions a component README cites that its manifest does not pin."""
    p = pin(component)
    if not p:
        return []
    allowed = {p.get("version", ""), "go/" + p.get("version", ""), p.get("tag", ""), p.get("commit", "")} - {""}
    bad = []
    for m in re.finditer(r"go/v\d+\.\d+\.\d+|anvilkit-agent-contracts/go v\S+?(?=[`\s])|anvilkit-agent-contracts#([0-9a-f]{7,40})", readme):
        token = m.group(0)
        version = token.split()[-1] if " v" in token else (m.group(1)[:7] if m.group(1) else token)
        if version not in allowed:
            bad.append(f"{component}/README.md cites {token!r}; {p['manifest']} pins {p.get('version') or p.get('commit')}")
    return bad


def check() -> list[str]:
    errs = []
    for rel, wanted in blocks().items():
        path = ROOT / rel
        text = path.read_text()
        new, missing = apply(text, wanted)
        errs += [f"{rel}: the generated block {n!r} is missing (tools/docs-status.py writes it between its markers)" for n in missing]
        if new != text:
            errs.append(f"{rel}: a generated block drifted from the checkout (run tools/docs-status.py)")
    for c in COMPONENTS:
        readme = ROOT / c / "README.md"
        if readme.exists():
            errs += contract_citations(c, readme.read_text())
    return errs


def write() -> int:
    changed = []
    for rel, wanted in blocks().items():
        path = ROOT / rel
        text = path.read_text()
        new, missing = apply(text, wanted)
        for n in missing:
            print(f"{rel}: no markers for {n!r}; add `<!-- generated:{n} -->` and `<!-- /generated:{n} -->` where it belongs")
        if new != text:
            path.write_text(new)
            changed.append(rel)
    print("rewritten: " + (", ".join(changed) if changed else "nothing (up to date)"))
    return 0


def self_test() -> int:
    cases = []
    text = "a\n<!-- generated:x -->\nold\n<!-- /generated:x -->\nb\n"
    new, missing = apply(text, {"x": "new\n"})
    cases.append(("a block's body is replaced between its markers", new == "a\n<!-- generated:x -->\nnew\n<!-- /generated:x -->\nb\n" and not missing))
    cases.append(("a missing block is named", apply("plain", {"x": "y"})[1] == ["x"]))
    cases.append(("a record's status is its first clause", status_of("IMPLEMENTED (twelfth pass, 2026-09-16; verified)") == "IMPLEMENTED"
                  and status_of("VERIFIED locally") == "VERIFIED locally" and status_of("**FAIL**: 14 of 17") == "FAIL"))
    sample = '<a id="unit-status-records"></a>\n\n### Unit status records\n\n| Unit | Status |\n| --- | --- |\n| P01 | IMPLEMENTED |\n| P0.1 | IMPLEMENTED on the foundation (x) |\n\n## Next\n| P99 | X |\n'
    cases.append(("units are read from the records table only", units(sample) == [("P01", "IMPLEMENTED"), ("P0.1", "IMPLEMENTED on the foundation")]))
    delivery = ROOT / "docs/architecture/delivery.md"
    if delivery.is_file():
        real = units(delivery.read_text())
        cases.append(("the real records table yields P01..P24 and the P0 units", {"P01", "P12", "P24", "P0.1"} <= {u for u, _ in real}))
    else:
        print("UNEXECUTED  self-test: the real records table (docs/ is local-only and not in this tree)")
    api = pin("services/agent/api")
    cases.append(("a Go component's pin comes from go.mod", bool(api) and api["kind"] == "go" and api["version"].startswith("v")))
    if api:
        stale = f"requires `{CONTRACTS_GO} v0.0.1` (tag `go/v0.0.1`)"
        cases.append(("a README citing another version is refused", len(contract_citations("services/agent/api", stale)) == 2))
        cases.append(("a README citing the pinned version passes", contract_citations("services/agent/api", contracts_line(api)) == []))
    for name, ok in cases:
        print(f"{'PASS' if ok else 'FAIL'}  self-test: {name}")
    failed = [n for n, ok in cases if not ok]
    print(f"docs-status self-test: {len(cases) - len(failed)}/{len(cases)} PASS")
    return 1 if failed else 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    if sys.argv[1:] == ["--check"]:
        errs = check()
        for e in errs:
            print("FAIL", e)
        print(f"docs-status: {len(errs)} drift finding(s)")
        return 1 if errs else 0
    if sys.argv[1:]:
        print(__doc__)
        return 2
    return write()


if __name__ == "__main__":
    sys.exit(main())
