#!/usr/bin/env python3
"""Read-only check of the project memory files (AGENTS.md section 11).

Usage: python3 tools/check-memory.py [--root PATH] [--self-test]

Scope: docs/SESSION_SUMMARY.md, docs/HANDOFF.md, docs/BLOCKERS.md, docs/DECISIONS.md,
docs/ROADMAP.md and the Compose procedures under docs/operations/compose/. These files
are local-only (docs/ is gitignored): without docs/ the run ends UNEXECUTED (exit 2).
tools/check-docs.py does not cover the five memory files; this script does, with the
same Markdown rules (balanced fences, table widths, resolvable links and anchors,
imported from check-docs.py) plus the memory rules below.

Memory rules (each failure names the file and the rule):
  header     the second non-empty line is `Last Updated: <UTC minute>` with at most one
             short parenthesis, at most 240 characters: a replaced stamp, never an
             appended diary (AGENTS.md 11.3).
  budget     SESSION_SUMMARY and HANDOFF at most 16000 bytes, ROADMAP at most 20000,
             BLOCKERS' active part (before `# Resolution History`) at most 24000. Over
             budget means: condense at the owner or move history to its evidence record
             or docs/archive/memory/; never drop a constraint to fit.
  resume     SESSION_SUMMARY has one `# Resume` section with the labels Objective,
             Authorized scope, Stopping point, Uncommitted work, External operations,
             Pending checks, and a `## Verified state` table.
  next       SESSION_SUMMARY has exactly one `# Next Action` heading, the explicit
             anchor `next-action` once, and the labels Action, Owner, Prerequisites,
             Completion condition.
  handoff    HANDOFF links SESSION_SUMMARY.md#next-action and holds no next action of
             its own (no `**Action:**` line, no `# Next Action` heading).
  decisions  every `# DEC-nnn` record has a Status line (PROPOSED, ACTIVE, SUPERSEDED,
             REVERTED) and one row in the index table with the same status; the index
             has no row without a record.
  blockers   every `# B-nnn` record has a Status line (OPEN, IN_PROGRESS, RESOLVED,
             CLOSED); OPEN/IN_PROGRESS records come before `# Resolution History`,
             RESOLVED/CLOSED after it; the active index lists exactly the active ones.
  roadmap    every `## M<n>` milestone has a Status line.

Exit status: 0 PASS, 1 FAIL, 2 UNEXECUTED (docs/ absent or the script cannot run).
This script never writes files and never contacts a service.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import re
import sys
import tempfile

MEMORY_FILES = (
    "docs/SESSION_SUMMARY.md",
    "docs/HANDOFF.md",
    "docs/BLOCKERS.md",
    "docs/DECISIONS.md",
    "docs/ROADMAP.md",
)
BUDGETS = {
    "docs/SESSION_SUMMARY.md": 16000,
    "docs/HANDOFF.md": 16000,
    "docs/ROADMAP.md": 20000,
}
BLOCKERS_ACTIVE_BUDGET = 24000
HEADER_MAX = 240
HEADER_RE = re.compile(r"^Last Updated: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z( \([^()]{1,160}\))?$")
RESUME_LABELS = ("Objective", "Authorized scope", "Stopping point", "Uncommitted work", "External operations", "Pending checks")
NEXT_LABELS = ("Action", "Owner", "Prerequisites", "Completion condition")
DEC_STATUSES = {"PROPOSED", "ACTIVE", "SUPERSEDED", "REVERTED"}
B_STATUSES = {"OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"}
ACTIVE_B = {"OPEN", "IN_PROGRESS"}


def load_check_docs(tools_dir: pathlib.Path):
    spec = importlib.util.spec_from_file_location("check_docs", tools_dir / "check-docs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def second_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[1] if len(lines) > 1 else ""


def record_statuses(text: str, prefix: str) -> list[tuple[str, str, int]]:
    """(identifier, status, line) for every `# <prefix>-nnn` record; status '' when missing."""
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^# ({prefix}-\d+)\s*$", line)
        if not m:
            continue
        status = ""
        for j in range(i + 1, min(i + 8, len(lines))):
            if lines[j].strip() == "Status:" and j + 1 < len(lines):
                status = lines[j + 1].strip()
                break
        out.append((m.group(1), status, i + 1))
    return out


def check_memory(root: pathlib.Path, mod) -> list[str]:
    failures: list[str] = []
    texts: dict[str, str] = {}
    for rel in MEMORY_FILES:
        p = root / rel
        if not p.is_file():
            failures.append(f"{rel}: missing required memory file")
            continue
        texts[rel] = p.read_text(encoding="utf-8")
    if failures:
        return failures

    files = [root / rel for rel in MEMORY_FILES]
    compose_dir = root / "docs" / "operations" / "compose"
    files += sorted(compose_dir.glob("*.md")) if compose_dir.is_dir() else []
    md_failures, _ = mod.check_markdown(root, files)
    failures += md_failures

    for rel, text in texts.items():
        header = second_line(text)
        if not HEADER_RE.match(header) or len(header) > HEADER_MAX:
            failures.append(f"{rel}: header rule: line 2 must be `Last Updated: <UTC minute>` (one short parenthesis at most, {HEADER_MAX} characters at most), not an appended diary")
        size = len(text.encode("utf-8"))
        if rel in BUDGETS and size > BUDGETS[rel]:
            failures.append(f"{rel}: budget rule: {size} bytes exceeds {BUDGETS[rel]}; condense at the owner or move history to its evidence record or docs/archive/memory/")

    ss = texts["docs/SESSION_SUMMARY.md"]
    if len(re.findall(r"^# Resume\s*$", ss, flags=re.M)) != 1:
        failures.append("docs/SESSION_SUMMARY.md: resume rule: exactly one `# Resume` section")
    for label in RESUME_LABELS:
        if f"**{label}:**" not in ss:
            failures.append(f"docs/SESSION_SUMMARY.md: resume rule: missing label `**{label}:**`")
    if not re.search(r"^## Verified state\s*$", ss, flags=re.M):
        failures.append("docs/SESSION_SUMMARY.md: resume rule: missing `## Verified state` table")
    if len(re.findall(r"^# Next Action\s*$", ss, flags=re.M)) != 1:
        failures.append("docs/SESSION_SUMMARY.md: next rule: exactly one `# Next Action` heading")
    if ss.count('<a id="next-action"></a>') != 1:
        failures.append("docs/SESSION_SUMMARY.md: next rule: the explicit anchor `next-action` must appear once")
    for label in NEXT_LABELS:
        if not re.search(rf"^\*\*{re.escape(label)}[^*]*:\*\*", ss, flags=re.M):
            failures.append(f"docs/SESSION_SUMMARY.md: next rule: missing label `**{label}:**`")

    ho = texts["docs/HANDOFF.md"]
    if "SESSION_SUMMARY.md#next-action" not in ho:
        failures.append("docs/HANDOFF.md: handoff rule: must link SESSION_SUMMARY.md#next-action")
    if re.search(r"^\*\*Action:\*\*", ho, flags=re.M) or re.search(r"^# Next Action\s*$", ho, flags=re.M):
        failures.append("docs/HANDOFF.md: handoff rule: holds a next action of its own; SESSION_SUMMARY owns it")

    de = texts["docs/DECISIONS.md"]
    records = record_statuses(de, "DEC")
    index_rows = {m.group(1): m.group(2).strip() for m in re.finditer(r"^\|\s*(DEC-\d+)\s*\|[^|]*\|[^|]*\|\s*([A-Z_]+)\s*\|", de, flags=re.M)}
    for ident, status, line in records:
        if status not in DEC_STATUSES:
            failures.append(f"docs/DECISIONS.md:{line}: decisions rule: {ident} status {status!r} is not one of {sorted(DEC_STATUSES)}")
        if ident not in index_rows:
            failures.append(f"docs/DECISIONS.md: decisions rule: {ident} has no row in the index table")
        elif index_rows[ident] != status:
            failures.append(f"docs/DECISIONS.md: decisions rule: index says {ident} {index_rows[ident]}, the record says {status}")
    for ident in sorted(set(index_rows) - {r[0] for r in records}):
        failures.append(f"docs/DECISIONS.md: decisions rule: index row {ident} has no record")

    bl = texts["docs/BLOCKERS.md"]
    history_at = bl.find("\n# Resolution History")
    if history_at < 0:
        failures.append("docs/BLOCKERS.md: blockers rule: missing `# Resolution History` section")
        history_line = len(bl.splitlines()) + 1
    else:
        history_line = bl[:history_at].count("\n") + 2
    active_listed = set(re.findall(r"^- \[(B-\d+)\]\(#b-\d+\)", bl, flags=re.M))
    active_records = set()
    for ident, status, line in record_statuses(bl, "B"):
        if status not in B_STATUSES:
            failures.append(f"docs/BLOCKERS.md:{line}: blockers rule: {ident} status {status!r} is not one of {sorted(B_STATUSES)}")
            continue
        if status in ACTIVE_B:
            active_records.add(ident)
            if line > history_line:
                failures.append(f"docs/BLOCKERS.md:{line}: blockers rule: active {ident} sits under Resolution History")
        elif line < history_line:
            failures.append(f"docs/BLOCKERS.md:{line}: blockers rule: {status} {ident} sits above Resolution History")
    for ident in sorted(active_records - active_listed):
        failures.append(f"docs/BLOCKERS.md: blockers rule: active {ident} is not in the Active Blockers index")
    for ident in sorted(active_listed - active_records):
        failures.append(f"docs/BLOCKERS.md: blockers rule: the Active Blockers index lists {ident}, which is not OPEN or IN_PROGRESS")
    active_size = len(bl[:history_at].encode("utf-8")) if history_at >= 0 else len(bl.encode("utf-8"))
    if active_size > BLOCKERS_ACTIVE_BUDGET:
        failures.append(f"docs/BLOCKERS.md: budget rule: the active part is {active_size} bytes, over {BLOCKERS_ACTIVE_BUDGET}; move resolved history below Resolution History or condense")

    rm = texts["docs/ROADMAP.md"]
    lines = rm.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^## M\d+\b", line):
            window = "\n".join(lines[i + 1:i + 6])
            if "Status:" not in window:
                failures.append(f"docs/ROADMAP.md:{i + 1}: roadmap rule: milestone without a Status line")
    return failures


def self_test(mod) -> int:
    """Minimal passing tree, then one broken variant per rule; prints PASS/FAIL per case."""
    ok_files = {
        "docs/SESSION_SUMMARY.md": "# SESSION SUMMARY\n\nLast Updated: 2026-10-31T16:00Z\n\n# Resume\n\n**Objective:** x\n**Authorized scope:** x\n**Stopping point:** x\n**Uncommitted work:** none\n**External operations:** none\n**Pending checks:** none\n\n## Verified state\n\n| Check | Inputs | Result | Invalidated by |\n|---|---|---|---|\n| a | b | c | d |\n\n<a id=\"next-action\"></a>\n\n# Next Action\n\n**Action:** x\n\n**Owner:** o\n\n**Prerequisites:** none\n\n**Completion condition:** c\n",
        "docs/HANDOFF.md": "# HANDOFF\n\nLast Updated: 2026-10-31T16:00Z (scope)\n\nSee [next action](SESSION_SUMMARY.md#next-action).\n",
        "docs/BLOCKERS.md": "# BLOCKERS\n\nLast Updated: 2026-10-31T16:00Z\n\n# Active Blockers\n\n- [B-002](#b-002): x\n\n# B-002\n\nStatus:\nOPEN\n\n# Resolution History\n\n# B-001\n\nStatus:\nRESOLVED\n",
        "docs/DECISIONS.md": "# DECISIONS\n\nLast Updated: 2026-10-31T16:00Z\n\n| ID | Date | Title | Status |\n|---|---|---|---|\n| DEC-001 | 2026-09-14 | x | ACTIVE |\n\n# DEC-001\n\nStatus:\nACTIVE\n",
        "docs/ROADMAP.md": "# ROADMAP\n\nLast Updated: 2026-10-31T16:00Z\n\n## M1\n\nStatus:\nDONE\n",
    }
    variants = [
        ("header diary", "docs/ROADMAP.md", ("Last Updated: 2026-10-31T16:00Z", "Last Updated: 2026-10-31T16:00Z (a; b; c, then more " + "x" * 300 + ")"), "header rule"),
        ("budget", "docs/HANDOFF.md", ("See", "x" * 16100 + "\nSee"), "budget rule"),
        ("resume label", "docs/SESSION_SUMMARY.md", ("**Pending checks:** none", ""), "resume rule"),
        ("two next actions", "docs/SESSION_SUMMARY.md", ("# Next Action\n", "# Next Action\n\n# Next Action\n"), "next rule"),
        ("handoff action", "docs/HANDOFF.md", ("See", "**Action:** do\n\nSee"), "handoff rule"),
        ("decision index mismatch", "docs/DECISIONS.md", ("| ACTIVE |", "| SUPERSEDED |"), "decisions rule"),
        ("resolved blocker above history", "docs/BLOCKERS.md", ("Status:\nOPEN", "Status:\nRESOLVED"), "blockers rule"),
        ("milestone status", "docs/ROADMAP.md", ("Status:\nDONE\n", ""), "roadmap rule"),
        ("broken link", "docs/HANDOFF.md", ("SESSION_SUMMARY.md#next-action", "SESSION_SUMMARY.md#nowhere"), "missing anchor"),
    ]
    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "docs").mkdir()
        for rel, text in ok_files.items():
            (root / rel).write_text(text, encoding="utf-8")
        results.append(("the minimal tree passes", check_memory(root, mod) == []))
        for name, rel, (old, new), expect in variants:
            (root / rel).write_text(ok_files[rel].replace(old, new, 1), encoding="utf-8")
            found = check_memory(root, mod)
            results.append((f"{name} is refused ({expect})", any(expect in f for f in found)))
            (root / rel).write_text(ok_files[rel], encoding="utf-8")
    for name, ok in results:
        print(f"{'PASS' if ok else 'FAIL'}  self-test: {name}")
    return 0 if all(ok for _, ok in results) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    root = pathlib.Path(args.root).resolve()
    mod = load_check_docs(pathlib.Path(__file__).resolve().parent)
    if args.self_test:
        return self_test(mod)
    if not (root / "docs").is_dir():
        print("UNEXECUTED: docs/ is not in this tree (the memory files are local-only); nothing was checked")
        return 2
    failures = check_memory(root, mod)
    for f in failures:
        print("FAIL", f)
    print(f"project memory: {len(MEMORY_FILES)} memory files, {len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
