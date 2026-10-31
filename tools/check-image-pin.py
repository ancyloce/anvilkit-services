#!/usr/bin/env python3
"""The deploy pin check (DEC-034): a push to main whose only change is the Compose image pin
(deploy/compose/images.env) skips the ci workflow (its paths-ignore); .github/workflows/pin.yml
runs this instead, and its success is the commit's deploy gate (DEC-009: no commit with a failed
or missing check is deployed).

  python3 tools/check-image-pin.py [COMMIT]     # default HEAD; needs git and an authenticated gh
  python3 tools/check-image-pin.py --self-test  # offline

PASS when all three hold:
  pin-only  COMMIT changes the pin file and nothing else (otherwise exit 3: not a pin commit, the
            ci workflow is its check);
  release   the committed pin is byte for byte what deploy/compose/images.sh (at COMMIT) writes from
            the release manifest of a SIGNED v* release of this repository. Compared by Git blob
            hash: the file is never read (owner rule of 2026-10-25 on environment files);
  tree      the nearest ancestor that is not itself a pin commit has a successful ci run on a push,
            so everything but the pin passed the full ci workflow. A pin commit pushed together
            with other commits fails this: push it alone after its parent's ci is green, or deploy
            on the ci run of that push instead.
Exit 0 PASS; 1 FAIL; 2 UNEXECUTED (gh or the API unavailable); 3 not a pin commit.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PIN = "deploy/compose/images.env"
GENERATOR = "deploy/compose/images.sh"
RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")


class Unrunnable(Exception):
    """gh or the GitHub API is unavailable (exit 2)."""


def git(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, input=stdin, capture_output=True, text=True, check=True).stdout


def gh(*args: str) -> str:
    p = subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        raise Unrunnable(f"gh {' '.join(args[:3])}: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout


def repository() -> str:
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    m = re.search(r"github\.com[:/](.+?)(\.git)?$", git("remote", "get-url", "origin").strip())
    if not m:
        raise Unrunnable("origin is not a GitHub repository")
    return m.group(1)


def changed(commit: str) -> list[str]:
    return sorted(f for f in git("diff", "--name-only", f"{commit}^", commit).splitlines() if f)


def pin_blob(commit: str) -> str | None:
    """The pin file's blob at commit, from a listing of its directory (never its content)."""
    for line in git("ls-tree", commit, str(pathlib.PurePosixPath(PIN).parent) + "/").splitlines():
        meta, path = line.split("\t", 1)
        if path == PIN:
            return meta.split()[2]
    return None


