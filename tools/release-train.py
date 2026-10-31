#!/usr/bin/env python3
"""The release train (DEC-035). Once a Job image change is on main with green CI, one command,
started by the owner, runs the rest of the release path. Each step waits for the previous step's
checks, and the train stops at the first red.

  .local/verification-venv/bin/python tools/release-train.py plan [--images "IMAGE ..."] [--at REV] [--out DIR]
  .local/verification-venv/bin/python tools/release-train.py run DIR      # the owner, in a terminal
  .local/verification-venv/bin/python tools/release-train.py --self-test  # offline
  .local/verification-venv/bin/python tools/release-train.py --replay     # read-only Git; network for the bumps

plan is read-only (git ls-remote, the GitHub API, local objects). It picks the next free tags,
reports the preconditions and writes DIR/plan.json (default outputs/release-train/<release tag>/).
run executes the plan's steps in order:
  jobs       tag jobs-vX (on REV, default origin/main), dispatch job-images.yml for the images, wait,
             and take the digests from the SIGNED job-images manifest of the tag's release;
  team       only when the validator is among the images: re-pin jobs/codegen/team/Dockerfile to the
             new validator, commit and push the team, wait for its ci; commit the team gitlink and the
             status rows in the parent, push, wait for ci. A second jobs-v tag then publishes the team
             image alone (job-images.yml: the team image is based on the validator it pins);
  contracts  re-pin jobs/profiles.json and its Go and TypeScript copies (each profile whose image or
             sidecar image was republished: the new digest, revision + 1), commit, push, wait for the
             contracts ci, tag go/vY, wait until proxy.golang.org serves it;
  consumers  Control and Workflow require go/vY, Knowledge pins the re-pinned commit (with its README
             lines as tools/docs-status.py writes them); commit and push each, wait for each ci;
  parent     re-pin the admission chart and the policy fixtures, commit them with the new gitlinks,
             regenerate and commit the status rows, push (the pre-push gate runs), wait for ci and
             supply-chain;
  release    tag vZ on that commit, wait for the release workflow, take its SIGNED manifest;
  pin        write the Compose image pin with deploy/compose/images.sh, commit it alone, push, wait for
             the pin workflow (DEC-034).
Without --images the train is a service release: release and pin only. The train stops before any
deployment, which still needs the owner's authorization (DEC-009, DEC-015).

Every step first reads the remote state and skips what is already done. After a stop, fix the cause
(for a failed workflow run, e.g. `gh run rerun <id> --failed`) and run the same command again: the
train continues where it stopped. Progress and evidence: DIR/state.json, DIR/train.log and the
manifests under DIR.

run needs an interactive terminal and the typed release tag. Development agents prepare and check
plans but never run a train: every commit, tag and push stays the owner's (AGENTS.md section 4,
DEC-008). Exit 0 done; 1 stopped or blocked; 2 usage.
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
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
CONTRACTS, TEAM, VALIDATOR_SOURCE = "contracts", "jobs/codegen/team", "jobs/validator"
CONTRACTS_GO = "github.com/ancyloce/anvilkit-agent-contracts/go"
VALIDATOR, TEAM_IMAGE, SUPERVISOR = "anvilkit-job-validator", "anvilkit-codegen-team", "anvilkit-codegen"
JOB_IMAGES = (VALIDATOR, "anvilkit-job-access-sidecar", SUPERVISOR, "anvilkit-parser", TEAM_IMAGE)
PROFILE_COPIES = ("jobs/profiles.json", "go/jobschema/profiles.json", "ts/src/jobs/profiles.json")
PARENT_REPIN = ("deploy/policies/chart/templates", "deploy/policies/tests/resources")
# The consumers of the Job profiles, bumped after every re-pin (BLOCKERS.md B-002 steps 6 and 9).
CONSUMERS = {"services/agent/control": "go", "services/agent/workflow": "go", "services/agent/knowledge": "pnpm"}
PIN, PIN_GENERATOR = "deploy/compose/images.env", "deploy/compose/images.sh"
ROWS = ("README.md", "AGENTS.md")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
VALIDATOR_FROM = re.compile(r"^FROM \$\{VALIDATOR_REPOSITORY\}@(sha256:[0-9a-f]{64})$", re.M)
GENERATED_CLIENTS = re.compile(r'("@anvilkit/generated-clients": "github:ancyloce/anvilkit-agent-contracts#)([0-9a-f]{40})(&path:ts")')
FIXTURE_REVISION = r'(profileId\\?":\\?"{pid}\\?",\\?"profileRevision\\?":\\?")\d+(\\?")'
# Neutralise the caller's Go settings: the sums must come from the public proxy and checksum database.
GO_ENV = {"GOWORK": "off", "GOFLAGS": "", "GOPROXY": "https://proxy.golang.org", "GOSUMDB": "sum.golang.org",
          "GOPRIVATE": "", "GONOPROXY": "", "GONOSUMDB": "", "GOINSECURE": ""}
POLL = 30

_spec = importlib.util.spec_from_file_location("docs_status", ROOT / "tools/docs-status.py")
docs_status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(docs_status)

LOG: pathlib.Path | None = None


class Stop(Exception):
    """The train stops: a red check, a conflict or an unmet precondition. Fix it and run again."""


def utc(fmt: str = "%Y-%m-%dT%H:%M:%SZ") -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(fmt)


def say(msg: str) -> None:
    line = f"{utc()} {msg}"
    print(line, flush=True)
    if LOG:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


# --- Git and GitHub -------------------------------------------------------------------------

def run(argv: list[str], cwd: str | pathlib.Path = ".", stdin: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=ROOT / cwd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, **env} if env else None)


def git(*args: str, cwd: str = ".", stdin: str | None = None) -> str:
    p = run(["git", *args], cwd, stdin)
    if p.returncode != 0:
        raise Stop(f"git {' '.join(args)} ({cwd}): {(p.stderr or p.stdout).strip()[-400:]}")
    return p.stdout.strip()


def git_ok(*args: str, cwd: str = ".") -> bool:
    return run(["git", *args], cwd).returncode == 0


def show(cwd: str, rev: str, path: str) -> str | None:
    """A file's committed text, or None when the revision lacks it."""
    p = run(["git", "show", f"{rev}:{path}"], cwd)
    return p.stdout if p.returncode == 0 else None


def blob_at(cwd: str, rev: str, path: str) -> str:
    """The blob of path at rev ('' when absent). Committed files are compared by blob hash, never
    read: the Compose pin is an environment file (owner rule of 2026-10-25)."""
    p = run(["git", "rev-parse", "-q", "--verify", f"{rev}:{path}"], cwd)
    return p.stdout.strip() if p.returncode == 0 else ""


def blob_of(cwd: str, text: str) -> str:
    return git("hash-object", "--stdin", cwd=cwd, stdin=text)


def gitlink(rev: str, path: str) -> str | None:
    entry = git("ls-tree", rev, path).split()
    return entry[2] if len(entry) >= 3 and entry[1] == "commit" else None


def gh(*args: str, read: bool = False) -> str:
    """gh; a read is retried on errors (a transient API or network failure must not end a wait)."""
    for attempt in range(5 if read else 1):
        p = run(["gh", *args])
        if p.returncode == 0:
            return p.stdout
        if attempt < (4 if read else 0):
            time.sleep(10 * (attempt + 1))
    raise Stop(f"gh {' '.join(args[:4])}: {(p.stderr or p.stdout).strip()[-400:]}")


def repo_of(cwd: str) -> str:
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", git("remote", "get-url", "origin", cwd=cwd))
    if not m:
        raise Stop(f"{cwd}: origin is not a GitHub repository")
    return m.group(1)


def remote_refs(cwd: str, *patterns: str) -> dict[str, str]:
    """ref -> commit on origin; annotated tags peeled."""
    out: dict[str, str] = {}
    for line in git("ls-remote", "origin", *patterns, cwd=cwd).splitlines():
        sha, ref = line.split("\t")
        if ref.endswith("^{}"):
            out[ref[:-3]] = sha
        else:
            out.setdefault(ref, sha)
    return out


def tags(cwd: str, prefix: str) -> dict[str, str]:
    return {r[len("refs/tags/"):]: c for r, c in remote_refs(cwd, f"refs/tags/{prefix}*").items()}


def next_tag(prefix: str, existing: list[str]) -> str:
    """The patch release after the highest PREFIX<major>.<minor>.<patch> in existing."""
    versions = sorted(tuple(map(int, m.groups())) for t in existing
                      if (m := re.fullmatch(re.escape(prefix) + r"(\d+)\.(\d+)\.(\d+)", t)))
    if not versions:
        raise Stop(f"no {prefix}X.Y.Z tag to continue from")
    major, minor, patch = versions[-1]
    return f"{prefix}{major}.{minor}.{patch + 1}"


def latest_tag(prefix: str, existing: dict[str, str]) -> str:
    return max((t for t in existing if re.fullmatch(re.escape(prefix) + r"\d+\.\d+\.\d+", t)),
               key=lambda t: tuple(map(int, t[len(prefix):].split("."))))


