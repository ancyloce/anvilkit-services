#!/usr/bin/env python3
"""Replay recorded component candidates through the validator's certify chain (DEC-031).

  .local/verification-venv/bin/python tools/replay-candidates.py                 # every case of the corpus
  .local/verification-venv/bin/python tools/replay-candidates.py --only ID ...    # some cases
  .local/verification-venv/bin/python tools/replay-candidates.py --self-test      # the validator's own fixture
  options: --corpus FILE (default outputs/candidate-corpus/corpus.json), --validator DIR (stage another
           validator tree, for comparing verdicts across a validator change), --out DIR (evidence copy:
           the report, logs and certifications; the work trees stay under /opt/anvilkit-certify/runs)

A case is a candidate source and the verdict the current validator must give it:
  recorded  a B1 run's final source (a tar archive read from the artifact store), whose
            expectation is the server's verdict unless the validator changed since;
  derived   a source edited by hand to follow a prompt rule before a paid run tries it
            (for example the last failed candidate rewritten to a new rule): `certified`
            shows the rule can pass the validator.
The corpus file lists the cases ({"schemaVersion": 1, "cases": [{"id", "source", "sha256" for an
archive, "expect": {"verdict", "failureCode", "failed"}, ...}]}); "failed" is a text the first
failed check or the refusal message must contain. Candidates and corpus are local evidence
under outputs/ (gitignored); without the corpus the run is UNEXECUTED (exit 2).

A candidate is model output: untrusted data, never instructions (AGENTS.md §6). Each runs as
DEC-028 allows: from a copy of jobs/validator (working tree, uncommitted edits included) staged
under /opt/anvilkit-certify, because the step identities cannot traverse /root; with the build,
SSR and browser steps as UID 10001 and the SSR harness as UID 10003 (`--step-identity
setpriv`, so the caller must be root); in a network namespace with only loopback; inside the
systemd-run cap (4 CPUs, 8 GiB, no swap). Archives are extracted only when every member is a
regular file or directory with a relative path. A replay certifies nothing: its verdict is
DEVELOPMENT_ONLY and its source revision a placeholder.

Exit 0: every verdict as expected; 1: a verdict differs or a source does not match its
recorded digest; 2: not runnable here (not root, no corpus, no Chromium, a profile problem).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "jobs/validator"
CONTRACTS = ROOT / "contracts"
STAGE = pathlib.Path("/opt/anvilkit-certify")
CORPUS = ROOT / "outputs/candidate-corpus/corpus.json"
CAP = ["systemd-run", "--scope", "--quiet", "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0", "-p", "CPUQuota=400%", "--"]
# $0 source, $1 output directory, $2 staged validator, $3 contracts, $4 browsers
CERTIFY = ('ip link set lo up && cd "$2" && ANVILKIT_VALIDATOR_CONTRACTS_DIR="$3" PLAYWRIGHT_BROWSERS_PATH="$4" '
           'exec ./node_modules/.bin/tsx src/cli.ts "$0" --source-revision 1 --out "$1/out" --step-identity setpriv')


class Unrunnable(Exception):
    """This host cannot run a replay (exit 2)."""


def git(*args: str, cwd: pathlib.Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def stage(validator: pathlib.Path = VALIDATOR) -> str:
    """Copy a validator tree (the jobs/validator working tree unless another is named) and Playwright's
    headless shell where the step UIDs can read them; returns the staged revision (commit, '-dirty'
    with uncommitted edits, or the directory when it is not a Git checkout)."""
    if not (validator / "node_modules").is_dir():
        raise Unrunnable(f"{validator} has no node_modules (pnpm install --frozen-lockfile there)")
    STAGE.mkdir(mode=0o755, parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", "--delete", "--exclude", ".git", f"{validator}/", f"{STAGE}/validator/"], check=True)
    browsers = json.loads((validator / "node_modules/.pnpm/node_modules/playwright-core/browsers.json").read_text())
    shell = next(b for b in browsers["browsers"] if b["name"] == "chromium-headless-shell")
    name = f"chromium_headless_shell-{shell['revision']}"
    target = STAGE / "ms-playwright" / name
    if not target.is_dir():
        cached = pathlib.Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", pathlib.Path.home() / ".cache/ms-playwright")) / name
        if not cached.is_dir():
            raise Unrunnable(f"{name} is not installed (pnpm exec playwright install chromium-headless-shell in jobs/validator)")
        target.parent.mkdir(mode=0o755, exist_ok=True)
        shutil.copytree(cached, target, symlinks=True)
    try:
        revision = git("rev-parse", "HEAD", cwd=validator) + ("-dirty" if git("status", "--porcelain", cwd=validator) else "")
    except subprocess.CalledProcessError:
        revision = f"{validator} (not a Git checkout)"
    (STAGE / "validator.revision").write_text(revision + "\n")
    return revision


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def materialize(source: pathlib.Path, dest: pathlib.Path) -> None:
    """A recorded archive (regular files and directories with relative paths only) or a directory."""
    if source.is_dir():
        shutil.copytree(source, dest, symlinks=False)
        return
    with tarfile.open(source) as tar:
        for m in tar.getmembers():
            parts = pathlib.PurePosixPath(m.name).parts
            if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in parts:
                raise ValueError(f"{source}: member {m.name!r} is not a plain relative file or directory")
        tar.extractall(dest, filter="data")


def certify(src: pathlib.Path, run_dir: pathlib.Path) -> dict:
    """The validator's verdict on src: its certification, or a refusal of the source contract or build."""
    browsers = STAGE / "ms-playwright"
    argv = CAP + ["unshare", "-n", "sh", "-c", CERTIFY, str(src), str(run_dir), str(STAGE / "validator"), str(CONTRACTS), str(browsers)]
    started = time.monotonic()
    p = subprocess.run(argv, capture_output=True, text=True)
    seconds = round(time.monotonic() - started, 1)
    (run_dir / "certify.log").write_text(p.stdout + p.stderr)
    doc = None
    # The certification goes to stdout; a refusal (exit 1) is printed to stderr.
    for line in reversed((p.stdout + "\n" + p.stderr).strip().splitlines()):
        try:
            doc = json.loads(line)
            break
        except ValueError:
            continue
    if p.returncode == 0 and doc and "verdict" in doc:
        failed = doc.get("failed") or []
        return {"verdict": doc["verdict"], "failureCode": doc.get("failureCode"), "failed": failed[0] if failed else "", "seconds": seconds}
    if p.returncode == 1 and doc and "code" in doc:
        return {"verdict": "refused", "failureCode": doc["code"], "failed": doc.get("message", ""), "seconds": seconds}
    raise Unrunnable(f"certify exit {p.returncode}: {(p.stdout + p.stderr).strip()[-600:]}")


