#!/usr/bin/env python3
"""Supply-chain preflight of the committed dependencies (DEC-032): what makes a release
BLOCKED or its image build fail, checked on every push to main and daily, before a tag meets it.

  .local/verification-venv/bin/python tools/check-dependencies.py                # pins, then scan
  .local/verification-venv/bin/python tools/check-dependencies.py --pins         # no Docker
  .local/verification-venv/bin/python tools/check-dependencies.py --scan [--report-out FILE]
  .local/verification-venv/bin/python tools/check-dependencies.py --report FILE  # judge a saved Trivy report
  .local/verification-venv/bin/python tools/check-dependencies.py --images MANIFEST [--report-dir DIR]
  .local/verification-venv/bin/python tools/check-dependencies.py --self-test

  scan  The release gate's own rule, read from tools/release-artifacts.py: its pinned Trivy image,
        BLOCKING_SEVERITIES and policy_view, so a fixable CRITICAL or HIGH vulnerability blocks.
        Here Trivy reads the lockfiles and go.mod files of the build contexts the release builds
        (its OWNED and JOBS lists, the codegen supervisor included), not the images. Go's
        standard library is judged at the version the context's Dockerfile builds with
        (FROM golang:X.Y.Z), because go.mod's go line is only a minimum; a context without a Go
        build stage ships none.
  pins  Every `name=version` an apt-get install line of those Dockerfiles pins must still be
        served by the Debian archive for its suite: main, -updates or -security; bookworm or
        trixie by the version's deb12 or deb13 suffix, else by the stage's base image. A pin
        the archive dropped fails the image build at release time (plan 0006 §9.10).
  images  RB-F09: the built images themselves, OS packages included (B-010, B-011, B-013 surfaced
        only at the release gate). Every image a release manifest names (its `reference`, by
        digest), and every Job image the checkout's contracts/jobs/profiles.json pins (under the
        manifest's registry, when the manifest does not already name that digest), is scanned
        remotely with the gate's pinned Trivy and judged by the gate's policy_view: a fixable
        CRITICAL or HIGH finding blocks, an unfixable one does not (RB-F08). supply-chain.yml runs it
        daily on the latest `v*` release's manifest, so a fix published after the release shows up
        before the next train is planned. One report per image under --report-dir.
Not covered: license categories (an image property the release gate still judges), the
images a train has not built yet (a Dockerfile changed since the last release is scanned by
the gate when the train builds it), the contracts repository's own lockfiles (schemas
only reach the images), and the Node and Python runtimes of the base images. Lockfiles under
fixtures/ or testdata/ are data, never installed: the gate's image scans read installed
packages, not lockfiles (v0.1.12's reports list no lockfile target), so they do not count
(the first CI run flagged happy-dom 20.14.5 in the validator's hero fixture lockfile, while
the image installs 21.0.4).
Exit 0: nothing blocking; 1: a blocking finding; 2: not runnable here (--scan outside GitHub
Actions or without Docker, the archive unreachable for --pins). The scan runs a container, so it
runs in CI only (supply-chain.yml): DEC-028 keeps containers off the development host.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import lzma
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_artifacts", ROOT / "tools/release-artifacts.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

# The build contexts a release builds: its owned images and Job images (the latter include the
# supervisor the team image builds from).
CONTEXTS = sorted({c for _, c, _ in release.OWNED} | {c for _, c, *_ in release.JOBS})
ARCHIVE = {
    suite: [f"https://deb.debian.org/debian/dists/{suite}/main/binary-amd64/Packages.xz",
            f"https://deb.debian.org/debian/dists/{suite}-updates/main/binary-amd64/Packages.xz",
            f"https://deb.debian.org/debian-security/dists/{suite}-security/main/binary-amd64/Packages.xz"]
    for suite in ("bookworm", "trixie")
}
PIN = re.compile(r"(?<![\w.=-])([a-z0-9][a-z0-9+.-]+)=([0-9][^\s\\'\"]*)")
GOLANG = re.compile(r"^\s*FROM\s+golang:(\d+\.\d+\.\d+)", re.M | re.I)
DATA = re.compile(r"(^|/)(fixtures|testdata)/")


class Unrunnable(Exception):
    """This host cannot run the check (exit 2)."""


def dockerfiles(root: pathlib.Path = ROOT) -> list[pathlib.Path]:
    return sorted(p for c in CONTEXTS for p in (root / c).glob("Dockerfile*") if p.is_file())


def logical_lines(text: str) -> list[str]:
    return re.sub(r"\\\n", " ", text).splitlines()


def dockerfile_pins(path: pathlib.Path, base_suites: dict[str, str | None] | None = None) -> list[tuple[str, str, str | None]]:
    """(package, version, suite) of every apt-get install pin, the suite from the version's debNN
    suffix, else from the stage's base image: its reference (ARG defaults substituted) or, for a
    base built by the release itself (FROM ${VARIABLE}@sha256:..., JOBS), that image's own suite."""
    args: dict[str, str] = {}
    base_suites = base_suites or {}
    suite = None
    pins = []
    for line in logical_lines(path.read_text()):
        if m := re.match(r"\s*ARG\s+(\w+)=(\S+)", line):
            args[m.group(1)] = m.group(2)
        elif m := re.match(r"\s*FROM\s+(\S+)", line, re.I):
            ref = re.sub(r"\$\{?(\w+)\}?", lambda v: args.get(v.group(1), ""), m.group(1))
            suite = "trixie" if "trixie" in ref else "bookworm" if "bookworm" in ref else None
            if suite is None and (var := re.match(r"\$\{?(\w+)\}?", m.group(1))):
                suite = base_suites.get(var.group(1))
        elif re.match(r"\s*RUN\b", line) and "apt-get install" in line:
            for pkg, version in PIN.findall(line):
                pins.append((pkg, version, "bookworm" if "deb12" in version else "trixie" if "deb13" in version else suite))
    return pins