def workflow_runs(repo: str, workflow: str, event: str, sha: str | None = None, branch: str | None = None) -> list[dict]:
    query = f"repos/{repo}/actions/workflows/{workflow}/runs?event={event}&per_page=20"
    query += f"&head_sha={sha}" if sha else ""
    query += f"&branch={branch}" if branch else ""
    return json.loads(gh("api", query, read=True))["workflow_runs"]


def green(repo: str, workflow: str, sha: str) -> bool:
    return any(r.get("conclusion") == "success" for r in workflow_runs(repo, workflow, "push", sha=sha))


def wait_run(repo: str, workflow: str, label: str, *, event: str = "push", sha: str | None = None,
             branch: str | None = None, timeout: int = 5400, hint: str = "") -> dict:
    """Wait for the newest run of workflow for sha and/or branch (a push to main: branch main, so a tag
    push's run of the same commit is not taken); a conclusion other than success stops."""
    start, last = time.monotonic(), None
    while True:
        found = workflow_runs(repo, workflow, event, sha, branch)
        current = max(found, key=lambda r: r["id"]) if found else None
        if current is None and time.monotonic() - start > 300:
            raise Stop(f"no {workflow} run for {label} in {repo} after 5 min{hint}")
        if current is not None:
            state = (current["status"], current["conclusion"], current.get("run_attempt"))
            if state != last:
                say(f"  {repo} {workflow} {current['id']} ({label}): {current['status']} {current['conclusion'] or ''}".rstrip())
                last = state
            if current["status"] == "completed":
                if current["conclusion"] == "success":
                    return current
                raise Stop(f"{repo} {workflow} run {current['html_url']} ({label}) ended {current['conclusion']}: fix the cause "
                           f"or rerun it (gh run rerun {current['id']} --failed -R {repo}), then run the train again")
        if time.monotonic() - start > timeout:
            raise Stop(f"{repo} {workflow} ({label}) not finished after {timeout // 60} min")
        time.sleep(POLL)


def ensure_tag(cwd: str, tag: str, commit: str) -> None:
    """An annotated tag on commit, pushed and visible on origin; an existing tag elsewhere stops."""
    ref = f"refs/tags/{tag}"
    # An exact ls-remote pattern omits the peeled line (ref^{}), so an annotated tag would read as its
    # tag object; the glob returns it, and the exact key is taken below.
    remote = remote_refs(cwd, ref + "*").get(ref)
    if remote:
        if remote != commit:
            raise Stop(f"{cwd}: tag {tag} exists on origin at {remote[:12]}, not {commit[:12]}")
        return
    local = run(["git", "rev-parse", "-q", "--verify", f"{ref}^{{commit}}"], cwd).stdout.strip()
    if local and local != commit:
        raise Stop(f"{cwd}: the local tag {tag} points at {local[:12]}, not {commit[:12]}; delete it first")
    if not local:
        git("tag", "-a", tag, "-m", tag, commit, cwd=cwd)
    git("push", "origin", ref, cwd=cwd)
    for _ in range(12):
        if remote_refs(cwd, ref + "*").get(ref) == commit:
            say(f"  {cwd}: tag {tag} on {commit[:12]} is on origin")
            return
        time.sleep(5)
    raise Stop(f"{cwd}: tag {tag} was pushed, but origin does not show it")


def commit(cwd: str, message: str, paths: list[str]) -> None:
    """git commit of the staged paths; a failed commit (signing, a hook) unstages them again, so a rerun
    does not stop on a staged index the owner did not make."""
    p = run(["git", "commit", "-m", message], cwd)
    if p.returncode != 0:
        run(["git", "reset", "-q", "--", *paths], cwd)
        raise Stop(f"{cwd}: git commit failed ({(p.stderr or p.stdout).strip()[-300:]}); the train unstaged {paths[:5]} "
                   "and leaves them modified; fix the cause and run again")


def ci_base(sha: str) -> str:
    """The nearest commit at or before sha that is not a pin-only commit: ci skips those (paths-ignore)."""
    for _ in range(20):
        if git("diff", "--name-only", f"{sha}^", sha).splitlines() != [PIN]:
            return sha
        sha = git("rev-parse", f"{sha}^")
    raise Stop(f"20 pin-only commits before {sha[:12]}")


def synced(cwd: str, allowed: set[str]) -> None:
    """The repository is on main at origin/main, with nothing staged and no tracked change outside allowed."""
    if git("branch", "--show-current", cwd=cwd) != "main":
        raise Stop(f"{cwd}: not on branch main")
    head, main = git("rev-parse", "HEAD", cwd=cwd), git("rev-parse", "origin/main", cwd=cwd)
    if head != main:
        raise Stop(f"{cwd}: HEAD {head[:12]} is not origin/main {main[:12]}; pull or push first")
    if git("diff", "--cached", "--name-only", cwd=cwd):
        raise Stop(f"{cwd}: staged changes; commit or unstage them first")
    dirty = set(git("diff", "--name-only", cwd=cwd).splitlines()) - allowed
    if dirty:
        raise Stop(f"{cwd}: modified tracked files {sorted(dirty)[:5]}; commit or restore them first")


def commit_and_push(cwd: str, files: dict[str, str], message: str, also: set[str] = frozenset()) -> str:
    """Commit files (path -> text) on main and push. Returns the pushed commit; skips what origin/main
    already has, and a local commit that only lacks its push is pushed."""
    git("fetch", "--quiet", "origin", cwd=cwd)
    main, head = git("rev-parse", "origin/main", cwd=cwd), git("rev-parse", "HEAD", cwd=cwd)
    blobs = {f: blob_of(cwd, t) for f, t in files.items()}
    if all(blob_at(cwd, main, f) == b for f, b in blobs.items()):
        return main
    pending = head != main and git_ok("merge-base", "--is-ancestor", main, head, cwd=cwd)
    if not (pending and all(blob_at(cwd, head, f) == b for f, b in blobs.items())
            and set(git("diff", "--name-only", main, head, cwd=cwd).splitlines()) <= set(files) | also):
        synced(cwd, set(files) | also)
        for f, t in files.items():
            (ROOT / cwd / f).write_text(t, encoding="utf-8")
        git("add", "--", *files, cwd=cwd)
        commit(cwd, message, list(files))
        head = git("rev-parse", "HEAD", cwd=cwd)
    git("push", "origin", "HEAD:main", cwd=cwd)
    say(f"  {cwd}: pushed {head[:12]} to main")
    return head


def parent_commit(gitlinks: dict[str, str], files: dict[str, str], message: str, train: str) -> str:
    """Commit the parent's gitlinks and files, then the regenerated status rows, and push both
    together (the pre-push gate and CI check only the pushed tip). Returns the pushed tip."""
    git("fetch", "--quiet", "origin")
    main, head = git("rev-parse", "origin/main"), git("rev-parse", "HEAD")
    blobs = {f: blob_of(".", t) for f, t in files.items()}

    def has(rev: str) -> bool:
        return all(gitlink(rev, p) == c for p, c in gitlinks.items()) and all(blob_at(".", rev, f) == b for f, b in blobs.items())
    if has(main):
        return main
    pending = head != main and git_ok("merge-base", "--is-ancestor", main, head) and has(head) \
        and set(git("diff", "--name-only", main, head).splitlines()) <= set(gitlinks) | set(files) | set(ROWS)
    if not pending:
        # README.md and AGENTS.md must be clean here: the rows commit below takes all of their changes.
        synced(".", set(gitlinks) | set(files))
        for path, sha in gitlinks.items():
            if git("rev-parse", "HEAD", cwd=path) != sha:
                raise Stop(f"{path}: checked out {git('rev-parse', 'HEAD', cwd=path)[:12]}, not {sha[:12]}")
        for f, t in files.items():
            (ROOT / f).write_text(t, encoding="utf-8")
        git("add", "--", *gitlinks, *files)
        commit(".", message, [*gitlinks, *files])
    p = run([sys.executable, "tools/docs-status.py"])
    if p.returncode != 0:
        raise Stop(f"tools/docs-status.py: {(p.stdout + p.stderr).strip()[-400:]}")
    if rows := git("diff", "--name-only", "--", *ROWS).splitlines():
        git("add", "--", *rows)
        commit(".", f"📦 chore: regenerate the status rows in README.md and AGENTS.md\n\n"
                    f"tools/docs-status.py after the gitlink commit (release train {train}, DEC-035).", rows)
    head = git("rev-parse", "HEAD")
    git("push", "origin", "HEAD:main")
    say(f"  parent: pushed {head[:12]} to main")
    return head


# --- Re-pin and bump edits (pure) --------------------------------------------------------------