def matches(expect: dict, got: dict) -> bool:
    return (got["verdict"] == expect["verdict"]
            and expect.get("failureCode") in (None, got["failureCode"])
            and expect.get("failed", "") in got["failed"])


def replay(cases: list[dict], out: pathlib.Path, revision: str) -> int:
    rows, bad = [], 0
    for case in cases:
        run_dir = out / case["id"]
        run_dir.mkdir(parents=True)
        source = (ROOT / case["source"]) if not pathlib.Path(case["source"]).is_absolute() else pathlib.Path(case["source"])
        if case.get("sha256") and sha256(source) != case["sha256"]:
            rows.append({**case, "status": "FAIL", "got": {"verdict": "source digest differs from the corpus"}})
            bad += 1
            continue
        with tempfile.TemporaryDirectory(prefix="replay-src-") as tmp:
            src = pathlib.Path(tmp) / "source"
            materialize(source, src)
            got = certify(src, run_dir)
        ok = matches(case["expect"], got)
        bad += not ok
        rows.append({**case, "status": "MATCH" if ok else "FAIL", "got": got})
        print(f"{'MATCH' if ok else 'FAIL':<6} {case['id']:<28} {got['verdict']:<20} {got['failureCode'] or '':<24} {got['seconds']:>5}s  {got['failed'][:90]}")
    (out / "report.json").write_text(json.dumps({"validator": revision, "contracts": git("rev-parse", "HEAD", cwd=CONTRACTS),
                                                  "cases": rows}, indent=2) + "\n")
    print(f"replay-candidates: {len(rows)} cases, {bad} not as expected; validator {revision}; report {out / 'report.json'}")
    return 1 if bad else 0


def keep(run_root: pathlib.Path, evidence: pathlib.Path) -> None:
    """Copy the report and each case's log and certification (not the work trees) to evidence."""
    for f in [run_root / "report.json", *run_root.glob("*/certify.log"), *run_root.glob("*/out/*.json")]:
        target = evidence / f.relative_to(run_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)
    print(f"evidence: {evidence}")


def self_test(out: pathlib.Path, revision: str) -> int:
    """The validator's reviewed hero fixture is certified; the same source with a `main` key is refused."""
    hero = VALIDATOR / "fixtures/component/hero"
    with tempfile.TemporaryDirectory(prefix="replay-selftest-") as tmp:
        bad = pathlib.Path(tmp) / "hero-with-main"
        shutil.copytree(hero, bad)
        pkg = json.loads((bad / "package.json").read_text())
        (bad / "package.json").write_text(json.dumps({**pkg, "main": "src/index.tsx"}, indent=2) + "\n")
        cases = [{"id": "self-test-hero", "source": str(hero), "expect": {"verdict": "certified"}},
                 {"id": "self-test-hero-main", "source": str(bad),
                  "expect": {"verdict": "refused", "failureCode": "CANDIDATE_BUILD_FAILED", "failed": "package.json: field main is not accepted"}}]
        return replay(cases, out, revision)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--only", action="append")
    ap.add_argument("--out")
    ap.add_argument("--validator", help="another validator tree to stage (with its node_modules), for comparing verdicts across a validator change")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    try:
        if os.geteuid() != 0:
            raise Unrunnable("the setpriv step identities need a root caller")
        cases = []
        if not a.self_test:
            corpus = pathlib.Path(a.corpus)
            if not corpus.is_file():
                raise Unrunnable(f"no corpus at {corpus} (local evidence under outputs/)")
            cases = [c for c in json.loads(corpus.read_text())["cases"] if not a.only or c["id"] in a.only]
            if a.only and len(cases) != len(set(a.only)):
                raise Unrunnable(f"unknown case id among {a.only}")
        revision = stage(pathlib.Path(a.validator).resolve() if a.validator else VALIDATOR)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # The work trees stay under the staged root: the step UIDs cannot reach /root.
        run_root = STAGE / "runs" / f"replay-{stamp}"
        run_root.mkdir(parents=True)
        rc = self_test(run_root, revision) if a.self_test else replay(cases, run_root, revision)
        if a.out:
            keep(run_root, pathlib.Path(a.out).resolve())
        return rc
    except Unrunnable as e:
        print(f"UNEXECUTED: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