def final_suite(path: pathlib.Path) -> str | None:
    """The Debian suite of a Dockerfile's last stage (its base reference, ARG defaults substituted)."""
    args, suite = {}, None
    for line in logical_lines(path.read_text()):
        if m := re.match(r"\s*ARG\s+(\w+)=(\S+)", line):
            args[m.group(1)] = m.group(2)
        elif m := re.match(r"\s*FROM\s+(\S+)", line, re.I):
            ref = re.sub(r"\$\{?(\w+)\}?", lambda v: args.get(v.group(1), ""), m.group(1))
            suite = "trixie" if "trixie" in ref else "bookworm" if "bookworm" in ref else None
    return suite


def release_base_suites(root: pathlib.Path = ROOT) -> dict[str, str | None]:
    """FROM ${VARIABLE} bases the release builds itself (JOBS: name, Dockerfile, variable): the suite
    of that image's own Dockerfile."""
    contexts = {name: context for name, context, *_ in release.JOBS}
    return {variable: final_suite(root / contexts[name] / "Dockerfile")
            for _, _, _, base in release.JOBS if base for name, _, variable in [base]}


def archive_index(suite: str) -> dict[str, set[str]]:
    served: dict[str, set[str]] = {}
    for url in ARCHIVE[suite]:
        try:
            data = lzma.decompress(urllib.request.urlopen(url, timeout=120).read()).decode("utf-8", "replace")
        except OSError as e:
            raise Unrunnable(f"{url}: {e}")
        for stanza in data.split("\n\n"):
            pkg = re.search(r"^Package: (\S+)$", stanza, re.M)
            ver = re.search(r"^Version: (\S+)$", stanza, re.M)
            if pkg and ver:
                served.setdefault(pkg.group(1), set()).add(ver.group(1))
    return served


def pin_failures(pins: dict[pathlib.Path, list], index) -> list[str]:
    errs = []
    for path, entries in pins.items():
        for pkg, version, suite in entries:
            rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            if suite is None:
                errs.append(f"pins: {rel}: {pkg}={version}: no Debian suite (neither a debNN suffix nor a bookworm or trixie base)")
            elif version not in index(suite).get(pkg, set()):
                errs.append(f"pins: {rel}: {pkg}={version} is no longer served by the {suite} archive (main, -updates, -security)")
    return errs


