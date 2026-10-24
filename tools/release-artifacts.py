#!/usr/bin/env python3
"""Release artifacts (P23-02, P3.2): build, publish, scan, SBOM and sign the release candidate.

  .local/verification-venv/bin/python tools/release-artifacts.py [--run ID] [--only NAME ...] [--no-build] [--from-run RUN]
      [--registry-push HOST] [--registry REF] [--network NAME] [--signing key|keyless] [--key REF]
      [--certificate-identity ID --certificate-oidc-issuer URL] [--transparency-log] [--provenance]
  .local/verification-venv/bin/python tools/release-artifacts.py --self-test

Owned service images (the eight services, the Knowledge forwarder sidecar and the
knowledge/mcp migration Job) are built from each repository's own Dockerfile and pushed to
the registry; the fixed Job images are taken at the exact digests their reviewed profiles
pin (contracts/jobs/profiles.json), never rebuilt; the third-party platform images
(PLATFORM) are copied into the registry by digest. For every image, by digest: an SPDX
SBOM and a vulnerability and license scan (Trivy), a Cosign signature and a signed SBOM
attestation, both verified. The eight service charts are packaged, pushed as OCI artifacts
and signed. Everything is bound in outputs/qualification/<run>/release/release-manifest.json
together with each source's repository head, cleanliness and build-context digest, the
parent's release tag and the pinned tool images.

Status, derived from the inputs (P3.2, never hard-coded):
  BLOCKED                  any finding: a dirty or untracked input (a release builds only
                           committed sources), a fixable CRITICAL or HIGH vulnerability, a
                           forbidden license, a missing or unverified signature or
                           attestation, a failed build or push;
  SIGNED_DEVELOPMENT_ONLY  no finding, but a development placement: the development key
                           under .local/release (no ENV-10 custody), a plain-HTTP registry,
                           no transparency log, no build provenance or no release tag on the
                           parent;
  SIGNED                   no finding, built from a tagged parent with provenance, signed
                           keylessly in CI (OIDC, transparency log) or with a KMS/OpenBao-
                           held key (--key hashivault://... etc.) with the transparency log,
                           pushed to a TLS registry.
deploy/gitops/publish.py publishes nothing from a manifest whose status is not SIGNED (or,
for a development combination, SIGNED_DEVELOPMENT_ONLY) or that has findings.

The development defaults (registry localhost:5001 / anvilkit-dev-registry:5000, network
kind, key under .local/release, no transparency log) reproduce the DEVELOPMENT_ONLY
qualification placement; .github/workflows/release.yml runs the same tool keylessly on a
tag. The release policy is the tool's explicit rule pending the security owner's input,
not a derived threshold. --self-test proves the status rules on synthetic manifests.

Exit 0 when every artifact is published, signed and verified and the policy has no
blocking finding; 1 otherwise; 2 when Docker, helm or the registry is unavailable.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Development defaults; --registry-push, --registry and --network replace them (P3.2).
HOST_REGISTRY = os.environ.get("ANVILKIT_RELEASE_REGISTRY_PUSH", "localhost:5001")   # the registry as this host pushes to it
REGISTRY = os.environ.get("ANVILKIT_RELEASE_REGISTRY", "anvilkit-dev-registry:5000")  # the same registry as clusters and tools reach it
NETWORK = os.environ.get("ANVILKIT_RELEASE_NETWORK", "kind")
DEVELOPMENT_REGISTRIES = {"localhost:5001", "anvilkit-dev-registry:5000"}
BLOCKING_SEVERITIES = ("CRITICAL", "HIGH")
SETTINGS: dict = {"signing": "key", "key": None, "insecure": True, "tlog": False, "identity": None, "issuer": None, "provenance": False}
TRIVY = "aquasec/trivy@sha256:bcc376de8d77cfe086a917230e818dc9f8528e3c852f7b1aff648949b6258d1c"   # 0.69.3
COSIGN = "ghcr.io/sigstore/cosign/cosign@sha256:68839b7f13dac5a6744a5d8818e984dd39183374e37855c19e14d623d9bc9037"  # v2.6.1
KEYS = ROOT / ".local/release"
CACHE = ROOT / ".local/release/trivy-cache"

# (image, build context, extra docker build arguments)
OWNED = [
    ("anvilkit-agent-api", "services/agent/api", []),
    ("anvilkit-agent-control", "services/agent/control", []),
    ("anvilkit-agent-workflow", "services/agent/workflow", []),
    ("anvilkit-agent-model-proxy", "services/agent/model-proxy", ["--build-context", "contracts=contracts"]),
    ("anvilkit-agent-knowledge", "services/agent/knowledge", []),
    ("anvilkit-knowledge-forwarder", "services/agent/knowledge", ["--target", "forwarder"]),
    ("anvilkit-agent-mcp", "services/agent/mcp", []),
    ("anvilkit-agent-background-worker", "services/agent/background-worker", ["--build-context", "contracts=contracts"]),
    ("anvilkit-agent-inference", "services/agent/inference", ["--build-context", "contracts=contracts"]),
    ("anvilkit-migration", "jobs/migration", []),
]
CHARTS = [f"services/agent/{s}/deploy/chart" for s in ("api", "control", "workflow", "model-proxy", "knowledge", "mcp", "background-worker", "inference")]
# Third-party images the environment runs where only release-signed images are admitted
# (anvilkit-apps: the anvilkit-secrets holder Pod of the CSI-synced Secrets, P0.6): copied
# by digest (every platform of the index, the same digest) into the registry under
# vendor/, then scanned, given an SBOM and signed like the owned images. (name, upstream
# reference with digest, repository:tag in the registry)
PLATFORM = [
    ("pause", "registry.k8s.io/pause:3.10@sha256:ee6521f290b2168b6e0935a181d4cff9be1ac3f505666ef0e3c98fae8199917a", "vendor/pause:3.10"),
]


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def must(cmd: list[str], **kw) -> str:
    p = sh(cmd, **kw)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}...: {(p.stderr or p.stdout).strip()[-600:]}")
    return p.stdout


def helm() -> str | None:
    local = ROOT / ".local/bin/helm"
    return str(local) if local.exists() else shutil.which("helm")


def sha256_file(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def source(context: str) -> dict:
    """Repository head, cleanliness and the digest of the build context's files."""
    ctx = ROOT / context
    top = pathlib.Path(must(["git", "-C", str(ctx), "rev-parse", "--show-toplevel"]).strip())
    rel = ctx.resolve().relative_to(top.resolve())
    files = sorted(f for f in must(["git", "-C", str(top), "ls-files", "-co", "--exclude-standard", "--", str(rel)]).splitlines() if f)
    h = hashlib.sha256()
    for f in files:
        p = top / f
        if p.is_file():
            h.update(f.encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    dirty = must(["git", "-C", str(top), "status", "--porcelain", "--", str(rel)]).strip().splitlines()
    return {"repository": str(top.relative_to(ROOT)) if top != ROOT else ".", "head": must(["git", "-C", str(top), "rev-parse", "HEAD"]).strip(),
            "context": str(rel) or ".", "clean": not dirty, "uncommittedPaths": len(dirty), "contextDigest": "sha256:" + h.hexdigest(), "files": len(files)}


def tool(image: str, args: list[str], out: pathlib.Path, env: dict | None = None, expect: pathlib.Path | None = None) -> str:
    """Runs one pinned tool container. With expect, a container the Docker daemon
    fails to reap after the tool exited (observed on this host: the process gone,
    the container still "running") is bounded: when the output file is complete
    JSON the result stands and the named container is removed; otherwise it fails."""
    name = f"anvilkit-release-{secrets.token_hex(6)}"
    # The tool runs as the invoking user: on a CI runner that is not root and
    # cannot enter /root in the tool images, so the writable places are named
    # explicitly: Trivy's cache on the mounted volume at /cache, HOME=/tmp for
    # cosign's own cache.
    cmd = ["docker", "run", "--rm", "--name", name, "--network", NETWORK, "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{out}:/out", "-v", f"{KEYS}:/keys:ro",
           "-v", f"{CACHE}:/cache", "-e", "DOCKER_CONFIG=/tmp/docker", "-e", "TRIVY_CACHE_DIR=/cache", "-e", "HOME=/tmp"]
    # An authenticated registry: the host's Docker credentials, read-only (never on a command line).
    config = pathlib.Path(os.environ.get("DOCKER_CONFIG", pathlib.Path.home() / ".docker")) / "config.json"
    if not SETTINGS["insecure"] and config.is_file():
        cmd += ["-v", f"{config}:/tmp/docker/config.json:ro"]
    # Keyless signing in GitHub Actions: cosign's provider reads the runner's OIDC request
    # variables (their values from the client's environment, never the command line).
    if SETTINGS["signing"] == "keyless":
        for k in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
            if os.environ.get(k):
                cmd += ["-e", k]
    for k in env or {}:
        cmd += ["-e", k]  # the value comes from the docker client's environment, never the command line
    penv = {**os.environ, **(env or {})}
    if expect is None:
        return must(cmd + [image] + args, env=penv)
    try:
        return must(cmd + [image] + args, timeout=900, env=penv)
    except subprocess.TimeoutExpired:
        sh(["docker", "rm", "-f", name])
        try:
            json.loads(expect.read_text())
        except (OSError, ValueError):
            raise RuntimeError(f"{image.split('@')[0]} did not finish within 900 s") from None
        return ""


def keypair() -> str:
    KEYS.mkdir(parents=True, exist_ok=True)
    os.chmod(KEYS, 0o700)
    CACHE.mkdir(exist_ok=True)
    pw = KEYS / "cosign.password"
    if not pw.exists():
        pw.write_text(secrets.token_urlsafe(32))
        os.chmod(pw, 0o600)
    if not (KEYS / "cosign.key").exists():
        must(["docker", "run", "--rm", "-v", f"{KEYS}:/keys", "-w", "/keys", "-e", "COSIGN_PASSWORD",
              "-u", f"{os.getuid()}:{os.getgid()}", COSIGN, "generate-key-pair"], env={**os.environ, "COSIGN_PASSWORD": pw.read_text()})
        os.chmod(KEYS / "cosign.key", 0o600)
    return pw.read_text()


def signing_flags() -> tuple[list[str], list[str]]:
    """(sign flags, verify flags) of the configured signing mode."""
    registry = ["--allow-http-registry", "--allow-insecure-registry"] if SETTINGS["insecure"] else []
    if SETTINGS["signing"] == "keyless":
        return registry, registry + ["--certificate-identity", SETTINGS["identity"], "--certificate-oidc-issuer", SETTINGS["issuer"]]
    key = SETTINGS["key"] or "/keys/cosign.key"
    pub = SETTINGS["key"] or "/keys/cosign.pub"
    tlog_sign = [] if SETTINGS["tlog"] else ["--tlog-upload=false"]
    tlog_verify = [] if SETTINGS["tlog"] else ["--insecure-ignore-tlog=true"]
    return ["--key", key, *tlog_sign, *registry], ["--key", pub, *tlog_verify, *registry]


def sign_and_verify(ref: str, out: pathlib.Path, password: str, sbom: str | None) -> dict:
    env = {"COSIGN_PASSWORD": password} if SETTINGS["signing"] == "key" and not SETTINGS["key"] else {}
    sign, verify = signing_flags()
    tool(COSIGN, ["sign", "--yes", *sign, ref], out, env)
    verified = tool(COSIGN, ["verify", *verify, ref], out)
    result = {"signature": "VERIFIED" if ref.split("@")[1] in verified else "FAILED"}
    if sbom:
        tool(COSIGN, ["attest", "--yes", *sign, "--type", "spdxjson", "--predicate", f"/out/{sbom}", ref], out, env)
        att = tool(COSIGN, ["verify-attestation", *verify, "--type", "spdxjson", ref], out)
        result["sbomAttestation"] = "VERIFIED" if att.strip() else "FAILED"
    return result


def scan(name: str, ref: str, out: pathlib.Path) -> dict:
    insecure = ["--insecure"] if SETTINGS["insecure"] else []
    tool(TRIVY, ["image", *insecure, "--quiet", "--image-src", "remote", "--format", "spdx-json", "--output", f"/out/{name}.spdx.json", ref], out,
         expect=out / f"{name}.spdx.json")
    tool(TRIVY, ["image", *insecure, "--quiet", "--image-src", "remote", "--scanners", "vuln,license", "--format", "json", "--output", f"/out/{name}.trivy.json", ref], out,
         expect=out / f"{name}.trivy.json")
    sbom = json.loads((out / f"{name}.spdx.json").read_text())
    return {"sbom": {"file": f"{name}.spdx.json", "format": "SPDX JSON", "digest": sha256_file(out / f"{name}.spdx.json"), "packages": len(sbom.get("packages") or [])},
            "scan": {"file": f"{name}.trivy.json", "digest": sha256_file(out / f"{name}.trivy.json"), **policy_view(json.loads((out / f"{name}.trivy.json").read_text()))}}


def policy_view(report: dict) -> dict:
    """The release policy's reading of a Trivy report: every severity counted, every
    fixable CRITICAL or HIGH vulnerability named (blocking, P3.2), the license categories."""
    severities: dict[str, int] = {}
    fixable, licenses = [], {}
    for r in report.get("Results") or []:
        for v in r.get("Vulnerabilities") or []:
            severities[v["Severity"]] = severities.get(v["Severity"], 0) + 1
            if v["Severity"] in BLOCKING_SEVERITIES and v.get("FixedVersion"):
                fixable.append(f"{v['Severity']} {v['VulnerabilityID']} {v.get('PkgName')} {v.get('InstalledVersion')} -> {v['FixedVersion']}")
        for lic in r.get("Licenses") or []:
            licenses.setdefault(lic.get("Category", "unknown"), set()).add(lic.get("Name"))
    return {"severities": severities, "fixableBlocking": sorted(set(fixable)), "licenses": {k: sorted(n for n in v if n) for k, v in licenses.items()}}


def build(name: str, context: str, extra: list[str], run_id: str, out: pathlib.Path) -> str:
    tag = f"{HOST_REGISTRY}/{name}:{run_id}"
    log = out / f"{name}.build.log"
    if SETTINGS["provenance"]:
        # BuildKit's SLSA provenance (mode=max) is pushed with the image (P3.2).
        meta = out / f"{name}.build-metadata.json"
        p = sh(["docker", "buildx", "build", "--provenance=mode=max", "--push", "--metadata-file", str(meta), *extra, "-t", tag, context], cwd=ROOT)
        log.write_text(p.stdout + p.stderr)
        if p.returncode != 0:
            raise RuntimeError(f"build failed, see {log.relative_to(ROOT)}")
        digest = json.loads(meta.read_text()).get("containerimage.digest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RuntimeError("buildx recorded no image digest")
        return digest
    p = sh(["docker", "build", "--network=host", *extra, "-t", tag, context], cwd=ROOT)
    log.write_text(p.stdout + p.stderr)
    if p.returncode != 0:
        raise RuntimeError(f"build failed, see {log.relative_to(ROOT)}")
    pushed = must(["docker", "push", tag])
    m = re.search(r"digest: (sha256:[0-9a-f]{64})", pushed)
    if not m:
        raise RuntimeError("push printed no digest")
    return m[1]


def mirror(upstream: str, target: str) -> str:
    """Copies an upstream image by digest into the registry and returns that digest once
    the registry serves it unchanged."""
    digest = upstream.split("@", 1)[1]
    must(["docker", "buildx", "imagetools", "create", "--tag", f"{HOST_REGISTRY}/{target}", upstream])
    repo = target.split(":", 1)[0]
    req = urllib.request.Request(f"http://{HOST_REGISTRY}/v2/{repo}/manifests/{digest}", method="HEAD", headers={"Accept": ", ".join([
        "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])})
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.headers.get("Docker-Content-Digest") != digest:
            raise RuntimeError(f"the registry serves {repo} as {r.headers.get('Docker-Content-Digest')}, not {digest}")
    return digest


def yaml_version(chart_yaml: pathlib.Path) -> str:
    m = re.search(r"^version:\s*\"?([0-9A-Za-z.+-]+)\"?\s*$", chart_yaml.read_text(), re.M)
    if not m:
        raise RuntimeError(f"{chart_yaml}: no version")
    return m[1]


def job_images() -> list[tuple[str, str, list[str]]]:
    profiles = json.loads((ROOT / "contracts/jobs/profiles.json").read_text())["profiles"]
    seen: dict[tuple[str, str], list[str]] = {}
    for p in profiles:
        for key in ("image", "sidecarImage"):
            img = p.get(key)
            if img:
                seen.setdefault((img["repository"], img["digest"]), []).append(f"{p['profileId']}@{p['revision']}")
    return [(repo, digest, users) for (repo, digest), users in sorted(seen.items())]


def registry_up() -> bool:
    if not SETTINGS["insecure"]:
        return True  # an authenticated TLS registry: the first push proves it
    try:
        with urllib.request.urlopen(f"http://{HOST_REGISTRY}/v2/", timeout=5) as r:
            return r.status == 200
    except OSError:
        return False


def parent_tag() -> str | None:
    """The release tag the parent's HEAD carries (a release builds from a tag)."""
    p = sh(["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=ROOT)
    return (p.stdout.strip() or None) if p.returncode == 0 else None


def findings_of(manifest: dict) -> list[str]:
    """Every blocking finding of a manifest's artifacts: the policy, the signatures and
    attestations, and the cleanliness of every source (P3.2)."""
    found: list[str] = []
    for name, e in manifest["images"].items():
        if e.get("signature") not in ("VERIFIED", "NOT_SIGNED (third-party image, pinned by digest)") or e.get("sbomAttestation", "VERIFIED") != "VERIFIED":
            found.append(f"{name}: signature {e.get('signature')}, attestation {e.get('sbomAttestation')}")
        src = e.get("source")
        if isinstance(src, dict) and src.get("clean") is False:
            found.append(f"{name}: built from a dirty input ({src.get('repository')}: {src.get('uncommittedPaths')} uncommitted or untracked paths)")
        scan_ = e.get("scan") or {}
        for v in scan_.get("fixableBlocking", scan_.get("fixableCritical", [])):
            found.append(f"{name}: policy: fixable {v}")
        if (scan_.get("licenses") or {}).get("forbidden"):
            found.append(f"{name}: policy: forbidden licenses {scan_['licenses']['forbidden']}")
    for name, e in manifest["charts"].items():
        if e.get("signature") != "VERIFIED":
            found.append(f"chart {name}: signature {e.get('signature')}")
        src = e.get("source")
        if isinstance(src, dict) and src.get("clean") is False:
            found.append(f"chart {name}: packaged from a dirty input ({src.get('repository')})")
    return found


def derive_status(findings: list[str], placement: dict) -> tuple[str, list[str]]:
    """BLOCKED on any finding; SIGNED only for a production placement; otherwise
    SIGNED_DEVELOPMENT_ONLY with its reasons (never hard-coded, P3.2)."""
    if findings:
        return "BLOCKED", []
    reasons = []
    if placement.get("signing") == "key" and not placement.get("kmsKey"):
        reasons.append("development signing key under .local/release (no ENV-10 key custody)")
    if placement.get("insecureRegistry"):
        reasons.append("plain-HTTP registry")
    if not placement.get("transparencyLog"):
        reasons.append("no transparency log")
    if not placement.get("provenance"):
        reasons.append("no build provenance")
    if not placement.get("tag"):
        reasons.append("the parent's HEAD carries no release tag")
    return ("SIGNED_DEVELOPMENT_ONLY" if reasons else "SIGNED"), reasons


def self_test() -> int:
    """P3.2 AC2/AC3: the status rules on synthetic manifests, without Docker."""
    clean = {"repository": ".", "clean": True}
    good = {"signature": "VERIFIED", "sbomAttestation": "VERIFIED", "source": clean, "scan": {"fixableBlocking": [], "licenses": {}}}
    production = {"signing": "keyless", "insecureRegistry": False, "transparencyLog": True, "provenance": True, "tag": "v1.0.0"}
    development = {"signing": "key", "insecureRegistry": True, "transparencyLog": False, "provenance": False, "tag": None}

    def manifest(**images) -> dict:
        return {"images": images, "charts": {"anvilkit-agent-api": {"signature": "VERIFIED", "source": clean}}}

    def vuln(severity: str, fixed: str | None) -> dict:
        v = {"Severity": severity, "VulnerabilityID": "CVE-0000-0001", "PkgName": "openssl", "InstalledVersion": "3.0.0"}
        return policy_view({"Results": [{"Vulnerabilities": [{**v, **({"FixedVersion": fixed} if fixed else {})}]}]})

    cases = []
    cases.append(("a clean, verified, keyless, tagged release with provenance is SIGNED",
                  derive_status(findings_of(manifest(api=good)), production)[0] == "SIGNED"))
    st, why = derive_status(findings_of(manifest(api=good)), development)
    cases.append(("the development placement is SIGNED_DEVELOPMENT_ONLY with its five reasons", st == "SIGNED_DEVELOPMENT_ONLY" and len(why) == 5))
    dirty = {**good, "source": {"repository": "services/agent/api", "clean": False, "uncommittedPaths": 3}}
    cases.append(("an image built from a dirty input is BLOCKED", derive_status(findings_of(manifest(api=dirty)), production)[0] == "BLOCKED"))
    cases.append(("a fixable HIGH vulnerability is BLOCKED", derive_status(findings_of(manifest(api={**good, "scan": vuln("HIGH", "3.0.1")})), production)[0] == "BLOCKED"))
    cases.append(("a fixable CRITICAL vulnerability is BLOCKED", derive_status(findings_of(manifest(api={**good, "scan": vuln("CRITICAL", "3.0.1")})), production)[0] == "BLOCKED"))
    unfixable = {**good, "scan": vuln("CRITICAL", None)}
    cases.append(("an unfixable CRITICAL is counted, not blocking",
                  derive_status(findings_of(manifest(api=unfixable)), production)[0] == "SIGNED" and unfixable["scan"]["severities"] == {"CRITICAL": 1}))
    cases.append(("a fixable MEDIUM is counted, not blocking", derive_status(findings_of(manifest(api={**good, "scan": vuln("MEDIUM", "1")})), production)[0] == "SIGNED"))
    cases.append(("an unverified signature is BLOCKED", derive_status(findings_of(manifest(api={**good, "signature": "FAILED"})), production)[0] == "BLOCKED"))
    m = manifest(api=good)
    m["charts"]["anvilkit-agent-api"]["signature"] = "FAILED"
    cases.append(("an unsigned chart is BLOCKED", derive_status(findings_of(m), production)[0] == "BLOCKED"))
    cases.append(("a KMS-held key with the transparency log is SIGNED",
                  derive_status(findings_of(manifest(api=good)), {**production, "signing": "key", "kmsKey": True})[0] == "SIGNED"))
    cases.append(("an untagged parent is not SIGNED", derive_status(findings_of(manifest(api=good)), {**production, "tag": None})[0] == "SIGNED_DEVELOPMENT_ONLY"))
    failures = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(f"{'PASS' if ok else 'FAIL':<5} self-test: {n}")
    print(f"release-artifacts self-test: {len(cases) - len(failures)}/{len(cases)} PASS")
    return 1 if failures else 0


def main() -> int:
    global HOST_REGISTRY, REGISTRY, NETWORK
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    ap.add_argument("--only", action="append", help="limit to these image or chart names (repeatable)")
    ap.add_argument("--no-build", action="store_true", help="reuse the images this run already pushed (manifest of --run)")
    ap.add_argument("--from-run", help="derive this run from another run's manifest: its artifacts are carried over (with their evidence "
                                       "in that run) except those --only selects, which are rebuilt")
    ap.add_argument("--registry-push", default=HOST_REGISTRY, help="the registry as this host pushes to it")
    ap.add_argument("--registry", default=REGISTRY, help="the registry as clusters and the tool containers reach it")
    ap.add_argument("--network", default=NETWORK, help="the Docker network of the tool containers (host in CI)")
    ap.add_argument("--signing", choices=["key", "keyless"], default="key", help="key: --key, by default the development key under .local/release; keyless: CI OIDC")
    ap.add_argument("--key", help="a cosign key reference held outside this machine (hashivault://..., awskms://..., gcpkms://...)")
    ap.add_argument("--certificate-identity", help="keyless: the signer identity verification requires (the release workflow's ref)")
    ap.add_argument("--certificate-oidc-issuer", help="keyless: the OIDC issuer verification requires")
    ap.add_argument("--transparency-log", action="store_true", help="upload key signatures to the transparency log (always on for keyless)")
    ap.add_argument("--provenance", action="store_true", help="build with BuildKit SLSA provenance (docker buildx, pushed with the image)")
    ap.add_argument("--self-test", action="store_true", help="prove the status rules on synthetic manifests and exit")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    HOST_REGISTRY, REGISTRY, NETWORK = a.registry_push, a.registry, a.network
    if a.signing == "keyless" and not (a.certificate_identity and a.certificate_oidc_issuer):
        ap.error("--signing keyless needs --certificate-identity and --certificate-oidc-issuer")
    SETTINGS.update({"signing": a.signing, "key": a.key, "insecure": HOST_REGISTRY in DEVELOPMENT_REGISTRIES or REGISTRY in DEVELOPMENT_REGISTRIES,
                     "tlog": a.signing == "keyless" or a.transparency_log, "identity": a.certificate_identity, "issuer": a.certificate_oidc_issuer,
                     "provenance": a.provenance})
    helm_bin = helm()
    if not shutil.which("docker") or not helm_bin or not registry_up():
        print(f"UNEXECUTED: docker, helm and the registry ({HOST_REGISTRY}) are required")
        return 2
    out = ROOT / "outputs/qualification" / a.run / "release"
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "release-manifest.json"
    prior = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    carried_from = None
    if a.from_run:
        source_manifest = ROOT / "outputs/qualification" / a.from_run / "release/release-manifest.json"
        prior, carried_from = json.loads(source_manifest.read_text()), a.from_run
        if not a.only:
            print("--from-run needs --only: the artifacts to rebuild")
            return 2

    def carry(entry: dict) -> dict:
        """An artifact taken from another run keeps its SBOM, scan and signature evidence there."""
        return {**entry, "evidenceRun": entry.get("evidenceRun", carried_from)} if carried_from else entry
    development_key = a.signing == "key" and not a.key
    password = keypair() if development_key else ""
    CACHE.mkdir(parents=True, exist_ok=True)
    placement = {"signing": a.signing, "kmsKey": bool(a.key), "insecureRegistry": SETTINGS["insecure"], "transparencyLog": SETTINGS["tlog"],
                 "provenance": a.provenance, "tag": parent_tag()}
    if a.signing == "keyless":
        signing = {"mode": "keyless", "certificateIdentity": a.certificate_identity, "certificateOidcIssuer": a.certificate_oidc_issuer, "transparencyLog": "rekor"}
    elif a.key:
        signing = {"mode": "key", "key": a.key, "custody": "held outside this machine (KMS/OpenBao)", "transparencyLog": "rekor" if SETTINGS["tlog"] else "not used"}
    else:
        pub = KEYS / "cosign.pub"
        signing = {"mode": "key", "publicKey": "cosign.pub", "publicKeyDigest": sha256_file(pub),
                   "custody": "DEVELOPMENT_ONLY key under .local/release (ENV-10 custody NOT_VERIFIED)", "transparencyLog": "rekor" if SETTINGS["tlog"] else "not used"}
        shutil.copy(pub, out / "cosign.pub")
    manifest = {"schemaVersion": 1, "run": a.run, "parentTag": placement["tag"],
                "registry": {"push": HOST_REGISTRY, "reference": REGISTRY, "insecure": SETTINGS["insecure"]},
                "signing": signing, "provenance": "BuildKit SLSA mode=max" if a.provenance else "none",
                "tools": {"trivy": TRIVY, "cosign": COSIGN, "helm": must([helm_bin, "version", "--short"]).strip(), "docker": must(["docker", "version", "--format", "{{.Server.Version}}"]).strip()},
                "policy": "block: fixable CRITICAL or HIGH vulnerability; forbidden license category; dirty input; unverified signature or attestation (P3.2 rule pending the security owner)",
                "images": {}, "charts": {}}
    findings: list[str] = []
    want = set(a.only or [])

    def selected(n: str) -> bool:
        return not want or n in want

    for name, context, extra in OWNED:
        if not selected(name):
            if name in prior.get("images", {}):
                manifest["images"][name] = carry(prior["images"][name])
            continue
        print(f"image {name}: ", end="", flush=True)
        try:
            entry = {"kind": "service", "source": source(context), "build": {"context": context, "arguments": extra}}
            digest = prior["images"][name]["digest"] if a.no_build else build(name, context, extra, a.run, out)
            ref = f"{REGISTRY}/{name}@{digest}"
            entry.update({"reference": ref, "digest": digest})
            entry.update(scan(name, ref, out))
            entry.update(sign_and_verify(ref, out, password, f"{name}.spdx.json"))
            manifest["images"][name] = entry
            print(f"{digest[:19]}… signed {entry['signature']}, attestation {entry['sbomAttestation']}, {entry['scan']['severities']}")
        except RuntimeError as e:
            findings.append(f"{name}: {e}")
            print(f"FAILED {e}")
    for repo, digest, users in job_images():
        if repo.startswith("docker.io/"):
            name = repo.rsplit("/", 1)[1]
            ref, owned = f"{repo}@{digest}", False
        else:
            name, ref, owned = repo, f"{REGISTRY}/{repo}@{digest}", True
        key = f"{name}@{digest[7:19]}"
        if not selected(name) and not selected(key):
            if key in prior.get("images", {}):
                manifest["images"][key] = carry(prior["images"][key])
            continue
        print(f"job image {key}: ", end="", flush=True)
        try:
            entry = {"kind": "job" if owned else "third-party job", "reference": ref, "digest": digest, "profiles": users,
                     "source": "the reviewed profile's pinned digest (not rebuilt)"}
            entry.update(scan(key.replace("@", "-"), ref, out))
            entry.update(sign_and_verify(ref, out, password, f"{key.replace('@', '-')}.spdx.json") if owned
                         else {"signature": "NOT_SIGNED (third-party image, pinned by digest)"})
            manifest["images"][key] = entry
            print(f"signed {entry['signature']}, {entry['scan']['severities']}")
        except RuntimeError as e:
            findings.append(f"{key}: {e}")
            print(f"FAILED {e}")
    for name, upstream, target in PLATFORM:
        if not selected(name):
            if name in prior.get("images", {}):
                manifest["images"][name] = carry(prior["images"][name])
            continue
        print(f"platform image {name}: ", end="", flush=True)
        try:
            digest = mirror(upstream, target)
            ref = f"{REGISTRY}/{target.split(':', 1)[0]}@{digest}"
            entry = {"kind": "third-party platform", "reference": ref, "digest": digest,
                     "source": {"upstream": upstream, "mirroredAs": f"{REGISTRY}/{target}"}}
            entry.update(scan(name, ref, out))
            entry.update(sign_and_verify(ref, out, password, f"{name}.spdx.json"))
            manifest["images"][name] = entry
            print(f"{digest[:19]}… signed {entry['signature']}, attestation {entry['sbomAttestation']}, {entry['scan']['severities']}")
        except (RuntimeError, OSError) as e:
            findings.append(f"{name}: {e}")
            print(f"FAILED {e}")
    charts = out / "charts"
    charts.mkdir(exist_ok=True)
    for chart in CHARTS:
        name = chart.split("/")[2]
        name = f"anvilkit-agent-{name}"
        if not selected(name + "-chart") and want:
            if name in prior.get("charts", {}):
                manifest["charts"][name] = carry(prior["charts"][name])
            continue
        print(f"chart {name}: ", end="", flush=True)
        try:
            # A per-run prerelease version: a published chart tag is never overwritten.
            base = yaml_version(ROOT / chart / "Chart.yaml")
            pkg = must([helm_bin, "package", str(ROOT / chart), "-d", str(charts), "--version", f"{base}-qualification.{a.run}"]).strip().split(": ")[-1]
            pushed = sh([helm_bin, "push", pkg, f"oci://{HOST_REGISTRY}/charts", *(["--plain-http"] if SETTINGS["insecure"] else [])])
            m = re.search(r"Digest: (sha256:[0-9a-f]{64})", pushed.stdout + pushed.stderr)
            if pushed.returncode != 0 or not m:
                raise RuntimeError((pushed.stderr or pushed.stdout).strip()[-300:])
            version = re.search(r"-(\d+\.\d+\.\d+[^/]*)\.tgz$", pkg)[1]
            ref = f"{REGISTRY}/charts/{name}@{m[1]}"
            entry = {"version": version, "reference": ref, "digest": m[1], "package": sha256_file(pathlib.Path(pkg)), "source": source(chart)}
            entry.update(sign_and_verify(ref, out, password, None))
            manifest["charts"][name] = entry
            print(f"{version} {m[1][:19]}… signed {entry['signature']}")
        except RuntimeError as e:
            findings.append(f"chart {name}: {e}")
            print(f"FAILED {e}")
    findings += findings_of(manifest)
    if carried_from:
        manifest["derivedFrom"] = {"run": carried_from, "manifestSha256": sha256_file(ROOT / "outputs/qualification" / carried_from / "release/release-manifest.json"),
                                   "rebuilt": sorted(want)}
    manifest["findings"] = findings
    manifest["placement"] = placement
    manifest["status"], manifest["developmentReasons"] = derive_status(findings, placement)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"\nmanifest {manifest_path.relative_to(ROOT)} {sha256_file(manifest_path)}: {manifest['status']}, "
          f"{len(manifest['images'])} images, {len(manifest['charts'])} charts, {len(findings)} findings")
    for f in findings:
        print(f"  {f}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
