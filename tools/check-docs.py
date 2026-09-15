#!/usr/bin/env python3
"""Read-only documentation check for the current architecture baseline.

Usage: python3 tools/check-docs.py [--root PATH] [--include-archive]

Scope: README.md, AGENTS.md, CLAUDE.md, docs/README.md and every Markdown file under
docs/architecture/. docs/archive/ is historical and is skipped unless --include-archive
is given; its results are then printed under a separate heading and never affect the
exit status.

Checks:
  1. Every required input exists. A missing required document fails the run; nothing is
     skipped silently.
  2. Fenced code blocks are balanced; every table row has the header's column count.
  3. Every relative link resolves to a file and every fragment resolves to an explicit
     `<a id>` or a generated heading anchor.

Contract sources are checked by tools/check-contracts.py. This script never writes files
and never contacts a service; it establishes nothing about runtime behavior.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

REQUIRED_INPUTS = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/README.md",
    "docs/architecture/architecture.md",
    "docs/architecture/requirements.md",
    "docs/architecture/technology.md",
    "docs/architecture/contracts.md",
    "docs/architecture/execution.md",
    "docs/architecture/components.md",
    "docs/architecture/knowledge.md",
    "docs/architecture/mcp.md",
    "docs/architecture/platform.md",
    "docs/architecture/security.md",
    "docs/architecture/delivery.md",
)

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
    h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    h = h.replace("`", "").replace("*", "").replace("_", "")
    h = h.strip().lower()
    h = re.sub(r"[^\w\s-]", "", h, flags=re.UNICODE)
    return re.sub(r"\s", "-", h)


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


def check_markdown(root: pathlib.Path, files: list[pathlib.Path]) -> tuple[list[str], dict[str, int]]:
    failures: list[str] = []
    counts = {"files": len(files), "tables": 0, "links": 0}
    texts = {p: p.read_text(encoding="utf-8") for p in files}
    anchors = {p: anchors_for(t) for p, t in texts.items()}
    for p, t in texts.items():
        rel = p.relative_to(root)
        ids = EXPLICIT_ANCHOR_RE.findall(t)
        for d in sorted({i for i in ids if ids.count(i) > 1}):
            failures.append(f"{rel}: duplicate explicit anchor id {d!r}")
        fences = sum(1 for line in t.splitlines() if FENCE_RE.match(line))
        if fences % 2:
            failures.append(f"{rel}: unbalanced code fences ({fences})")
        body = strip_fences(t).splitlines()
        i = 0
        while i < len(body):
            if body[i].lstrip().startswith("|"):
                block = []
                while i < len(body) and body[i].lstrip().startswith("|"):
                    block.append((i + 1, body[i]))
                    i += 1
                if len(block) >= 2:
                    counts["tables"] += 1
                    widths = [(ln, len(CODE_SPAN_RE.sub("code", row.strip()).strip("|").split("|"))) for ln, row in block]
                    header = widths[0][1]
                    for ln, w in widths[1:]:
                        if w != header:
                            failures.append(f"{rel}:{ln}: table row has {w} cells, header has {header}")
            else:
                i += 1
        for m in LINK_RE.finditer(strip_fences(CODE_SPAN_RE.sub("", t))):
            target = m.group(2)
            counts["links"] += 1
            if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                continue
            path_part, _, frag = target.partition("#")
            if path_part:
                dest = (p.parent / path_part).resolve()
                if not dest.exists():
                    failures.append(f"{rel}: broken link target {target!r}")
                    continue
                if frag and dest.suffix.lower() == ".md":
                    dest_anchors = anchors.get(dest) or anchors_for(dest.read_text(encoding="utf-8"))
                    if frag not in dest_anchors:
                        failures.append(f"{rel}: missing anchor {frag!r} in {dest.relative_to(root)}")
            elif frag and frag not in anchors[p]:
                failures.append(f"{rel}: missing in-file anchor {frag!r}")
    return failures, counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    ap.add_argument("--include-archive", action="store_true", help="also report docs/archive/ (informational)")
    args = ap.parse_args()
    root = pathlib.Path(args.root).resolve()

    missing = [n for n in REQUIRED_INPUTS if not (root / n).is_file()]
    for n in missing:
        print(f"FAIL missing required input {n}")
    if missing:
        print(f"{len(missing)} required inputs missing; the current baseline cannot be checked")
        return 1

    current = [root / n for n in ("README.md", "AGENTS.md", "CLAUDE.md", "docs/README.md")]
    current += sorted((root / "docs" / "architecture").glob("*.md"))
    failures, counts = check_markdown(root, current)
    for f in failures:
        print("FAIL", f)
    print(f"current baseline: {counts['files']} files, {counts['tables']} tables, {counts['links']} links, {len(failures)} failures")

    if args.include_archive:
        archive = sorted((root / "docs" / "archive").rglob("*.md"))
        arch_failures, arch_counts = check_markdown(root, archive) if archive else ([], {"files": 0, "tables": 0, "links": 0})
        print(f"docs/archive (informational, not part of the exit status): {arch_counts['files']} files, {len(arch_failures)} findings")
        for f in arch_failures[:40]:
            print("ARCHIVE", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