def repin_profiles(text: str, digests: dict[str, str]) -> tuple[str, dict[str, str], dict[str, str]]:
    """profiles.json with every profile whose image or sidecar image is in digests re-pinned: the
    new digest and revision + 1. Returns (text, {old digest: new digest}, {profileId: revision}).
    A repository whose profiles pin more than one digest stops the train (which one to replace?)."""
    doc = json.loads(text)
    pins: dict[str, set[str]] = {}
    for p in doc["profiles"]:
        for key in ("image", "sidecarImage"):
            if (img := p.get(key)) and img["repository"] in digests:
                pins.setdefault(img["repository"], set()).add(img["digest"])
    replaced: dict[str, str] = {}
    for repository, new in digests.items():
        if not DIGEST.match(new):
            raise Stop(f"{repository}: {new!r} is not a sha256 digest")
        if len(pins.get(repository, ())) != 1:
            raise Stop(f"the profiles pin {repository} at {len(pins.get(repository, ()))} digests, not one; re-pin it by hand")
        (old,) = pins[repository]
        if old != new:
            replaced[old] = new
    revisions: dict[str, str] = {}
    for p in doc["profiles"]:
        moved = False
        for key in ("image", "sidecarImage"):
            if (img := p.get(key)) and img["repository"] in digests and img["digest"] in replaced:
                img["digest"], moved = replaced[img["digest"]], True
        if moved:
            p["revision"] = str(int(p["revision"]) + 1)
            revisions[p["profileId"]] = p["revision"]
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n", replaced, revisions


def repin_comment(line: str, revisions: dict[str, str], tag: str, date: str) -> str:
    for pid, revision in revisions.items():
        line = re.sub(rf"((?<![\w-]){re.escape(pid)}(?![\w-])[^\n]*?revision )\d+", rf"\g<1>{revision}", line)
    line = re.sub(r"jobs-v\d+\.\d+\.\d+", tag, line)
    return re.sub(r"since \d{4}-\d{2}-\d{2}", f"since {date}", line)


def repin_text(text: str, replaced: dict[str, str], revisions: dict[str, str], tag_of: dict[str, str], date: str) -> str:
    """A chart template or policy fixture re-pinned: each old digest replaced, the profileRevision of
    each re-pinned profile in launch envelopes (plain or JSON-escaped), and in the comment block
    above a chart variable whose digest changed, the revisions, the jobs-v tag and the 'since' date.
    tag_of maps each new digest to the jobs-v tag that published it."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        moved = [new for old, new in replaced.items() if old in line]
        if not moved:
            continue
        for old, new in replaced.items():
            line = line.replace(old, new)
        lines[i] = line
        if i > 0 and re.fullmatch(r"\s*- name: \w+", lines[i - 1]):
            k = i - 2
            while k >= 0 and lines[k].lstrip().startswith("#"):
                lines[k] = repin_comment(lines[k], revisions, tag_of[moved[-1]], date)
                k -= 1
    out = "\n".join(lines)
    for pid, revision in revisions.items():
        out = re.sub(FIXTURE_REVISION.format(pid=re.escape(pid)), rf"\g<1>{revision}\g<2>", out)
    return out


def repin_dockerfile(text: str, new: str, date: str, source: str, tag: str) -> str:
    """The team Dockerfile based on validator digest new, published by tag from validator commit source."""
    found = list(VALIDATOR_FROM.finditer(text))
    if len(found) != 1:
        raise Stop("jobs/codegen/team/Dockerfile: expected exactly one FROM ${VALIDATOR_REPOSITORY}@sha256:… line")
    if found[0].group(1) == new:
        return text  # already pinned: a rerun on a later day must not rewrite the comment's date
    text = text[:found[0].start(1)] + new + text[found[0].end(1):]
    text = re.sub(r"(# Pinned )\d{4}-\d{2}-\d{2}( to the validator)", rf"\g<1>{date}\g<2>", text)
    return re.sub(r"(anvilkit-job-validator )[0-9a-f]{7,40}( \(release )jobs-v\d+\.\d+\.\d+", rf"\g<1>{source[:7]}\g<2>{tag}", text)


def edit_gomod(text: str, version: str) -> str:
    out, n = re.subn(rf"^(\s*{re.escape(CONTRACTS_GO)}) v\S+$", rf"\g<1> {version}", text, flags=re.M)
    if n != 1:
        raise Stop("go.mod: expected one require line of the contracts module")
    return out


def edit_gosum(text: str, version: str, sums: dict[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    add = [f"{CONTRACTS_GO} {version} {sums['Sum']}\n", f"{CONTRACTS_GO} {version}/go.mod {sums['GoModSum']}\n"]
    if all(a in lines for a in add):
        return text
    last = max((i for i, ln in enumerate(lines) if ln.startswith(CONTRACTS_GO + " ")), default=None)
    if last is None:
        raise Stop("go.sum: no line of the contracts module")
    return "".join(lines[:last + 1] + add + lines[last + 1:])


def edit_package_json(text: str, commit: str) -> str:
    out, n = GENERATED_CLIENTS.subn(rf"\g<1>{commit}\g<3>", text)
    if n != 1:
        raise Stop("package.json: expected one git-pinned @anvilkit/generated-clients dependency")
    return out


def readme_with_pin(text: str, pin: dict) -> str:
    out, missing = docs_status.apply(text, {"contracts": docs_status.contracts_line(pin)})
    if missing:
        raise Stop("README.md: no generated contracts block")
    return out


def go_sums(version: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="release-train-go-") as tmp:
        p = run(["go", "mod", "download", "-json", f"{CONTRACTS_GO}@{version}"], tmp, env=GO_ENV)
    info = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}
    if p.returncode != 0 or not info.get("Sum") or not info.get("GoModSum"):
        raise Stop(f"proxy.golang.org does not serve {CONTRACTS_GO}@{version} yet: {(info.get('Error') or p.stderr).strip()[-300:]}")
    return {"Sum": info["Sum"], "GoModSum": info["GoModSum"]}


def pnpm_lock(cwd: str, rev: str, package_json: str) -> str:
    """The lockfile pnpm writes for package_json, resolved off-tree from rev's workspace files."""
    with tempfile.TemporaryDirectory(prefix="release-train-pnpm-") as tmp:
        for name in ("pnpm-lock.yaml", "pnpm-workspace.yaml", ".npmrc"):
            if (text := show(cwd, rev, name)) is not None:
                (pathlib.Path(tmp) / name).write_text(text, encoding="utf-8")
        (pathlib.Path(tmp) / "package.json").write_text(package_json, encoding="utf-8")
        p = run(["pnpm", "install", "--lockfile-only", "--ignore-scripts"], tmp)
        if p.returncode != 0:
            raise Stop(f"{cwd}: pnpm install --lockfile-only: {(p.stderr or p.stdout).strip()[-400:]}")
        return (pathlib.Path(tmp) / "pnpm-lock.yaml").read_text(encoding="utf-8")


def consumer_files(cwd: str, kind: str, rev: str, version: str, commit: str, tag: str) -> dict[str, str]:
    """The bumped manifests and README of a consumer at rev: go.mod/go.sum requiring version, or
    package.json/pnpm-lock.yaml pinning commit. Any lockfile change beyond the pin stops the train."""
    readme = show(cwd, rev, "README.md")
    if kind == "go":
        files = {"go.mod": edit_gomod(show(cwd, rev, "go.mod"), version),
                 "go.sum": edit_gosum(show(cwd, rev, "go.sum"), version, go_sums(version))}
        pin = {"kind": "go", "version": version, "manifest": "go.mod"}
    else:
        old = show(cwd, rev, "pnpm-lock.yaml")
        files = {"package.json": edit_package_json(show(cwd, rev, "package.json"), commit)}
        files["pnpm-lock.yaml"] = pnpm_lock(cwd, rev, files["package.json"])
        before = GENERATED_CLIENTS.search(show(cwd, rev, "package.json")).group(2)
        stray = [ln for ln in difflib.unified_diff(old.splitlines(), files["pnpm-lock.yaml"].splitlines(), lineterm="", n=0)
                 if ln[:1] in "+-" and not ln.startswith(("+++", "---")) and before not in ln and commit not in ln]
        if stray:
            raise Stop(f"{cwd}: pnpm changed more than the contracts pin ({len(stray)} lines, e.g. {stray[0][:120]!r}); bump it by hand")
        pin = {"kind": "ts", "commit": commit[:7], "tag": tag, "manifest": "package.json"}
    files["README.md"] = readme_with_pin(readme, pin)
    return files


# --- Plan ---------------------------------------------------------------------------------------

def step_ids(plan: dict) -> list[str]:
    """Every id names its plan's tags, so a state.json left by another plan can never mark a step done."""
    jobs = [f"jobs:{j['tag']}" for j in plan["jobs"]]
    ids = jobs[:1] + ([f"team:{plan['jobs'][0]['tag']}"] + jobs[1:] if len(jobs) > 1 else [])
    if plan["images"]:
        tag = plan["contracts"]["tag"]
        ids += [f"contracts:{tag}", f"consumers:{tag}", f"parent:{tag}"]
    return ids + [f"release:{plan['release']}", f"pin:{plan['release']}"]


def job_runs(images: list[str]) -> list[list[str]]:
    """The job-images runs: the validator first and the team, rebuilt on it, in a run of its own."""
    if VALIDATOR not in images:
        return [images] if images else []
    first = [i for i in images if i != TEAM_IMAGE]
    return [first, [TEAM_IMAGE]]


