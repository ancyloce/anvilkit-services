#!/usr/bin/env python3
"""Signed-image admission check of anvilkit-apps (P23-03) against the qualification cluster.

  .local/verification-venv/bin/python deploy/qualification/check-admission.py --run RUN

Prepares fixture repositories in the registry from one pinned third-party image (the
LocalCheck profile's busybox digest): unsigned; signed by a throwaway foreign key; signed
by the release key without the SBOM attestation. Then asks the cluster's admission chain
with server-side dry runs (Kyverno's webhook runs; nothing is created) to admit a Pod in
anvilkit-apps for each case:

  allowed   a release image by digest, signed and attested by the release key; the fixture
            signed by the release key without an SBOM attestation (attestations are verified
            by the release tool, not at admission: Kyverno 1.19.1 requires a tlog bundle)
  denied    the same release image by tag; an image from another registry; the unsigned and
            foreign-signed fixtures; a signed release image in an init container next to an
            unsigned one; two containers of which one is unsigned

Writes outputs/qualification/<RUN>/admission/admission.json. Exit 0 when every case got
the expected decision, 1 otherwise, 2 when an input is missing.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
KUBECONFIG = ROOT / ".local/qualification/kubeconfig"
KUBECTL = str(ROOT / ".local/bin/kubectl")
KEYS = ROOT / ".local/release"
COSIGN = "ghcr.io/sigstore/cosign/cosign@sha256:68839b7f13dac5a6744a5d8818e984dd39183374e37855c19e14d623d9bc9037"
REGISTRY = "anvilkit-dev-registry:5000"
FIXTURE = "busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0"


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def cosign(args: list[str], keys: pathlib.Path, password: str = "") -> None:
    p = sh(["docker", "run", "--rm", "--network", "kind", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{keys}:/keys:ro", "-e", "COSIGN_PASSWORD", COSIGN, *args,
            "--allow-http-registry", "--allow-insecure-registry"], env={**os.environ, "COSIGN_PASSWORD": password})  # never on a command line
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-300:])


def push_fixture(repo: str) -> str:
    sh(["docker", "pull", "-q", FIXTURE])
    tag = f"localhost:5001/{repo}:fixture"
    sh(["docker", "tag", FIXTURE, tag])
    out = sh(["docker", "push", tag]).stdout
    digest = next(w for w in out.split() if w.startswith("sha256:"))
    return f"{REGISTRY}/{repo}@{digest}"


def admit(name: str, images: list[str], init: list[str] | None = None) -> tuple[bool, str]:
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": f"admission-{name}", "namespace": "anvilkit-apps"},
           "spec": {"containers": [{"name": f"c{i}", "image": img, "command": ["true"]} for i, img in enumerate(images)],
                    **({"initContainers": [{"name": f"i{i}", "image": img, "command": ["true"]} for i, img in enumerate(init)]} if init else {})}}
    p = sh([KUBECTL, "--kubeconfig", str(KUBECONFIG), "create", "--dry-run=server", "-f", "-"], input=json.dumps(pod))
    return p.returncode == 0, (p.stdout + p.stderr).strip().splitlines()[-1][:300] if (p.stdout + p.stderr).strip() else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    manifest_path = ROOT / "outputs/qualification" / a.run / "release/release-manifest.json"
    if not manifest_path.exists() or not KUBECONFIG.exists():
        print("UNEXECUTED: the release manifest and the qualification cluster are required")
        return 2
    manifest = json.loads(manifest_path.read_text())
    release = manifest["images"]["anvilkit-agent-api"]["reference"]
    password = (KEYS / "cosign.password").read_text()
    unsigned = push_fixture("anvilkit-admission-unsigned")
    foreign = push_fixture("anvilkit-admission-foreign")
    unattested = push_fixture("anvilkit-admission-unattested")
    with tempfile.TemporaryDirectory() as tmp:
        fk = pathlib.Path(tmp)
        throwaway = secrets.token_hex(16)
        p = sh(["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{fk}:/keys", "-w", "/keys", "-e", "COSIGN_PASSWORD", COSIGN, "generate-key-pair"],
               env={**os.environ, "COSIGN_PASSWORD": throwaway})
        if p.returncode != 0:
            raise RuntimeError(p.stderr)
        cosign(["sign", "--yes", "--key", "/keys/cosign.key", "--tlog-upload=false", foreign], fk, throwaway)
    cosign(["sign", "--yes", "--key", "/keys/cosign.key", "--tlog-upload=false", unattested], KEYS, password)
    cases = [
        ("release-signed-attested", [release], None, True),
        ("release-by-tag", [release.split("@")[0] + ":" + a.run], None, False),
        ("other-registry", [f"docker.io/library/{FIXTURE}"], None, False),
        ("unsigned", [unsigned], None, False),
        ("foreign-key", [foreign], None, False),
        # Admission does not check SBOM attestations (Kyverno 1.19.1 requires a
        # transparency-log bundle for them); the release verifies them offline.
        ("signed-without-attestation", [unattested], None, True),
        ("unsigned-init-container", [release], [unsigned], False),
        ("unsigned-second-container", [release, unsigned], None, False),
    ]
    results, failures = [], 0
    for name, images, init, expected in cases:
        allowed, message = admit(name, images, init)
        ok = allowed == expected
        failures += not ok
        results.append({"case": name, "images": images, "initContainers": init or [], "expected": "allowed" if expected else "denied",
                        "observed": "allowed" if allowed else "denied", "pass": ok, "message": message})
        print(f"{'PASS' if ok else 'FAIL'} {name:<28} expected {'allowed' if expected else 'denied':<7} observed {'allowed' if allowed else 'denied':<7} {'' if allowed else message[:140]}")
    out = ROOT / "outputs/qualification" / a.run / "admission"
    out.mkdir(parents=True, exist_ok=True)
    (out / "admission.json").write_text(json.dumps({"run": a.run, "policy": "anvilkit-apps-signed-images", "cases": results}, indent=2) + "\n")
    print(f"{len(cases) - failures}/{len(cases)} admission cases as expected")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