def generated_blob(generator: str, manifest: pathlib.Path) -> str | None:
    """The blob hash of what images.sh writes from manifest; None when it refuses the manifest."""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(generator)
    try:
        p = subprocess.run(["sh", f.name, str(manifest)], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    if p.returncode != 0:
        return None
    return git("hash-object", "--stdin", stdin=p.stdout).strip()


def matching_release(commit: str, blob: str, repo: str, limit: int = 30) -> str | None:
    """The v* release whose manifest images.sh turns into exactly the committed pin."""
    generator = git("show", f"{commit}:{GENERATOR}")
    releases = json.loads(gh("release", "list", "-R", repo, "--limit", str(limit), "--json", "tagName,isDraft"))
    with tempfile.TemporaryDirectory(prefix="image-pin-") as tmp:
        for rel in releases:
            tag = rel["tagName"]
            if rel["isDraft"] or not RELEASE_TAG.match(tag):
                continue
            target = pathlib.Path(tmp) / tag
            target.mkdir()
            try:
                gh("release", "download", tag, "-R", repo, "--pattern", "release-manifest.json", "-D", str(target))
            except Unrunnable:
                continue  # a release without a SIGNED manifest asset
            if generated_blob(generator, target / "release-manifest.json") == blob:
                return tag
    return None


def ci_succeeded(sha: str, repo: str) -> bool:
    runs = json.loads(gh("api", f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={sha}&event=push&per_page=20"))["workflow_runs"]
    return any(r.get("conclusion") == "success" for r in runs)


def verdict(pin_only: bool, release: str | None, base: str | None, base_green: bool) -> tuple[int, str]:
    if not pin_only:
        return 3, "not a pin commit: it changes more than the pin, so the ci workflow is its check"
    if release is None:
        return 1, "the committed pin is not what deploy/compose/images.sh writes from any SIGNED v* release manifest"
    if base is None or not base_green:
        return 1, (f"the nearest non-pin ancestor {base or '(none)'} has no successful ci run on a push: push the pin commit "
                   "alone after its parent's ci is green, or deploy on the ci run of the push that carried it")
    return 0, f"pin of release {release}; the rest of the tree passed ci at {base}"


def check(commit: str) -> int:
    repo = repository()
    sha = git("rev-parse", commit).strip()
    files = changed(sha)
    pin_only = files == [PIN]
    release = base = None
    base_green = False
    if pin_only:
        blob = pin_blob(sha)
        release = matching_release(sha, blob, repo) if blob else None
        base = git("rev-parse", f"{sha}^").strip()
        for _ in range(20):
            if changed(base) != [PIN]:
                break
            base = git("rev-parse", f"{base}^").strip()
        base_green = ci_succeeded(base, repo)
    code, why = verdict(pin_only, release, base[:12] if base else None, base_green)
    label = {0: "PASS", 1: "FAIL", 3: "NOT A PIN COMMIT"}[code]
    print(f"{label}: {sha[:12]}: {why}")
    return code


def self_test() -> int:
    cases = []
    cases.append(("a commit changing more than the pin is not a pin commit", verdict(False, "v1.0.0", "a", True)[0] == 3))
    cases.append(("a pin no SIGNED release produces fails", verdict(True, None, "a", True)[0] == 1))
    cases.append(("a pin whose tree did not pass ci fails", verdict(True, "v1.0.0", "a", False)[0] == 1))
    cases.append(("a release's own pin on a green tree passes", verdict(True, "v1.0.0", "a", True)[0] == 0))
    images = {n: {"reference": f"ghcr.io/o/{n}@sha256:{'a' * 64}"} for n in (
        "anvilkit-agent-api", "anvilkit-agent-control", "anvilkit-agent-workflow", "anvilkit-agent-model-proxy", "anvilkit-agent-knowledge",
        "anvilkit-knowledge-forwarder", "anvilkit-agent-mcp", "anvilkit-agent-background-worker", "anvilkit-agent-inference", "anvilkit-migration")}
    generator = (ROOT / GENERATOR).read_text()
    with tempfile.TemporaryDirectory() as tmp:
        def manifest(**kw) -> pathlib.Path:
            p = pathlib.Path(tmp) / f"m{len(list(pathlib.Path(tmp).iterdir()))}.json"
            p.write_text(json.dumps({"run": "v1.0.0", "status": "SIGNED", "findings": [], "registry": {"reference": "ghcr.io/o"},
                                     "images": images, **kw}))
            return p
        signed = generated_blob(generator, manifest())
        expected = subprocess.run(["sh", "-c", f'sh "$0" "$1"', str(ROOT / GENERATOR), str(manifest())], capture_output=True, text=True).stdout
        cases.append(("the generated pin's blob is Git's hash of images.sh's output", signed == git("hash-object", "--stdin", stdin=expected).strip()))
        cases.append(("another release gives another blob", generated_blob(generator, manifest(run="v1.0.1")) not in (None, signed)))
        cases.append(("a BLOCKED manifest gives no pin", generated_blob(generator, manifest(status="BLOCKED")) is None))
        cases.append(("a manifest with findings gives no pin", generated_blob(generator, manifest(findings=["x"])) is None))
    failed = [n for n, ok in cases if not ok]
    for n in failed:
        print("FAIL", n)
    print(f"check-image-pin self-test: {'PASS' if not failed else 'FAIL'} ({len(cases) - len(failed)}/{len(cases)})")
    return 1 if failed else 0


def main() -> int:
    args = sys.argv[1:]
    if args == ["--self-test"]:
        return self_test()
    if len(args) > 1:
        print(__doc__)
        return 2
    try:
        return check(args[0] if args else "HEAD")
    except Unrunnable as e:
        print(f"UNEXECUTED: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