def make_plan(images: list[str], at: str | None) -> tuple[dict, list[tuple[str, str]]]:
    """The plan and its preconditions [(OK or BLOCKED or NOTE, text)], read-only."""
    checks: list[tuple[str, str]] = []
    unknown = [i for i in images if i not in JOB_IMAGES]
    if unknown:
        raise Stop(f"unknown Job images {unknown}; known: {' '.join(JOB_IMAGES)}")
    if VALIDATOR in images and TEAM_IMAGE not in images:
        images = images + [TEAM_IMAGE]
        checks.append(("NOTE", f"{TEAM_IMAGE} added: its image is based on the validator it pins (job-images.yml)"))
    if SUPERVISOR in images and TEAM_IMAGE not in images:
        checks.append(("NOTE", f"{SUPERVISOR} without {TEAM_IMAGE}: the team image builds the supervisor from jobs/codegen/supervisor too"))
    for tool in ("git", "gh", "sh") + (("go", "pnpm") if images else ()):
        checks.append(("OK" if shutil.which(tool) else "BLOCKED", f"{tool} on PATH"))
    checks.append(("OK" if run(["gh", "auth", "status"]).returncode == 0 else "BLOCKED", "gh is authenticated"))
    parent_repo = repo_of(".")
    main = remote_refs(".", "refs/heads/main")["refs/heads/main"]
    head = git("rev-parse", "HEAD")
    branch = git("branch", "--show-current")
    checks.append(("OK" if branch == "main" and head == main else "BLOCKED", f"parent: on main at origin/main {main[:12]} (HEAD {head[:12]})"))
    dirty = git("diff", "--name-only", "HEAD")
    checks.append(("OK" if not dirty else "BLOCKED", "parent: no modified tracked file" + (f" ({dirty.splitlines()[:3]})" if dirty else "")))
    checks.append(("OK" if (ROOT / ".git/hooks/pre-push").exists() else "NOTE", "parent: the pre-push gate is installed (DEC-029)"))
    at_sha = git("rev-parse", f"{at or main}^{{commit}}")
    checks.append(("OK" if git_ok("merge-base", "--is-ancestor", at_sha, main) else "BLOCKED", f"{at_sha[:12]} is on origin/main"))
    # The first tag (or a service release) goes on at; an image train's parent push builds on main.
    for sha in dict.fromkeys([at_sha] + ([main] if images else [])):
        base = ci_base(sha)
        checks.append(("OK" if green(parent_repo, "ci.yml", base) else "BLOCKED",
                       f"parent ci is green on {base[:12]}" + (f" (the nearest non-pin commit before {sha[:12]})" if base != sha else "")))
    if not images:
        checks.append(("OK" if green(parent_repo, "supply-chain.yml", at_sha) else "NOTE",
                       f"parent supply-chain is green on {at_sha[:12]} (the release step waits for it)"))
    parent_tags = tags(".", "")
    jobs = []
    jobs_tags = list(t for t in parent_tags if t.startswith("jobs-v"))
    for run_images in job_runs(images):
        tag = next_tag("jobs-v", jobs_tags)
        jobs_tags.append(tag)
        jobs.append({"tag": tag, "images": run_images})
    plan = {"schemaVersion": 1, "release": next_tag("v", list(parent_tags)), "created": utc("%Y-%m-%dT%H:%MZ"),
            "images": images, "at": at_sha, "jobs": jobs, "consumers": sorted(CONSUMERS) if images else []}
    for tag in [j["tag"] for j in jobs] + [plan["release"]]:
        checks.append(("OK" if not git_ok("rev-parse", "-q", "--verify", f"refs/tags/{tag}") else "BLOCKED", f"tag {tag} is free locally and on origin"))
    if images:
        contracts_tags = tags(CONTRACTS, "go/v")
        base = latest_tag("go/v", contracts_tags)
        plan["contracts"] = {"tag": next_tag("go/v", list(contracts_tags)), "base": base, "baseCommit": contracts_tags[base]}
        c_main = remote_refs(CONTRACTS, "refs/heads/main")["refs/heads/main"]
        checks.append(("OK" if c_main == contracts_tags[base] else "BLOCKED",
                       f"contracts: origin/main {c_main[:12]} is {base} (no unreleased contract change rides along)"))
        profiles = show(CONTRACTS, contracts_tags[base], PROFILE_COPIES[0])
        try:
            _, _, revisions = repin_profiles(profiles, {i: "sha256:" + "0" * 64 for i in images})
            plan["profileRevisions"] = revisions
            checks.append(("OK", "profiles: one digest per image; re-pinned revisions " + ", ".join(f"{p} {r}" for p, r in revisions.items())))
        except Stop as e:
            checks.append(("BLOCKED", f"profiles at {base}: {e}"))
        for path, kind in sorted(CONSUMERS.items()):
            manifest = show(path, "HEAD", "go.mod" if kind == "go" else "package.json") or ""
            pinned = (f"{CONTRACTS_GO} {base[3:]}" in manifest) if kind == "go" else (contracts_tags[base] in manifest)
            checks.append(("OK" if pinned else "BLOCKED", f"{path}: pins {base}"))
        # Each parent commit of the train records only its own gitlinks, so every repository it touches
        # (the team in every image train: the parent step's diff would list it) starts at its gitlink.
        for path in [CONTRACTS, *sorted(CONSUMERS), TEAM]:
            r_main = remote_refs(path, "refs/heads/main")["refs/heads/main"]
            ok = git("branch", "--show-current", cwd=path) == "main" and git("rev-parse", "HEAD", cwd=path) == r_main \
                and not git("diff", "--name-only", "HEAD", cwd=path) and gitlink("HEAD", path) == r_main
            checks.append(("OK" if ok else "BLOCKED", f"{path}: clean, on main at origin/main {r_main[:12]}, the parent's gitlink"))
    return plan, checks


def describe(plan: dict) -> list[str]:
    out = []
    for n, job in enumerate(plan["jobs"]):
        where = plan["at"][:12] if n == 0 else "the parent tip after the team step"
        out.append(f"jobs:{job['tag']}: tag {job['tag']} on {where}; job-images.yml for {' '.join(job['images'])}; SIGNED manifest")
        if n == 0 and len(plan["jobs"]) > 1:
            out.append("team: re-pin jobs/codegen/team/Dockerfile to the new validator; team ci; parent gitlink and rows; parent ci")
    if plan["images"]:
        c = plan["contracts"]
        revs = ", ".join(f"{p} {int(r) - 1}->{r}" for p, r in plan.get("profileRevisions", {}).items())
        out.append(f"contracts:{c['tag']}: re-pin the profiles on {c['base']} ({revs}); contracts ci; tag {c['tag']}; proxy.golang.org")
        out.append(f"consumers: {', '.join(p.rsplit('/', 1)[-1] for p in plan['consumers'])} pin {c['tag']}; each ci")
        out.append("parent: admission chart and policy fixtures re-pinned, gitlinks, status rows; one push; ci and supply-chain")
    where = "the parent step's commit" if plan["images"] else plan["at"][:12]
    out.append(f"release:{plan['release']}: tag {plan['release']} on {where}; release.yml; SIGNED manifest")
    out.append(f"pin: {PIN} from that manifest, committed and pushed alone; pin.yml")
    return out


def plan_cmd(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="release-train.py plan")
    ap.add_argument("--images", default="", help="space- or comma-separated Job images (none: a service release)")
    ap.add_argument("--at", default=None, help="the parent commit the first jobs-v tag goes on (default origin/main)")
    ap.add_argument("--out", default=None, help="plan directory (default outputs/release-train/<release tag>)")
    a = ap.parse_args(argv)
    plan, checks = make_plan([i for i in re.split(r"[\s,]+", a.images) if i], a.at)
    for status, text in checks:
        print(f"{status:<8}{text}")
    print(f"\nrelease train {plan['release']}:")
    for n, line in enumerate(describe(plan), 1):
        print(f"  {n}. {line}")
    print("  then: the owner's deployment authorization (not part of the train)")
    if any(s == "BLOCKED" for s, _ in checks):
        print("\nBLOCKED: no plan written; clear the BLOCKED lines and plan again.")
        return 1
    out = ROOT / (a.out or f"outputs/release-train/{plan['release']}")
    if (out / "state.json").exists():
        print(f"\nnot written: {out.relative_to(ROOT)} holds a train's state.json; continue that train with run, or plan with --out DIR")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(f"\nplan written: {(out / 'plan.json').relative_to(ROOT)}\nthe owner starts it: .local/verification-venv/bin/python "
          f"tools/release-train.py run {out.relative_to(ROOT)}")
    return 0


# --- Steps ----------------------------------------------------------------------------------------

