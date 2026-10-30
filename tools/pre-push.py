#!/usr/bin/env python3
"""Pre-push gate of the parent repository (DEC-029): the commit being pushed must pass the
static units that most often turned the parent's CI red after a gitlink commit.

  .local/verification-venv/bin/python tools/pre-push.py <remote> <url> < refs   # as Git's pre-push hook
  .local/verification-venv/bin/python tools/pre-push.py --commit REV             # check one commit by hand
  .local/verification-venv/bin/python tools/pre-push.py --self-test

Install once per checkout (remove the file to uninstall; `git push --no-verify` skips it):

  printf '#!/bin/sh\\nexec "$(git rev-parse --show-toplevel)/.local/verification-venv/bin/python" -I "$(git rev-parse --show-toplevel)/tools/pre-push.py" "$@"\\n' > .git/hooks/pre-push
  chmod +x .git/hooks/pre-push

For every commit the push sends to a branch or tag, the commit is materialized as CI's clean
recursive clone sees it: the parent's committed tree plus each replacement repository's
committed tree at the gitlink that commit records (the export set of
tools/check-source-export.py, whose functions this reuses). Two units of the static chain then
run inside that export:
  docs   tools/check-docs.py, which includes tools/docs-status.py --check (stale generated
         status rows in README.md and AGENTS.md). Exit 2 (docs/ is local-only, never exported)
         is not a failure, as in CI.
  evals  tools/run-evaluation.py --check (the registry's file:line citations)
Only committed bytes count: regenerated rows left uncommitted in the working tree do not make a
stale commit pass. A replacement repository whose recorded commit is missing locally fails.
The gate never writes outside a temporary directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
ZERO = re.compile(r"^0+$")

_spec = importlib.util.spec_from_file_location("check_source_export", ROOT / "tools/check-source-export.py")
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)


def git(*args: str, cwd: pathlib.Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def materialize(commit: str, target: pathlib.Path) -> tuple[pathlib.Path, list[str]]:
    """Extract the commit's export set under target; returns the docs-status metadata file
    (outside the exported tree, as tools/check-source-export.py records it) and failures."""
    _, gitlinks = export.committed(ROOT, commit)
    failures: list[str] = []
    tree = target / "tree"
    export.extract(ROOT, commit, tree)
    repos: dict[str, str] = {}
    for rel in export.REPLACEMENT_REPOS:
        sub = gitlinks.pop(rel, None)
        if sub is None:
            failures.append(f"{rel}: not recorded as a gitlink in {commit[:12]} (absent from a clean clone)")
            continue
        if not (ROOT / rel / ".git").exists():
            failures.append(f"{rel}: not mounted in this checkout, so {sub[:12]} cannot be exported")
            continue
        try:
            export.extract(ROOT / rel, sub, tree / rel)
        except subprocess.CalledProcessError:
            failures.append(f"{rel}: the recorded commit {sub[:12]} is not in the local repository (fetch or commit it first)")
            continue
        repos[rel] = sub
    for legacy in gitlinks:
        shutil.rmtree(tree / legacy, ignore_errors=True)
    tags: dict[str, list[str]] = {}
    for line in git("for-each-ref", "--format=%(objectname) %(*objectname) %(refname:short)", "refs/tags/go/", cwd=ROOT / "contracts").splitlines():
        parts = line.split()
        tags.setdefault(parts[1] if len(parts) == 3 else parts[0], []).append(parts[-1])
    meta = target / "docs-status-metadata.json"
    meta.write_text(json.dumps({"gitlinks": dict(sorted(repos.items())), "contractsTags": tags}))
    return meta, failures


def units(tree: pathlib.Path, meta: pathlib.Path) -> list[tuple[str, str, str]]:
    """(name, PASS or FAIL, detail) of the docs and evals units run inside the export."""
    env = dict(os.environ, ANVILKIT_DOCS_STATUS_METADATA=str(meta))
    out = []
    for name, argv in (("docs", [sys.executable, "tools/check-docs.py"]), ("evals", [sys.executable, "tools/run-evaluation.py", "--check"])):
        p = subprocess.run(argv, cwd=tree, env=env, capture_output=True, text=True)
        text = (p.stdout + p.stderr).strip()
        # check-docs ends 2 (UNEXECUTED) whenever the local-only docs/ is absent, as in every
        # export and in CI; its root-document findings still end it with 1.
        status = "PASS" if p.returncode == 0 or (name == "docs" and p.returncode == 2) else "FAIL"
        detail = "\n".join(ln for ln in text.splitlines() if ln.startswith("FAIL") or "drift" in ln)[-2000:] or text[-1500:]
        out.append((name, status, detail if status == "FAIL" else ""))
    return out


def check_commit(commit: str) -> list[str]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="anvilkit-pre-push-") as tmp:
        meta, failures = materialize(commit, pathlib.Path(tmp))
        if not failures:
            for name, status, detail in units(pathlib.Path(tmp) / "tree", meta):
                print(f"{status:<5} {name:<6} {commit[:12]}")
                if status == "FAIL":
                    failures.append(f"{name}: {detail}")
    for f in failures:
        print(f"FAIL  {commit[:12]}: {f}")
    print(f"pre-push: {commit[:12]} checked in {time.monotonic() - started:.1f}s, {len(failures)} failures")
    return failures


def pushed_commits(lines: list[str]) -> list[str]:
    """The distinct commits a pre-push input sends (deletions skipped, tags peeled)."""
    commits: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or ZERO.match(parts[1]):
            continue
        commit = git("rev-parse", f"{parts[1]}^{{commit}}")
        if commit not in commits:
            commits.append(commit)
    return commits


def self_test() -> int:
    """HEAD's export passes; a drifted status row and a moved registry citation each fail."""
    head = git("rev-parse", "HEAD")
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="anvilkit-pre-push-selftest-") as tmp:
        meta, failures = materialize(head, pathlib.Path(tmp))
        tree = pathlib.Path(tmp) / "tree"
        base = {n: s for n, s, _ in units(tree, meta)}
        if failures or base != {"docs": "PASS", "evals": "PASS"}:
            errors.append(f"baseline: HEAD {head[:12]} must pass ({failures or base})")
        readme = tree / "README.md"
        text = readme.read_text()
        row = re.search(r"^\| `contracts` \| `[^`]+` \| `([0-9a-f]{7})` \|$", text, re.M)
        if not row:
            errors.append("README.md: no contracts row in the generated repositories block")
        else:
            readme.write_text(text[:row.start(1)] + "0000000" + text[row.end(1):])
            if dict((n, s) for n, s, _ in units(tree, meta))["docs"] != "FAIL":
                errors.append("a stale gitlink row in README.md did not fail the docs unit")
            readme.write_text(text)
        registry = tree / "tests/evals/registry.json"
        reg = registry.read_text()
        cite = re.search(r'"file": "([^"]+\.go):(\d+)"', reg)
        if not cite:
            errors.append("tests/evals/registry.json: no Go file:line citation")
        else:
            registry.write_text(reg[:cite.start(2)] + str(int(cite.group(2)) + 1) + reg[cite.end(2):])
            if dict((n, s) for n, s, _ in units(tree, meta))["evals"] != "FAIL":
                errors.append(f"a moved citation ({cite.group(1)}) did not fail the evals unit")
    for e in errors:
        print("FAIL", e)
    print(f"pre-push self-test: {'PASS' if not errors else 'FAIL'} (baseline HEAD {head[:12]}, 2 negatives)")
    return 1 if errors else 0


def main() -> int:
    # Git exports GIT_DIR, GIT_WORK_TREE and similar to a hook (githooks(5)); left in place they
    # would point the Git commands run in the submodules and contracts/ at the parent's repository.
    for name in subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True).stdout.split():
        os.environ.pop(name, None)
    sys.stdout.reconfigure(line_buffering=True)  # Git shows a hook's output on stderr; keep the order
    args = sys.argv[1:]
    if args == ["--self-test"]:
        return self_test()
    if len(args) == 2 and args[0] == "--commit":
        return 1 if check_commit(git("rev-parse", f"{args[1]}^{{commit}}")) else 0
    commits = pushed_commits(sys.stdin.read().splitlines())
    failed = [c for c in commits if check_commit(c)]
    if failed:
        print("pre-push: push refused. Regenerate the rows with tools/docs-status.py (or fix the cited lines), commit, "
              "and push again; `git push --no-verify` skips this gate.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
