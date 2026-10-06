#!/usr/bin/env python3
"""Release artifacts (P23-02): build, publish, scan, SBOM and sign the release candidate.

  .local/verification-venv/bin/python tools/release-artifacts.py [--run ID] [--only NAME ...] [--no-build] [--from-run RUN]

Owned service images (the eight services, the Knowledge forwarder sidecar and the
knowledge/mcp migration Job) are built from the current working trees with each
repository's own Dockerfile and pushed to the registry; the fixed Job images are taken
at the exact digests their reviewed profiles pin (contracts/jobs/profiles.json), never
rebuilt. For every image, by digest: a CycloneDX SBOM and a vulnerability and license
scan (Trivy), a Cosign signature and a signed SBOM attestation, both verified with the
public key. The eight service charts are packaged, pushed as OCI artifacts and signed.
Everything is bound in outputs/qualification/<run>/release-manifest.json together with
each source's repository head, cleanliness and build-context digest and the pinned
tool images.

DEVELOPMENT_ONLY placement: the registry is the development foundation's CNCF
Distribution registry (E02's alternative), not Harbor; the signing key is generated
under .local/release/ (0600), not held under ENV-10 custody; no signature or
attestation is uploaded to a transparency log (--tlog-upload=false), so nothing leaves
this machine. The release policy blocks on a CRITICAL vulnerability with a fixed
version and on a forbidden license; it is the tool's explicit rule pending the
security owner's input, not a derived threshold.

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
HOST_REGISTRY = "localhost:5001"          # the registry as the host pushes to it
REGISTRY = "anvilkit-dev-registry:5000"   # the same registry as clusters and tools reach it
NETWORK = "kind"
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
    cmd = ["docker", "run", "--rm", "--name", name, "--network", NETWORK, "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{out}:/out", "-v", f"{KEYS}:/keys:ro",
           "-v", f"{CACHE}:/root/.cache/trivy", "-e", "DOCKER_CONFIG=/tmp/docker"]
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


def sign_and_verify(ref: str, out: pathlib.Path, password: str, sbom: str | None) -> dict:
    flags = ["--allow-http-registry", "--allow-insecure-registry"]
    env = {"COSIGN_PASSWORD": password}
    tool(COSIGN, ["sign", "--yes", "--key", "/keys/cosign.key", "--tlog-upload=false", *flags, ref], out, env)
    verified = tool(COSIGN, ["verify", "--key", "/keys/cosign.pub", "--insecure-ignore-tlog=true", *flags, ref], out)
    result = {"signature": "VERIFIED" if ref.split("@")[1] in verified else "FAILED"}
    if sbom:
        tool(COSIGN, ["attest", "--yes", "--key", "/keys/cosign.key", "--tlog-upload=false", "--type", "cyclonedx",
                      "--predicate", f"/out/{sbom}", *flags, ref], out, env)
        att = tool(COSIGN, ["verify-attestation", "--key", "/keys/cosign.pub", "--insecure-ignore-tlog=true", "--type", "cyclonedx", *flags, ref], out)
        result["sbomAttestation"] = "VERIFIED" if att.strip() else "FAILED"
    return result


def scan(name: str, ref: str, out: pathlib.Path) -> dict:
    tool(TRIVY, ["image", "--insecure", "--quiet", "--image-src", "remote", "--format", "cyclonedx", "--output", f"/out/{name}.cdx.json", ref], out,
         expect=out / f"{name}.cdx.json")
    tool(TRIVY, ["image", "--insecure", "--quiet", "--image-src", "remote", "--scanners", "vuln,license", "--format", "json", "--output", f"/out/{name}.trivy.json", ref], out,
         expect=out / f"{name}.trivy.json")
    sbom = json.loads((out / f"{name}.cdx.json").read_text())
    report = json.loads((out / f"{name}.trivy.json").read_text())
    severities: dict[str, int] = {}
    fixable_critical, licenses = [], {}
    for r in report.get("Results") or []:
        for v in r.get("Vulnerabilities") or []:
            severities[v["Severity"]] = severities.get(v["Severity"], 0) + 1
            if v["Severity"] == "CRITICAL" and v.get("FixedVersion"):
                fixable_critical.append(f"{v['VulnerabilityID']} {v.get('PkgName')} {v.get('InstalledVersion')} -> {v['FixedVersion']}")
        for lic in r.get("Licenses") or []:
            cat = lic.get("Category", "unknown")
            licenses.setdefault(cat, set()).add(lic.get("Name"))
    return {"sbom": {"file": f"{name}.cdx.json", "digest": sha256_file(out / f"{name}.cdx.json"), "components": len(sbom.get("components") or [])},
            "scan": {"file": f"{name}.trivy.json", "digest": sha256_file(out / f"{name}.trivy.json"), "severities": severities,
                     "fixableCritical": sorted(set(fixable_critical)), "licenses": {k: sorted(n for n in v if n) for k, v in licenses.items()}}}


def build(name: str, context: str, extra: list[str], run_id: str, out: pathlib.Path) -> str:
    tag = f"{HOST_REGISTRY}/{name}:{run_id}"
    log = out / f"{name}.build.log"
    p = sh(["docker", "build", "--network=host", *extra, "-t", tag, context], cwd=ROOT)
    log.write_text(p.stdout + p.stderr)
    if p.returncode != 0:
        raise RuntimeError(f"build failed, see {log.relative_to(ROOT)}")
    pushed = must(["docker", "push", tag])
    m = re.search(r"digest: (sha256:[0-9a-f]{64})", pushed)
    if not m:
        raise RuntimeError("push printed no digest")
    return m[1]


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
    try:
        with urllib.request.urlopen(f"http://{HOST_REGISTRY}/v2/", timeout=5) as r:
            return r.status == 200
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    ap.add_argument("--only", action="append", help="limit to these image or chart names (repeatable)")
    ap.add_argument("--no-build", action="store_true", help="reuse the images this run already pushed (manifest of --run)")
    ap.add_argument("--from-run", help="derive this run from another run's manifest: its artifacts are carried over (with their evidence "
                                       "in that run) except those --only selects, which are rebuilt")
    a = ap.parse_args()
    helm_bin = helm()
    if not shutil.which("docker") or not helm_bin or not registry_up():
        print("UNEXECUTED: docker, helm and the development registry (localhost:5001) are required")
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
    password = keypair()
    pub = KEYS / "cosign.pub"
    manifest = {"schemaVersion": 1, "run": a.run, "placement": "DEVELOPMENT_ONLY",
                "registry": {"push": HOST_REGISTRY, "reference": REGISTRY, "product": "CNCF Distribution (E02 alternative); Harbor NOT_RUN"},
                "signing": {"publicKey": "cosign.pub", "publicKeyDigest": sha256_file(pub), "custody": "DEVELOPMENT_ONLY key under .local/release (ENV-10 custody NOT_VERIFIED)", "transparencyLog": "not used"},
                "tools": {"trivy": TRIVY, "cosign": COSIGN, "helm": must([helm_bin, "version", "--short"]).strip(), "docker": must(["docker", "version", "--format", "{{.Server.Version}}"]).strip()},
                "policy": "block: CRITICAL vulnerability with a fixed version; forbidden license category (DEVELOPMENT_ONLY rule pending the security owner)",
                "images": {}, "charts": {}}
    shutil.copy(pub, out / "cosign.pub")
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
            entry.update(sign_and_verify(ref, out, password, f"{name}.cdx.json"))
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
            entry.update(sign_and_verify(ref, out, password, f"{key.replace('@', '-')}.cdx.json") if owned
                         else {"signature": "NOT_SIGNED (third-party image, pinned by digest)"})
            manifest["images"][key] = entry
            print(f"signed {entry['signature']}, {entry['scan']['severities']}")
        except RuntimeError as e:
            findings.append(f"{key}: {e}")
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
            pushed = sh([helm_bin, "push", pkg, f"oci://{HOST_REGISTRY}/charts", "--plain-http"])
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
    for name, e in manifest["images"].items():
        if e.get("signature") not in ("VERIFIED", "NOT_SIGNED (third-party image, pinned by digest)") or e.get("sbomAttestation", "VERIFIED") != "VERIFIED":
            findings.append(f"{name}: signature {e.get('signature')}, attestation {e.get('sbomAttestation')}")
        for v in e["scan"]["fixableCritical"]:
            findings.append(f"{name}: policy: fixable CRITICAL {v}")
        if e["scan"]["licenses"].get("forbidden"):
            findings.append(f"{name}: policy: forbidden licenses {e['scan']['licenses']['forbidden']}")
    for name, e in manifest["charts"].items():
        if e.get("signature") != "VERIFIED":
            findings.append(f"chart {name}: signature {e.get('signature')}")
    if carried_from:
        manifest["derivedFrom"] = {"run": carried_from, "manifestSha256": sha256_file(ROOT / "outputs/qualification" / carried_from / "release/release-manifest.json"),
                                   "rebuilt": sorted(want)}
    manifest["findings"] = findings
    manifest["status"] = "BLOCKED" if findings else "SIGNED_DEVELOPMENT_ONLY"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"\nmanifest {manifest_path.relative_to(ROOT)} {sha256_file(manifest_path)}: {manifest['status']}, "
          f"{len(manifest['images'])} images, {len(manifest['charts'])} charts, {len(findings)} findings")
    for f in findings:
        print(f"  {f}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