class Train:
    def __init__(self, out: pathlib.Path):
        self.out = out
        raw = (out / "plan.json").read_bytes()
        self.plan = json.loads(raw)
        state_file = out / "state.json"
        self.state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {"done": {}}
        digest = hashlib.sha256(raw).hexdigest()
        if self.state.setdefault("plan", digest) != digest:
            raise Stop(f"{out.relative_to(ROOT)}/state.json belongs to another plan.json; restore that plan or plan into a new directory")
        self.repo = repo_of(".")
        # One date for the whole train: a resume on a later day must write the same comments.
        self.today = self.state.setdefault("date", utc("%Y-%m-%d"))
        tag = self.plan.get("contracts", {}).get("tag")
        self.ids = {"team": f"team:{self.plan['jobs'][0]['tag']}" if self.plan["jobs"] else "",
                    "consumers": f"consumers:{tag}", "parent": f"parent:{tag}"}

    def save(self) -> None:
        self.state["updated"] = utc()
        (self.out / "state.json").write_text(json.dumps(self.state, indent=2) + "\n", encoding="utf-8")

    def done(self, step: str) -> dict:
        return self.state["done"][step]

    def signed_manifest(self, tag: str, name: str, images: list[str]) -> tuple[pathlib.Path, dict]:
        target = self.out / tag.replace("/", "-")
        target.mkdir(parents=True, exist_ok=True)
        gh("release", "download", tag, "-R", self.repo, "--pattern", name, "-D", str(target), "--clobber", read=True)
        manifest = json.loads((target / name).read_text(encoding="utf-8"))
        if manifest.get("status") != "SIGNED" or manifest.get("findings"):
            raise Stop(f"{tag}: manifest status {manifest.get('status')} with {len(manifest.get('findings') or [])} findings")
        for image in images:
            entry = manifest.get("images", {}).get(image) or {}
            if not DIGEST.match(entry.get("digest", "")) or entry.get("signature") != "VERIFIED" or entry.get("sbomAttestation") != "VERIFIED":
                raise Stop(f"{tag}: {image} is not in the manifest with a digest and a VERIFIED signature and SBOM attestation")
        return target / name, manifest

    def jobs(self, n: int) -> dict:
        job = self.plan["jobs"][n]
        tag, images = job["tag"], job["images"]
        at = self.plan["at"] if n == 0 else self.done(self.ids["team"])["parent"]
        ensure_tag(".", tag, at)
        # The runs list lags a dispatch: a recorded dispatch is never repeated (a second run would
        # publish a second image set).
        dispatched = self.state.setdefault("dispatched", {})
        if tag not in dispatched and not workflow_runs(self.repo, "job-images.yml", "workflow_dispatch", branch=tag):
            gh("workflow", "run", "job-images.yml", "-R", self.repo, "--ref", tag, "-f", f"images={' '.join(images)}")
            dispatched[tag] = utc()
            self.save()
            say(f"  dispatched job-images.yml on {tag} for {' '.join(images)}")
        found = wait_run(self.repo, "job-images.yml", tag, event="workflow_dispatch", branch=tag, timeout=4 * 3600,
                         hint=f"; it was dispatched at {dispatched[tag]}: if no run exists, dispatch it by hand "
                              f"(gh workflow run job-images.yml --ref {tag} -f images='{' '.join(images)}') and run the train again")
        path, manifest = self.signed_manifest(tag, "job-images-manifest.json", images)
        return {"tag": tag, "commit": at, "run": found["id"], "manifest": str(path.relative_to(ROOT)),
                "digests": {i: manifest["images"][i]["digest"] for i in images}, "validatorSource": gitlink(at, VALIDATOR_SOURCE)}

    def team(self) -> dict:
        first = self.done(f"jobs:{self.plan['jobs'][0]['tag']}")
        git("fetch", "--quiet", "origin", cwd=TEAM)
        text = repin_dockerfile(show(TEAM, "origin/main", "Dockerfile"), first["digests"][VALIDATOR], self.today,
                                first["validatorSource"], first["tag"])
        sha = commit_and_push(TEAM, {"Dockerfile": text},
                              f"🔧 chore: pin the validator published by {first['tag']}\n\n"
                              f"FROM {first['digests'][VALIDATOR]} (anvilkit-job-validator {first['validatorSource'][:7]}, manifest SIGNED); "
                              f"release train {self.plan['release']} (DEC-035).")
        wait_run(repo_of(TEAM), "ci.yml", f"team {sha[:12]}", sha=sha, branch="main", timeout=3600)
        tip = parent_commit({TEAM: sha}, {}, f"📦 chore: update subproject commit reference for team component\n\n"
                                               f"The team Dockerfile pins the validator of {first['tag']} (release train {self.plan['release']}, DEC-035).",
                            self.plan["release"])
        wait_run(self.repo, "ci.yml", f"parent {tip[:12]}", sha=tip, branch="main")
        return {"team": sha, "parent": tip}

    def digests(self) -> tuple[dict[str, str], dict[str, str]]:
        """Every published digest and the jobs-v tag that published it."""
        digests, tag_of = {}, {}
        for job in self.plan["jobs"]:
            for image, digest in self.done(f"jobs:{job['tag']}")["digests"].items():
                digests[image], tag_of[digest] = digest, job["tag"]
        return digests, tag_of

    def contracts(self) -> dict:
        c = self.plan["contracts"]
        digests, _ = self.digests()
        text, replaced, revisions = repin_profiles(show(CONTRACTS, c["baseCommit"], PROFILE_COPIES[0]), digests)
        git("fetch", "--quiet", "origin", cwd=CONTRACTS)
        main = git("rev-parse", "origin/main", cwd=CONTRACTS)
        if main != c["baseCommit"]:
            # Only this train's re-pin may sit between the base tag and origin/main.
            other = set(git("diff", "--name-only", c["baseCommit"], main, cwd=CONTRACTS).splitlines()) - set(PROFILE_COPIES)
            if other or any(blob_at(CONTRACTS, main, f) != blob_of(CONTRACTS, text) for f in PROFILE_COPIES):
                raise Stop(f"contracts: origin/main {main[:12]} moved past {c['base']} with other changes ({sorted(other)[:3]}); "
                           "release or revert them first")
        message = (f"✨ feat: re-pin the Job profiles to the images of {', '.join(j['tag'] for j in self.plan['jobs'])}\n\n"
                   + "".join(f"- {p}: revision {r}\n" for p, r in revisions.items())
                   + f"\nRelease train {self.plan['release']} (tools/release-train.py, DEC-035).")
        sha = commit_and_push(CONTRACTS, {f: text for f in PROFILE_COPIES}, message)
        wait_run(repo_of(CONTRACTS), "ci.yml", f"contracts {sha[:12]}", sha=sha, branch="main", timeout=3600)
        ensure_tag(CONTRACTS, c["tag"], sha)
        for attempt in range(20):
            try:
                go_sums(c["tag"][3:])
                break
            except Stop:
                if attempt == 19:
                    raise
                time.sleep(POLL)
        say(f"  proxy.golang.org serves {CONTRACTS_GO}@{c['tag'][3:]}")
        return {"commit": sha, "tag": c["tag"], "replaced": replaced, "revisions": revisions}

    def consumers(self) -> dict:
        c = self.done(f"contracts:{self.plan['contracts']['tag']}")
        version, commit = c["tag"][3:], c["commit"]
        shas = {}
        for path in self.plan["consumers"]:
            git("fetch", "--quiet", "origin", cwd=path)
            files = consumer_files(path, CONSUMERS[path], "origin/main", version, commit, c["tag"])
            what = f"{CONTRACTS_GO} {version}" if CONSUMERS[path] == "go" else f"@anvilkit/generated-clients at contracts {commit[:7]} ({c['tag']})"
            shas[path] = commit_and_push(path, files, f"📦 chore: update the contracts dependency to {c['tag']}\n\n"
                                                      f"{what}; release train {self.plan['release']} (DEC-035).")
        for path, sha in shas.items():
            wait_run(repo_of(path), "ci.yml", f"{path.rsplit('/', 1)[-1]} {sha[:12]}", sha=sha, branch="main", timeout=3600)
        return shas

    def parent(self) -> dict:
        c = self.done(f"contracts:{self.plan['contracts']['tag']}")
        _, tag_of = self.digests()
        git("fetch", "--quiet", "origin")
        files, cited = {}, False
        for directory in PARENT_REPIN:
            for f in git("ls-tree", "--name-only", "origin/main", f"{directory}/").splitlines():
                base = show(".", "origin/main", f)
                if f.endswith(".yaml") and base is not None:
                    text = repin_text(base, c["replaced"], c["revisions"], tag_of, self.today)
                    cited = cited or any(new in text for new in c["replaced"].values())
                    if text != base:
                        files[f] = text
        if not cited:
            raise Stop("parent: no admission chart or policy fixture cites the re-pinned images; re-pin them by hand")
        gitlinks = {CONTRACTS: c["commit"], **self.done(self.ids["consumers"])}
        tip = parent_commit(gitlinks, files, f"📦 chore: re-pin the admission chart and policy fixtures; contracts {c['tag']} and its consumers\n\n"
                                                + "".join(f"- {p}: revision {r}\n" for p, r in c["revisions"].items())
                                                + f"\nRelease train {self.plan['release']} (tools/release-train.py, DEC-035).",
                            self.plan["release"])
        wait_run(self.repo, "ci.yml", f"parent {tip[:12]}", sha=tip, branch="main")
        wait_run(self.repo, "supply-chain.yml", f"parent {tip[:12]}", sha=tip, branch="main", timeout=3600)
        return {"commit": tip, "files": sorted(files)}

    def release(self) -> dict:
        tag = self.plan["release"]
        if self.plan["images"]:
            commit = self.done(self.ids["parent"])["commit"]
        else:
            # A service release tags the planned commit, never whatever main is at run time.
            commit = self.plan["at"]
            git("fetch", "--quiet", "origin")
            if not git_ok("merge-base", "--is-ancestor", commit, "origin/main"):
                raise Stop(f"the planned commit {commit[:12]} is no longer on origin/main; plan again")
            base = ci_base(commit)
            wait_run(self.repo, "ci.yml", f"parent {base[:12]}", sha=base, branch="main")
            wait_run(self.repo, "supply-chain.yml", f"parent {commit[:12]}", sha=commit, branch="main", timeout=3600)
        ensure_tag(".", tag, commit)
        found = wait_run(self.repo, "release.yml", tag, branch=tag, timeout=4 * 3600)
        path, _ = self.signed_manifest(tag, "release-manifest.json", [])
        return {"tag": tag, "commit": commit, "run": found["id"], "manifest": str(path.relative_to(ROOT))}

    def pin(self) -> dict:
        r = self.done(f"release:{self.plan['release']}")
        p = run(["sh", PIN_GENERATOR, str(ROOT / r["manifest"])])
        if p.returncode != 0:
            raise Stop(f"{PIN_GENERATOR} refused the manifest: {p.stderr.strip()[-300:]}")
        sha = commit_and_push(".", {PIN: p.stdout}, f"📦 chore: update image references in images.env for release {r['tag']}\n\n"
                                                    f"Written by {PIN_GENERATOR} from the SIGNED manifest of {r['tag']} (release run {r['run']}); "
                                                    f"release train {r['tag']} (DEC-035).")
        # The commit that brought this pin (main may have moved on since a first attempt pushed it).
        sha = git("log", "-1", "--format=%H", sha, "--", PIN)
        if git("diff", "--name-only", f"{sha}^", sha) != PIN:
            raise Stop(f"the pin commit {sha[:12]} changes more than {PIN}; its gate is the ci run of that push, not pin")
        found = wait_run(self.repo, "pin.yml", f"pin {sha[:12]}", sha=sha, branch="main", timeout=1800)
        return {"commit": sha, "run": found["id"], "blob": git("rev-parse", f"{sha}:{PIN}")}

    def step(self, step: str) -> dict:
        kind = step.split(":", 1)[0]
        if kind == "jobs":
            return self.jobs([f"jobs:{j['tag']}" for j in self.plan["jobs"]].index(step))
        return getattr(self, kind)()