def check_pins() -> list[str]:
    cache: dict[str, dict] = {}
    bases = release_base_suites()
    pins = {p: dockerfile_pins(p, bases) for p in dockerfiles()}
    errs = pin_failures(pins, lambda s: cache.setdefault(s, archive_index(s)))
    print(f"pins: {sum(len(v) for v in pins.values())} pins in {sum(1 for v in pins.values() if v)} Dockerfiles, {len(errs)} not served")
    return errs


def builders(root: pathlib.Path = ROOT) -> dict[str, str]:
    """The Go version each build context's Dockerfiles build with (the highest named)."""
    out = {}
    for c in CONTEXTS:
        versions = [v for p in (root / c).glob("Dockerfile*") if p.is_file() for v in GOLANG.findall(p.read_text())]
        if versions:
            out[c] = max(versions, key=lambda v: tuple(map(int, v.split("."))))
    return out


def context_of(target: str) -> str | None:
    return max((c for c in CONTEXTS if target == c or target.startswith(c + "/")), key=len, default=None)


def judged(report: dict, go: dict[str, str]) -> dict:
    """The report restricted to the released contexts, with Go's standard library at the builder's
    version: a finding fixed at or below it in the same minor line is not in the binary."""
    def ver(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

    results = []
    for r in report.get("Results") or []:
        context = context_of(r.get("Target", ""))
        if context is None or DATA.search(r.get("Target", "")):
            continue
        vulns = []
        for v in r.get("Vulnerabilities") or []:
            if v.get("PkgName") == "stdlib":
                builder = go.get(context)
                if builder is None:
                    continue
                fixed = [f.strip().lstrip("v") for f in (v.get("FixedVersion") or "").split(",") if f.strip()]
                if any(ver(f)[:2] == ver(builder)[:2] and ver(f) <= ver(builder) for f in fixed):
                    continue
                v = {**v, "InstalledVersion": builder}
            vulns.append(v)
        results.append({**r, "Vulnerabilities": vulns})
    return {"Results": results}


def scan_failures(report: dict, go: dict[str, str]) -> list[str]:
    errs = []
    for r in judged(report, go)["Results"]:
        for finding in release.policy_view({"Results": [r]})["fixableBlocking"]:
            errs.append(f"scan: {r['Target']}: fixable {finding}")
    return errs


def run_scan(report_out: pathlib.Path) -> dict:
    # DEC-028 keeps containers off the development host: the scan runs in GitHub Actions only
    # (.github/workflows/supply-chain.yml), even where Docker happens to be installed.
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise Unrunnable("the scan runs in GitHub Actions only (supply-chain.yml); DEC-028 keeps containers off this host")
    if subprocess.run(["docker", "version"], capture_output=True).returncode != 0:
        raise Unrunnable("docker is not available (the scan runs the release gate's pinned Trivy image)")
    cache = ROOT / ".local/release/trivy-cache"
    cache.mkdir(parents=True, exist_ok=True)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{ROOT}:/src:ro", "-v", f"{report_out.parent}:/out",
           "-v", f"{cache}:/cache", "-e", "TRIVY_CACHE_DIR=/cache", "-e", "HOME=/tmp", release.TRIVY, "fs", "--quiet", "--scanners", "vuln",
           "--format", "json", "--output", f"/out/{report_out.name}", "--skip-dirs", "**/node_modules", "/src"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise Unrunnable(f"trivy exit {p.returncode}: {(p.stdout + p.stderr).strip()[-800:]}")
    report = json.loads(report_out.read_text())
    # Trivy names targets relative to the scanned root.
    for r in report.get("Results") or []:
        r["Target"] = r.get("Target", "").removeprefix("/src/")
    return report


def manifest_images(manifest: dict, profiles: dict | None = None) -> list[tuple[str, str]]:
    """(name, reference) of every image a release manifest names, then every Job image the
    profiles pin under the manifest's registry; one entry per digest, by digest only."""
    digest_re = re.compile(r"^sha256:[0-9a-f]{64}$")
    out: dict[str, tuple[str, str]] = {}
    for key, img in sorted((manifest.get("images") or {}).items()):
        ref = img.get("reference") or ""
        digest = ref.rpartition("@")[2]
        if digest_re.match(digest) and digest not in out:
            out[digest] = (key.split("@")[0], ref)
    registry = ((manifest.get("registry") or {}).get("reference") or "").rstrip("/")
    for prof in (profiles or {}).get("profiles") or []:
        for field in ("image", "sidecarImage"):
            img = prof.get(field) or {}
            repo, digest = img.get("repository") or "", img.get("digest") or ""
            if not repo or not digest_re.match(digest) or digest in out:
                continue
            host = repo.split("/")[0]
            full = repo if "/" in repo and ("." in host or ":" in host) else f"{registry}/{repo}"
            out[digest] = (repo.rsplit("/", 1)[-1], f"{full}@{digest}")
    return sorted(out.values())


def image_failures(name: str, ref: str, report: dict) -> list[str]:
    """The gate's rule on one image's report: each fixable CRITICAL or HIGH finding."""
    short = ref.rpartition("@")[2][:19]
    return [f"images: {name} ({short}): fixable {f}" for f in release.policy_view(report)["fixableBlocking"]]


def run_image_scan(images: list[tuple[str, str]], report_dir: pathlib.Path) -> dict[tuple[str, str], dict]:
    """One remote Trivy image scan per reference, as the release gate runs it (vulnerabilities
    only; the gate's SBOM and license pass stay its own). The registry credentials are the
    runner's Docker configuration, mounted read-only, never on a command line."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise Unrunnable("the image scan runs in GitHub Actions only (supply-chain.yml); DEC-028 keeps containers off this host")
    if subprocess.run(["docker", "version"], capture_output=True).returncode != 0:
        raise Unrunnable("docker is not available (the scan runs the release gate's pinned Trivy image)")
    cache = ROOT / ".local/release/trivy-cache"
    cache.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    config = pathlib.Path(os.environ.get("DOCKER_CONFIG", pathlib.Path.home() / ".docker")) / "config.json"
    mounts = ["-v", f"{config}:/tmp/docker/config.json:ro"] if config.is_file() else []
    reports = {}
    for name, ref in images:
        out = f"{name}-{ref.rpartition('@')[2][7:19]}.trivy.json"
        cmd = ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{report_dir}:/out", "-v", f"{cache}:/cache", *mounts,
               "-e", "TRIVY_CACHE_DIR=/cache", "-e", "HOME=/tmp", "-e", "DOCKER_CONFIG=/tmp/docker", release.TRIVY,
               "image", "--quiet", "--image-src", "remote", "--scanners", "vuln", "--format", "json", "--output", f"/out/{out}", ref]

        def attempt(cmd=cmd, name=name):
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if p.returncode != 0:
                raise RuntimeError(f"trivy exit {p.returncode} for {name}: {(p.stdout + p.stderr).strip()[-800:]}")
        try:
            release.retried(attempt, True)
        except RuntimeError as e:
            raise Unrunnable(str(e)) from e
        reports[(name, ref)] = json.loads((report_dir / out).read_text())
    return reports


def self_test() -> int:
    cases = []
    go = {"services/agent/api": "1.27.3"}

    def vuln(target, pkg, severity, fixed, version="1.0.0"):
        v = {"VulnerabilityID": "CVE-0000-0001", "PkgName": pkg, "InstalledVersion": version, "Severity": severity}
        return {"Results": [{"Target": target, "Vulnerabilities": [{**v, **({"FixedVersion": fixed} if fixed else {})}]}]}

    cases.append(("a fixable HIGH in a released context blocks", scan_failures(vuln("services/agent/api/go.mod", "github.com/go-jose/go-jose/v4", "HIGH", "4.1.6"), go)))
    cases.append(("a fixable HIGH outside the released contexts does not", not scan_failures(vuln("tests/integration/go.mod", "x", "HIGH", "2"), go)))
    cases.append(("a fixture lockfile (data, never installed) does not: the first CI run's happy-dom finding",
                  not scan_failures(vuln("jobs/validator/fixtures/component/hero/pnpm-lock.yaml", "happy-dom", "HIGH", "21.0.4", "20.14.5"), go)))
    cases.append(("the context's own lockfile still does",
                  scan_failures(vuln("jobs/validator/pnpm-lock.yaml", "happy-dom", "HIGH", "21.0.4", "20.14.5"), go)))
    cases.append(("an unfixable CRITICAL does not", not scan_failures(vuln("services/agent/api/go.mod", "x", "CRITICAL", None), go)))
    cases.append(("a fixable MEDIUM does not", not scan_failures(vuln("services/agent/api/go.mod", "x", "MEDIUM", "2"), go)))
    cases.append(("stdlib fixed in the builder's minor line at or below it does not",
                  not scan_failures(vuln("services/agent/api/go.mod", "stdlib", "HIGH", "1.26.9, 1.27.2", "v1.26.8"), go)))
    cases.append(("stdlib fixed above the builder blocks", scan_failures(vuln("services/agent/api/go.mod", "stdlib", "HIGH", "1.27.4", "v1.26.8"), go)))
    cases.append(("stdlib in a context without a Go build stage does not",
                  not scan_failures(vuln("jobs/validator/pnpm-lock.yaml", "stdlib", "HIGH", "1.27.4"), go)))
    with tempfile.TemporaryDirectory() as tmp:
        df = pathlib.Path(tmp) / "Dockerfile"
        df.write_text("ARG PYTHON_IMAGE=python:3.12.12-slim-trixie@sha256:00\nFROM golang:1.27.3-alpine AS build\n"
                      "FROM ${PYTHON_IMAGE}\nRUN apt-get update && apt-get install -y --no-install-recommends \\\n"
                      "    libssl3t64=3.5.7-1~deb13u3 \\\n    tzdata=2025b-4 DEBIAN_FRONTEND=noninteractive \\\n && rm -rf /var/lib/apt/lists/*\n"
                      "FROM node:24.19.0-bookworm-slim\nRUN apt-get install -y libcap2=1:2.66-4+deb12u3+b1\n")
        pins = dockerfile_pins(df)
        cases.append(("pins: suites from the debNN suffix and the stage base, continuation lines joined",
                      pins == [("libssl3t64", "3.5.7-1~deb13u3", "trixie"), ("tzdata", "2025b-4", "trixie"), ("libcap2", "1:2.66-4+deb12u3+b1", "bookworm")]))
        served = {"trixie": {"libssl3t64": {"3.5.7-1~deb13u3"}, "tzdata": {"2025b-3"}}, "bookworm": {"libcap2": {"1:2.66-4+deb12u3+b1"}}}
        errs = pin_failures({df: pins}, lambda s: served[s])
        cases.append(("pins: a version the archive no longer serves fails, the served ones pass", len(errs) == 1 and "tzdata=2025b-4" in errs[0]))
        cases.append(("go builder version read from FROM golang", GOLANG.findall(df.read_text()) == ["1.27.3"]))
        team = pathlib.Path(tmp) / "Dockerfile.team"
        team.write_text("ARG VALIDATOR_REPOSITORY\nFROM ${VALIDATOR_REPOSITORY}@sha256:00\nRUN apt-get install -y ripgrep=14.1.1-1+b4\n")
        cases.append(("pins: a base the release builds (FROM ${VARIABLE}) takes that image's suite",
                      dockerfile_pins(team, {"VALIDATOR_REPOSITORY": "trixie"}) == [("ripgrep", "14.1.1-1+b4", "trixie")]))
    # RB-F09: the image scan's inputs and rule.
    d = lambda c: "sha256:" + c * 64  # noqa: E731
    manifest = {"registry": {"reference": "ghcr.io/owner"}, "images": {
        "anvilkit-agent-api": {"reference": f"ghcr.io/owner/anvilkit-agent-api@{d('a')}"},
        "anvilkit-parser@cccccccccccc": {"reference": f"ghcr.io/owner/anvilkit-parser@{d('c')}"},
        "broken": {"reference": "ghcr.io/owner/broken:latest"}}}
    profiles = {"profiles": [{"image": {"repository": "anvilkit-parser", "digest": d("c")}},
                             {"image": {"repository": "anvilkit-codegen-team", "digest": d("e")},
                              "sidecarImage": {"repository": "anvilkit-job-access-sidecar", "digest": d("f")}},
                             {"image": {"repository": "docker.io/library/busybox", "digest": d("b")}},
                             {"image": {"repository": "anvilkit-validator", "digest": "sha256:short"}}]}
    cases.append(("images: the manifest's references by digest, then the profiles' newer Job images under its registry, one per digest",
                  manifest_images(manifest, profiles) == sorted([
                      ("anvilkit-agent-api", f"ghcr.io/owner/anvilkit-agent-api@{d('a')}"), ("anvilkit-parser", f"ghcr.io/owner/anvilkit-parser@{d('c')}"),
                      ("anvilkit-codegen-team", f"ghcr.io/owner/anvilkit-codegen-team@{d('e')}"),
                      ("anvilkit-job-access-sidecar", f"ghcr.io/owner/anvilkit-job-access-sidecar@{d('f')}"),
                      ("busybox", f"docker.io/library/busybox@{d('b')}")])))

    def os_vuln(severity, fixed):
        v = {"VulnerabilityID": "CVE-2026-85091", "PkgName": "zlib1g", "InstalledVersion": "1:1.2.13.dfsg-1", "Severity": severity}
        return {"Results": [{"Target": "img (debian 12.13)", "Class": "os-pkgs", "Vulnerabilities": [{**v, **({"FixedVersion": fixed} if fixed else {})}]}]}
    ref = f"ghcr.io/owner/anvilkit-agent-inference@{d('9')}"
    cases.append(("images: a fixable HIGH OS package blocks (the B-013 pattern)", image_failures("inference", ref, os_vuln("HIGH", "1:1.2.13.dfsg-1+deb12u1"))))
    cases.append(("images: an unfixable HIGH does not (RB-F08: no bookworm fix yet)", not image_failures("inference", ref, os_vuln("HIGH", None))))
    cases.append(("images: a fixable MEDIUM does not", not image_failures("inference", ref, os_vuln("MEDIUM", "2"))))
    previous = os.environ.pop("GITHUB_ACTIONS", None)
    try:
        run_image_scan([("x", ref)], pathlib.Path(tempfile.gettempdir()))
        cases.append(("images: the scan refuses to run outside GitHub Actions", False))
    except Unrunnable:
        cases.append(("images: the scan refuses to run outside GitHub Actions", True))
    finally:
        if previous is not None:
            os.environ["GITHUB_ACTIONS"] = previous
    failed = [name for name, ok in cases if not ok]
    for name in failed:
        print("FAIL", name)
    print(f"check-dependencies self-test: {'PASS' if not failed else 'FAIL'} ({len(cases) - len(failed)}/{len(cases)})")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--pins", action="store_true")
    mode.add_argument("--scan", action="store_true")
    mode.add_argument("--report")
    mode.add_argument("--images", metavar="MANIFEST")
    mode.add_argument("--self-test", action="store_true")
    ap.add_argument("--report-out", default=str(ROOT / ".local/release/dependencies.trivy.json"))
    ap.add_argument("--report-dir", default=str(ROOT / ".local/release/images"))
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    errs: list[str] = []
    try:
        if a.images:
            profiles = json.loads((ROOT / "contracts/jobs/profiles.json").read_text())
            images = manifest_images(json.loads(pathlib.Path(a.images).read_text()), profiles)
            if not images:
                raise Unrunnable(f"{a.images} names no image by digest")
            for (name, ref), report in run_image_scan(images, pathlib.Path(a.report_dir).resolve()).items():
                errs += image_failures(name, ref, report)
            print(f"images: {len(images)} images scanned")
        elif a.report:
            errs += scan_failures(json.loads(pathlib.Path(a.report).read_text()), builders())
        else:
            if not a.scan:
                errs += check_pins()
            if not a.pins:
                errs += scan_failures(run_scan(pathlib.Path(a.report_out).resolve()), builders())
                print(f"scan: {len(CONTEXTS)} build contexts, Go builders {builders()}")
    except Unrunnable as e:
        for err in errs:
            print("FAIL", err)
        print(f"UNEXECUTED: {e}")
        return 1 if errs else 2
    for err in errs:
        print("FAIL", err)
    print(f"check-dependencies: {len(errs)} blocking findings")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