def confirm(plan: dict) -> tuple[bool, str]:
    """The owner types the release tag in a terminal; without one (an agent's shell) nothing runs.
    This stops an accidental start; it is no security boundary. Returns (confirmed, why not)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False, "stdin and stdout are not a terminal (run it in a terminal, not through a pipe or `!`)"
    try:
        # Unbuffered binary: a buffered read-write text stream needs a seekable file, and a terminal is not one.
        with open("/dev/tty", "rb+", buffering=0) as tty:
            tty.write(f"Type {plan['release']} to run this train with your Git and gh credentials: ".encode())
            typed = tty.readline().decode("utf-8", "replace").strip()
    except OSError as e:
        return False, f"/dev/tty cannot be used ({e})"
    return (True, "") if typed == plan["release"] else (False, f"typed {typed!r}, not {plan['release']}")


def run_cmd(out: pathlib.Path) -> int:
    global LOG
    out = out if out.is_absolute() else ROOT / out
    train = Train(out)
    print(f"release train {train.plan['release']} ({out.relative_to(ROOT)}):")
    for n, (sid, line) in enumerate(zip(step_ids(train.plan), describe(train.plan)), 1):
        print(f"  {n}. [{'done' if sid in train.state['done'] else 'todo'}] {line}")
    # The train commits only what it writes: other tracked changes in the parent are committed first. A
    # new train starts from a clean parent; a resumed one may find its own edits (the submodules it
    # moved, re-pinned files, and the rows when a rows commit failed after its gitlink commit).
    resumed = bool(train.state["done"])
    ahead = git("rev-parse", "HEAD") != git("rev-parse", "origin/main")
    allowed = ({CONTRACTS, TEAM, *CONSUMERS, PIN} | (set(ROWS) if ahead else set())) if resumed else set()
    foreign = [f for f in git("diff", "--name-only", "HEAD").splitlines()
               if f not in allowed and not (resumed and f.startswith(PARENT_REPIN))]
    if foreign:
        print(f"not started: the parent has tracked changes the train would not commit ({foreign[:5]}); commit or restore them first")
        return 1
    confirmed, why = confirm(train.plan)
    if not confirmed:
        print(f"not started: {why}. run needs an interactive terminal and the typed release tag. The owner starts a train; "
              "development agents never do (AGENTS.md section 4, DEC-008, DEC-035).")
        return 1
    LOG = out / "train.log"
    train.state.setdefault("started", utc())
    train.save()
    say(f"train {train.plan['release']} running")
    again = f"after fixing it, run again: .local/verification-venv/bin/python tools/release-train.py run {out.relative_to(ROOT)}"
    for sid in step_ids(train.plan):
        if sid in train.state["done"]:
            continue
        say(f"step {sid}")
        try:
            train.state["done"][sid] = train.step(sid)
        except Stop as e:
            say(f"STOPPED at {sid}: {e}")
            say(again)
            return 1
        except Exception as e:  # a missing tool, a malformed reply: logged as a stop, then the traceback
            say(f"STOPPED at {sid}: unexpected {type(e).__name__}: {e}")
            say(again)
            raise
        train.save()
        say(f"step {sid} done: {json.dumps(train.state['done'][sid])[:400]}")
    say(f"train {train.plan['release']} complete; deploying it needs the owner's authorization (HANDOFF deployment checklist)")
    return 0


# --- Self-test and replay ---------------------------------------------------------------------------

def self_test() -> int:
    cases: list[tuple[str, bool]] = []

    def stops(fn, *a) -> bool:
        try:
            fn(*a)
        except Stop:
            return True
        return False
    d = {k: "sha256:" + k * 64 for k in "abcdef"}
    cases.append(("next patch tag", next_tag("jobs-v", ["jobs-v0.1.9", "jobs-v0.1.15", "v0.2.0"]) == "jobs-v0.1.16"))
    cases.append(("next release tag ignores other prefixes", next_tag("v", ["v0.1.12", "jobs-v0.9.0", "go/v1.0.0"]) == "v0.1.13"))
    cases.append(("next go tag", next_tag("go/v", ["go/v0.1.24", "go/v0.1.3"]) == "go/v0.1.25"))
    profiles = json.dumps({"schemaVersion": 1, "profiles": [
        {"profileId": "team-dev", "revision": "18", "image": {"repository": TEAM_IMAGE, "digest": d["a"]},
         "sidecarImage": {"repository": "anvilkit-job-access-sidecar", "digest": d["c"]}},
        {"profileId": "team", "revision": "9", "image": {"repository": TEAM_IMAGE, "digest": d["a"]}},
        {"profileId": "validator", "revision": "7", "image": {"repository": VALIDATOR, "digest": d["b"]},
         "sidecarImage": {"repository": "anvilkit-job-access-sidecar", "digest": d["c"]}}]}, indent=2) + "\n"
    text, replaced, revisions = repin_profiles(profiles, {TEAM_IMAGE: d["d"]})
    cases.append(("team profiles get the digest and revision + 1", replaced == {d["a"]: d["d"]} and revisions == {"team-dev": "19", "team": "10"}))
    cases.append(("other profiles are untouched", json.loads(text)["profiles"][2] == json.loads(profiles)["profiles"][2]))
    cases.append(("a re-pin to the same digests changes nothing", repin_profiles(text, {TEAM_IMAGE: d["d"]})[0] == text))
    _, _, rev = repin_profiles(profiles, {"anvilkit-job-access-sidecar": d["e"]})
    cases.append(("a sidecar re-pin bumps every profile carrying it", rev == {"team-dev": "19", "validator": "8"}))
    ambiguous = profiles.replace(d["a"], d["f"], 1)
    cases.append(("a repository pinned at two digests stops", stops(repin_profiles, ambiguous, {TEAM_IMAGE: d["d"]})))
    cases.append(("an image no profile pins stops", stops(repin_profiles, profiles, {"anvilkit-parser": d["d"]})))
    cases.append(("a malformed digest stops", stops(repin_profiles, profiles, {TEAM_IMAGE: "sha256:1"})))
    chart = ("    # production profile team (disabled, revision 9) and its\n    # development twin team-dev (revision 18) pin the same\n"
             "    # image since 2026-10-29 (release jobs-v0.1.15); they may\n    - name: teamDigest\n"
             f"      expression: \"'{d['a']}'\"\n    - name: supervisorDigests\n      expression: \"['{d['b']}', '{d['a']}']\"\n"
             "    # unrelated comment, revision 3 of team, jobs-v0.0.1\n    - name: other\n      expression: \"'x'\"\n")
    want = chart.replace(d["a"], d["d"]).replace("revision 9)", "revision 10)").replace("(revision 18)", "(revision 19)") \
        .replace("since 2026-10-29 (release jobs-v0.1.15)", "since 2026-10-31 (release jobs-v0.1.16)")
    cases.append(("chart: digests and the comment above the changed variable", repin_text(chart, replaced, revisions, {d["d"]: "jobs-v0.1.16"}, "2026-10-31") == want))
    fixture = (f'      value: \'{{"profileId":"team-dev","profileRevision":"18","schemaVersion":1}}\'\n    image: r/{TEAM_IMAGE}@{d["a"]}\n'
               f'        \\"attempt\\":\\"1\\",\\"profileId\\":\\"team\\",\\"profileRevision\\":\\"9\\",\\"j\n'
               '      value: \'{"profileId":"validator","profileRevision":"7"}\'\n')
    fixed = repin_text(fixture, replaced, revisions, {d["d"]: "jobs-v0.1.16"}, "2026-10-31")
    cases.append(("fixture: plain and escaped envelopes and the image", fixed == fixture.replace(d["a"], d["d"]).replace(
        '"profileRevision":"18"', '"profileRevision":"19"').replace('\\"profileRevision\\":\\"9\\"', '\\"profileRevision\\":\\"10\\"')))
    dockerfile = ("# Pinned 2026-10-27 to the validator the job-images workflow published from\n"
                  "# anvilkit-job-validator d99dff1 (release jobs-v0.1.6, manifest SIGNED).\n"
                  f"FROM node AS build\nFROM ${{VALIDATOR_REPOSITORY}}@{d['b']}\n")
    cases.append(("team Dockerfile: the validator digest and its comment", repin_dockerfile(dockerfile, d["e"], "2026-10-31", "fdcecf8aa", "jobs-v0.1.16")
                  == dockerfile.replace(d["b"], d["e"]).replace("2026-10-27", "2026-10-31").replace("d99dff1 (release jobs-v0.1.6", "fdcecf8 (release jobs-v0.1.16")))
    cases.append(("a Dockerfile without the validator FROM stops", stops(repin_dockerfile, "FROM node\n", d["e"], "x", "f", "t")))
    pinned = repin_dockerfile(dockerfile, d["e"], "2026-10-31", "fdcecf8", "jobs-v0.1.16")
    cases.append(("a Dockerfile already on the validator is left as it is on a later day",
                  repin_dockerfile(pinned, d["e"], "2026-11-02", "fdcecf8", "jobs-v0.1.16") == pinned))
    gomod = f"module m\n\nrequire (\n\tgithub.com/a/b v1.0.0\n\t{CONTRACTS_GO} v0.1.24\n)\n"
    cases.append(("go.mod: the contracts require line only", edit_gomod(gomod, "v0.1.25") == gomod.replace("v0.1.24", "v0.1.25")))
    gosum = f"a v1 h1:x=\n{CONTRACTS_GO} v0.1.24 h1:o=\n{CONTRACTS_GO} v0.1.24/go.mod h1:g=\nz v1 h1:y=\n"
    sums = {"Sum": "h1:n=", "GoModSum": "h1:g="}
    bumped = edit_gosum(gosum, "v0.1.25", sums)
    cases.append(("go.sum: the two lines after the module's last line", bumped.splitlines()[3:5] == [f"{CONTRACTS_GO} v0.1.25 h1:n=", f"{CONTRACTS_GO} v0.1.25/go.mod h1:g="]
                  and edit_gosum(bumped, "v0.1.25", sums) == bumped))
    pkg = '{\n  "dependencies": {\n    "@anvilkit/generated-clients": "github:ancyloce/anvilkit-agent-contracts#' + "1" * 40 + '&path:ts"\n  }\n}\n'
    cases.append(("package.json: the generated-clients commit", edit_package_json(pkg, "2" * 40) == pkg.replace("1" * 40, "2" * 40)))
    cases.append(("Go README line as docs-status writes it", "requires `" + CONTRACTS_GO + " v0.1.25` (`go.mod`; tag `go/v0.1.25`)"
                  in readme_with_pin("x\n<!-- generated:contracts -->\nold\n<!-- /generated:contracts -->\n", {"kind": "go", "version": "v0.1.25", "manifest": "go.mod"})))
    team_only = {"images": [TEAM_IMAGE], "jobs": [{"tag": "jobs-v1.0.1"}], "contracts": {"tag": "go/v1.0.1"}, "release": "v1.0.1"}
    cases.append(("team-only steps, each named by its plan's tags", step_ids(team_only) == [
        "jobs:jobs-v1.0.1", "contracts:go/v1.0.1", "consumers:go/v1.0.1", "parent:go/v1.0.1", "release:v1.0.1", "pin:v1.0.1"]))
    validator = dict(team_only, jobs=[{"tag": "jobs-v1.0.1"}, {"tag": "jobs-v1.0.2"}])
    cases.append(("validator steps: the team re-pin between two job-images runs", step_ids(validator)[:3] == ["jobs:jobs-v1.0.1", "team:jobs-v1.0.1", "jobs:jobs-v1.0.2"]))
    replanned = dict(validator, jobs=[{"tag": "jobs-v1.0.3"}, {"tag": "jobs-v1.0.4"}], contracts={"tag": "go/v1.0.2"}, release="v1.0.2")
    cases.append(("a re-plan shares no step id with the old plan", not set(step_ids(validator)) & set(step_ids(replanned))))
    cases.append(("job runs: validator first, team alone", job_runs([VALIDATOR, "anvilkit-parser", TEAM_IMAGE]) == [[VALIDATOR, "anvilkit-parser"], [TEAM_IMAGE]]))
    cases.append(("service release steps", step_ids({"images": [], "jobs": [], "release": "v1.0.1"}) == ["release:v1.0.1", "pin:v1.0.1"]))
    cases += git_mechanics()
    cases.append(("a terminal: the typed release tag confirms", confirm_in_terminal("v9.9.9\n") == 0))
    cases.append(("a terminal: another text refuses", confirm_in_terminal("v9.9.8\n") == 1))
    failed = [n for n, ok in cases if not ok]
    for n in failed:
        print("FAIL", n)
    print(f"release-train self-test: {'PASS' if not failed else 'FAIL'} ({len(cases) - len(failed)}/{len(cases)})")
    return 1 if failed else 0


def confirm_in_terminal(typed: str) -> int:
    """confirm() alone, in a child whose controlling terminal is a new pseudo-terminal (as the owner's
    is), for a dummy plan; typed is fed to it. 0 confirmed, 1 refused, 2 failed. No train runs."""
    import pty
    pid, fd = pty.fork()
    if pid == 0:
        code = 2
        try:
            code = 0 if confirm({"release": "v9.9.9"})[0] else 1
        finally:
            os._exit(code)
    os.write(fd, typed.encode())
    try:
        while os.read(fd, 1024):
            pass
    except OSError:  # EIO once the child has exited
        pass
    os.close(fd)
    return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])


def git_mechanics() -> list[tuple[str, bool]]:
    """commit_and_push, parent_commit and ensure_tag against throwaway repositories with local bare
    origins in a temporary directory, never this checkout: ROOT points there for the duration."""
    global ROOT
    real, cases = ROOT, []
    real_head = git("rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="release-train-git-") as tmp:
        base = pathlib.Path(tmp).resolve()
        if base.is_relative_to(real):
            raise RuntimeError(f"{base} is inside the checkout")
        (base / "hooks").mkdir()

        def sh(*argv: str, cwd: pathlib.Path) -> str:
            return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

        def configure(repo: pathlib.Path) -> None:
            for key, value in (("user.name", "train test"), ("user.email", "train@test.invalid"), ("commit.gpgsign", "false"),
                               ("tag.gpgsign", "false"), ("core.hooksPath", str(base / "hooks"))):
                sh("git", "config", key, value, cwd=repo)
        for name in ("sub", "parent"):
            sh("git", "init", "-q", "--bare", "-b", "main", f"{name}.git", cwd=base)
            sh("git", "clone", "-q", f"{name}.git", f"seed-{name}", cwd=base)
            configure(base / f"seed-{name}")
        (base / "seed-sub/a.txt").write_text("1\n")
        (base / "seed-sub/b.txt").write_text("b\n")
        sh("git", "add", ".", cwd=base / "seed-sub")
        sh("git", "commit", "-qm", "seed", cwd=base / "seed-sub")
        sh("git", "push", "-q", "origin", "HEAD:main", cwd=base / "seed-sub")
        seed = base / "seed-parent"
        (seed / "tools").mkdir()
        (seed / "deploy").mkdir()
        (seed / "deploy/x.yaml").write_text("old\n")
        (seed / "README.md").write_text("rows: none\n")
        (seed / "tools/docs-status.py").write_text(  # writes the parent's rows from HEAD's gitlink, as the real one does
            "import pathlib, subprocess\nsha = subprocess.run(['git', 'ls-tree', 'HEAD', 'sub'], capture_output=True, text=True).stdout.split()[2]\n"
            "pathlib.Path('README.md').write_text(f'rows: {sha[:7]}\\n')\n")
        sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(base / "sub.git"), "sub", cwd=seed)
        sh("git", "add", ".", cwd=seed)
        sh("git", "commit", "-qm", "seed", cwd=seed)
        sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
        sh("git", "-c", "protocol.file.allow=always", "clone", "-q", "--recurse-submodules", "parent.git", "work", cwd=base)
        work = base / "work"
        configure(work)
        configure(work / "sub")
        sh("git", "checkout", "-q", "main", cwd=work / "sub")

        def origin(repo: str) -> str:
            return sh("git", "rev-parse", "main", cwd=base / f"{repo}.git")

        def stops(fn, *a) -> bool:
            try:
                fn(*a)
            except Stop:
                return True
            return False
        try:
            ROOT = work
            first = commit_and_push("sub", {"a.txt": "2\n"}, "two")
            cases.append(("git: a new commit is pushed to main", origin("sub") == first and (work / "sub/a.txt").read_text() == "2\n"))
            cases.append(("git: a rerun with the same files adds no commit", commit_and_push("sub", {"a.txt": "2\n"}, "two") == first == origin("sub")))
            (work / "sub/a.txt").write_text("3\n")
            sh("git", "commit", "-qam", "three", cwd=work / "sub")
            local = sh("git", "rev-parse", "HEAD", cwd=work / "sub")
            cases.append(("git: a local commit that lacks its push is pushed, not repeated",
                          commit_and_push("sub", {"a.txt": "3\n"}, "three") == local == origin("sub")))
            (work / "sub/b.txt").write_text("foreign\n")
            cases.append(("git: a foreign tracked change stops the commit", stops(commit_and_push, "sub", {"a.txt": "4\n"}, "four")
                          and origin("sub") == local))
            sh("git", "checkout", "-q", "--", "b.txt", cwd=work / "sub")
            ensure_tag("sub", "go/v0.0.1", local)
            cases.append(("git: the tag is created on origin, and a rerun accepts it",
                          remote_refs("sub", "refs/tags/go/v0.0.1*").get("refs/tags/go/v0.0.1") == local and not stops(ensure_tag, "sub", "go/v0.0.1", local)))
            cases.append(("git: a tag elsewhere stops", stops(ensure_tag, "sub", "go/v0.0.1", first)))
            (work / "README.md").write_text("foreign\n")
            cases.append(("git: a dirty README stops the parent commit", stops(parent_commit, {"sub": local}, {"deploy/x.yaml": "new\n"}, "repin", "t")))
            sh("git", "checkout", "-q", "--", "README.md", cwd=work)
            tip = parent_commit({"sub": local}, {"deploy/x.yaml": "new\n"}, "repin", "t")
            cases.append(("git: parent gitlink and file, then the rows, pushed together",
                          tip == origin("parent") and gitlink(tip, "sub") == local and show(".", tip, "deploy/x.yaml") == "new\n"
                          and show(".", tip, "README.md") == f"rows: {local[:7]}\n" and show(".", f"{tip}^", "README.md") == "rows: none\n"))
            cases.append(("git: a parent rerun adds no commit", parent_commit({"sub": local}, {"deploy/x.yaml": "new\n"}, "repin", "t") == tip == origin("parent")))
            hook = base / "hooks/pre-commit"
            hook.write_text("#!/bin/sh\nexit 1\n")
            hook.chmod(0o755)
            refused = stops(commit_and_push, "sub", {"a.txt": "5\n"}, "five")
            hook.unlink()
            cases.append(("git: a failed commit unstages its files, so a rerun commits them",
                          refused and not sh("git", "diff", "--cached", "--name-only", cwd=work / "sub")
                          and commit_and_push("sub", {"a.txt": "5\n"}, "five") == origin("sub") != local))
            (work / PIN).parent.mkdir(parents=True)
            (work / PIN).write_text("PIN=1\n")
            sh("git", "add", PIN, cwd=work)
            sh("git", "commit", "-qm", "pin", cwd=work)
            cases.append(("git: ci is judged on the nearest commit that is not pin-only", ci_base(sh("git", "rev-parse", "HEAD", cwd=work)) == tip))
            train_dir = work / "outputs/train"
            train_dir.mkdir(parents=True)
            (train_dir / "plan.json").write_text('{"release": "v1.0.0"}\n')
            (train_dir / "state.json").write_text('{"done": {"pin:v0.9.9": {}}, "plan": "another"}\n')
            cases.append(("a state.json of another plan stops the train", stops(Train, train_dir)))
        finally:
            ROOT = real
    cases.append(("git: this checkout is untouched", git("rev-parse", "HEAD") == real_head))
    return cases


# Re-pins and bumps the owner committed on 2026-10-28/29 (BLOCKERS.md B-002 steps 6 and 9), replayed
# from their parents: the train must write the same bytes.
REPIN_REPLAYS = [
    ("jobs-v0.1.15 (team)", "9f2d2d8^", "9f2d2d8", "98d4373^", "98d4373", "2026-10-29",
     {TEAM_IMAGE: ("sha256:c9a7dd3d291ef03367a75a787168568917fc11f3fe768342e92a39ebe90fc617", "jobs-v0.1.15")}),
    ("jobs-v0.1.13 and jobs-v0.1.14 (validator, then team)", "63c3cf4^", "63c3cf4", "04c93f4^", "7904367", "2026-10-29",
     {VALIDATOR: ("sha256:87458f7da41a64a847e1bbd94da67f263e76f0d352fe6772d2effdb2b9e12ec8", "jobs-v0.1.13"),
      TEAM_IMAGE: ("sha256:7214ac0234c5a9be4ab2cb54ba94423101493f18724c9c32b2fdadc1894ffac8", "jobs-v0.1.14")}),
]
DOCKERFILE_REPLAY = ("8a12a1e^", "8a12a1e", "sha256:87458f7da41a64a847e1bbd94da67f263e76f0d352fe6772d2effdb2b9e12ec8", "2026-10-29", "fdcecf8", "jobs-v0.1.13")
BUMP_REPLAYS = [("services/agent/control", "5d827ec^", "5d827ec"), ("services/agent/workflow", "6301ca7^", "6301ca7"),
                ("services/agent/knowledge", "bd98fc5^", "bd98fc5")]
BUMP_CONTRACTS = ("go/v0.1.24", "9f2d2d839ddc0d52869b16ee93f14cfa79807999")


def replay() -> int:
    failures: list[str] = []

    def compare(what: str, cwd: str, rev: str, path: str, text: str) -> int:
        want = show(cwd, rev, path)
        if want == text:
            return 1
        diff = list(difflib.unified_diff((want or "").splitlines(), text.splitlines(), lineterm="", n=0))[2:8]
        failures.append(f"{what}: {cwd}/{path} differs from {rev}: {diff}")
        return 0
    for what, c_base, c_target, p_base, p_target, date, images in REPIN_REPLAYS:
        digests = {i: d for i, (d, _) in images.items()}
        tag_of = {d: t for d, t in images.values()}
        text, replaced, revisions = repin_profiles(show(CONTRACTS, c_base, PROFILE_COPIES[0]), digests)
        same = sum(compare(what, CONTRACTS, c_target, f, text) for f in PROFILE_COPIES)
        changed = 0
        for directory in PARENT_REPIN:
            for f in git("ls-tree", "--name-only", p_base, f"{directory}/").splitlines():
                base = show(".", p_base, f)
                text = repin_text(base, replaced, revisions, tag_of, date)
                changed += text != base
                same += compare(what, ".", p_target, f, text)
        print(f"replay {what}: profiles {', '.join(f'{p} {r}' for p, r in revisions.items())}; {changed} parent files re-pinned; {same} files compared equal")
    base_rev, target, new, date, source, tag = DOCKERFILE_REPLAY
    if compare("team Dockerfile", TEAM, target, "Dockerfile", repin_dockerfile(show(TEAM, base_rev, "Dockerfile"), new, date, source, tag)):
        print(f"replay team Dockerfile {base_rev}..{target}: equal")
    tag, commit = BUMP_CONTRACTS
    for path, base_rev, target in BUMP_REPLAYS:
        try:
            files = consumer_files(path, CONSUMERS[path], base_rev, tag[3:], commit, tag)
        except Stop as e:
            failures.append(f"{path}: {e}")
            continue
        same = sum(compare(f"bump {path}", path, target, f, t) for f, t in files.items())
        print(f"replay bump {path} {base_rev}..{target}: {same}/{len(files)} files equal ({', '.join(files)})")
    for f in failures:
        print("FAIL", f)
    print(f"release-train replay: {'PASS' if not failures else 'FAIL'} ({len(failures)} differences)")
    return 1 if failures else 0


def main() -> int:
    args = sys.argv[1:]
    try:
        if args == ["--self-test"]:
            return self_test()
        if args == ["--replay"]:
            return replay()
        if args[:1] == ["plan"]:
            return plan_cmd(args[1:])
        if len(args) == 2 and args[0] == "run":
            return run_cmd(pathlib.Path(args[1]))
    except Stop as e:
        print(f"STOPPED: {e}")
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
