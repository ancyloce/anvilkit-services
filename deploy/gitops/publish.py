#!/usr/bin/env python3
"""Publish one environment's pinned combination as signed OCI charts (P23-04, P3.2).

  .local/verification-venv/bin/python deploy/gitops/publish.py --environment qualification --run RUN
  .local/verification-venv/bin/python deploy/gitops/publish.py --self-test

Reads deploy/gitops/environments/<environment>.yaml and the release manifest of RUN
(outputs/qualification/<RUN>/release/release-manifest.json, tools/release-artifacts.py),
then:
  * mirrors every upstream chart at its pinned version into <registry>/platform;
  * packages every parent platform chart (deploy/qualification/platform/<name>) with the
    per-run version <chart version>-<environment>.<RUN> into <registry>/platform;
  * resolves the service applications against the release manifest (chart version,
    image digests and references, release files) and packages the anvilkit-environment
    chart (deploy/gitops/environment) with the resolved values and the environment
    values files as anvilkit-environment-<environment> into <registry>/environments;
  * signs every pushed chart with the release key (no transparency log) and verifies it;
  * writes outputs/qualification/<RUN>/gitops/{environment-manifest.json,root-application.yaml}.

Release gate (P3.2): nothing is published from a release manifest that has findings or a
status other than SIGNED; a development combination (`development: true`, the
qualification environment) also accepts SIGNED_DEVELOPMENT_ONLY. Every image and chart the
combination takes must be VERIFIED and built from a clean source. A combination that names
its release (`release: {run, manifestSha256}`) is published only against exactly that
manifest: promotion is a reviewed change of that field, rollback the revert of it (the
previous release republished as a new environment chart version). A chart already in the
registry is reused only after cosign verifies its signature again, never on the strength
of an earlier run's record. Automated prune and self-heal are on only for a development
combination or one that declares a rollback window (`automation.rollbackWindow`); a
production combination otherwise syncs on an operator's command. An upstream chart whose
combination entry declares `verify` (cosign identity/issuer or a Helm provenance keyring)
is verified before it is mirrored; a production combination refuses an unverified one.
The registry and signing come from the combination (`registryPush`, `signing`) or the
development defaults. The environment manifest's status is derived: PUBLISHED for a
SIGNED release of a production combination, PUBLISHED_DEVELOPMENT_ONLY otherwise.
--self-test proves the gate on synthetic manifests without Docker.

--overlay FILE merges {applications: {<name>: {values: {...}}}} into those applications'
inline values (deploy/qualification/check-waves.py publishes its probe revisions this
way); the overlay and its digest are recorded in the environment manifest.

A combination entry that refers to an image or chart the release manifest lacks is an
error: nothing unpinned is published. Exit 0 on success, 1 on any failure, 2 when
docker, helm or the registry is unavailable.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tools"))
import pyenv_check  # noqa: E402

pyenv_check.require("yaml")
import yaml  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOST_REGISTRY = "localhost:5001"  # development default; the combination's registryPush replaces it
DEVELOPMENT_REGISTRIES = {"localhost:5001", "anvilkit-dev-registry:5000"}
SIGNING: dict = {"mode": "key", "key": None, "identity": None, "issuer": None, "insecure": True, "tlog": False}
COSIGN = "ghcr.io/sigstore/cosign/cosign@sha256:68839b7f13dac5a6744a5d8818e984dd39183374e37855c19e14d623d9bc9037"  # v2.6.1
KEYS = ROOT / ".local/release"


def must(cmd: list[str], **kw) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}: {(p.stderr or p.stdout).strip()[-500:]}")
    return p.stdout + p.stderr


def helm() -> str:
    local = ROOT / ".local/bin/helm"
    return str(local) if local.exists() else (shutil.which("helm") or "")


def sha256(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def set_path(values: dict, dotted: str, value) -> None:
    cur = values
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def release_gate(manifest: dict, development: bool, pinned: dict | None, manifest_sha: str) -> list[str]:
    """Why a release manifest may not be published (empty: it may), P3.2."""
    problems = []
    allowed = {"SIGNED", "SIGNED_DEVELOPMENT_ONLY"} if development else {"SIGNED"}
    if manifest.get("status") not in allowed:
        problems.append(f"release status {manifest.get('status')} (publishable: {', '.join(sorted(allowed))})")
    if manifest.get("findings"):
        problems.append(f"the release manifest has {len(manifest['findings'])} findings: {manifest['findings'][0]}")
    if pinned and pinned.get("manifestSha256") and pinned["manifestSha256"] != manifest_sha:
        problems.append(f"the combination pins release manifest {pinned['manifestSha256']}, this one is {manifest_sha}")
    return problems


def artifact_problem(kind: str, name: str, entry: dict | None) -> str | None:
    """A combination entry may take only a verified artifact built from a clean source."""
    if not entry:
        return f"the release manifest has no {kind} {name}"
    if entry.get("signature") != "VERIFIED":
        return f"{kind} {name}: signature {entry.get('signature')}"
    src = entry.get("source")
    if isinstance(src, dict) and src.get("clean") is False:
        return f"{kind} {name}: built from a dirty input"
    return None


def sync_policy(development: bool, automation: dict | None) -> dict:
    """Automated prune/self-heal only for a development combination or with a declared
    rollback window; a production combination otherwise syncs on command."""
    if development or (automation or {}).get("rollbackWindow"):
        return {"automated": {"prune": True, "selfHeal": True}}
    return {}


# The root's sync walks every wave, so one child's transient Degraded reading (a
# fresh Certificate before cert-manager marks it Issuing) would otherwise end the
# whole rollout, and Argo CD does not retry a failed revision on its own. The same
# retry as the children's (environment/templates/applications.yaml).
RETRY = {"limit": 5, "backoff": {"duration": "10s", "factor": 2, "maxDuration": "3m"}}


def root_sync_policy(development: bool, automation: dict | None) -> dict:
    return {**sync_policy(development, automation), "syncOptions": ["RespectIgnoreDifferences=true"], "retry": RETRY}


def self_test() -> int:
    """P3.2 AC3: the release gate, the artifact rule, the reuse rule and the sync policy."""
    signed = {"status": "SIGNED", "findings": []}
    cases = [
        ("a BLOCKED manifest is refused", bool(release_gate({"status": "BLOCKED", "findings": ["x: policy"]}, True, None, "sha256:a"))),
        ("a manifest with findings is refused even when its status says SIGNED", bool(release_gate({"status": "SIGNED", "findings": ["x"]}, False, None, "sha256:a"))),
        ("SIGNED_DEVELOPMENT_ONLY is refused for a production combination", bool(release_gate({"status": "SIGNED_DEVELOPMENT_ONLY", "findings": []}, False, None, "sha256:a"))),
        ("SIGNED_DEVELOPMENT_ONLY is accepted for a development combination", not release_gate({"status": "SIGNED_DEVELOPMENT_ONLY", "findings": []}, True, None, "sha256:a")),
        ("a SIGNED manifest is accepted", not release_gate(signed, False, None, "sha256:a")),
        ("a manifest other than the one the combination pins is refused", bool(release_gate(signed, False, {"run": "r", "manifestSha256": "sha256:b"}, "sha256:a"))),
        ("an unsigned chart is refused", artifact_problem("chart", "c", {"signature": "FAILED"}) is not None),
        ("an image built from a dirty input is refused", artifact_problem("image", "i", {"signature": "VERIFIED", "source": {"clean": False}}) is not None),
        ("a verified, clean image is accepted", artifact_problem("image", "i", {"signature": "VERIFIED", "source": {"clean": True}}) is None),
        ("an already published chart whose signature does not verify is refused",
         reuse_published("platform", "c", "1.0.0", "sha256:" + "0" * 64, lambda ref: False)["signature"] == "FAILED"),
        ("an already published chart is reused only after it verifies",
         reuse_published("platform", "c", "1.0.0", "sha256:" + "0" * 64, lambda ref: True)["signature"] == "VERIFIED (re-verified)"),
        ("production syncs on command without a rollback window", sync_policy(False, None) == {}),
        ("production with a declared rollback window automates", "automated" in sync_policy(False, {"rollbackWindow": "30m"})),
        ("development automates", "automated" in sync_policy(True, None)),
        ("the root retries a failed sync like its children, automated or not",
         root_sync_policy(True, None)["retry"] == RETRY and root_sync_policy(False, None)["retry"] == RETRY
         and "automated" not in root_sync_policy(False, None)),
    ]
    failures = [n for n, ok in cases if not ok]
    for n, ok in cases:
        print(f"{'PASS' if ok else 'FAIL':<5} self-test: {n}")
    print(f"publish self-test: {len(cases) - len(failures)}/{len(cases)} PASS")
    return 1 if failures else 0


def published_digest(path: str, name: str, version: str) -> str | None:
    """The digest of an already published chart version: a published tag is never
    overwritten, so a republish reuses it once its signature verifies again."""
    scheme = "http" if SIGNING["insecure"] else "https"
    req = urllib.request.Request(f"{scheme}://{HOST_REGISTRY}/v2/{path}/{name}/manifests/{version.replace('+', '_')}", method="HEAD",
                                 headers={"Accept": "application/vnd.oci.image.manifest.v1+json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.headers.get("Docker-Content-Digest")
    except OSError:
        return None


def merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        base[key] = merge(base.get(key) or {}, value) if isinstance(value, dict) else value
    return base


def cosign_base() -> list[str]:
    base = ["docker", "run", "--rm", "--network", SIGNING.get("network", "kind"), "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{KEYS}:/keys:ro",
            "-e", "DOCKER_CONFIG=/tmp/docker"]
    config = pathlib.Path(os.environ.get("DOCKER_CONFIG", pathlib.Path.home() / ".docker")) / "config.json"
    if not SIGNING["insecure"] and config.is_file():
        base += ["-v", f"{config}:/tmp/docker/config.json:ro"]
    if SIGNING["mode"] == "keyless":
        base += [x for k in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN") if os.environ.get(k) for x in ("-e", k)]
    return base


def cosign_flags() -> tuple[list[str], list[str]]:
    registry = ["--allow-http-registry", "--allow-insecure-registry"] if SIGNING["insecure"] else []
    if SIGNING["mode"] == "keyless":
        # The release workflow signs the service charts, the promote workflow the
        # environment chart: one identity pattern names both (signing.certificateIdentityRegexp).
        return registry, registry + ["--certificate-identity-regexp", SIGNING["identity"], "--certificate-oidc-issuer", SIGNING["issuer"]]
    key, pub = SIGNING["key"] or "/keys/cosign.key", SIGNING["key"] or "/keys/cosign.pub"
    return (["--key", key, *([] if SIGNING["tlog"] else ["--tlog-upload=false"]), *registry],
            ["--key", pub, *([] if SIGNING["tlog"] else ["--insecure-ignore-tlog=true"]), *registry])


def verify_signature(ref: str) -> bool:
    _, verify = cosign_flags()
    p = subprocess.run(cosign_base() + [COSIGN, "verify", *verify, ref], capture_output=True, text=True)
    return p.returncode == 0 and ref.split("@")[1] in p.stdout


def reuse_published(path: str, name: str, version: str, digest: str, verify=None) -> dict:
    """An already published chart version, reused only after its signature verifies now
    (P3.2: never "VERIFIED (published earlier)" without a cosign call)."""
    ref = f"{REGISTRY_REF[0]}/{path}/{name}@{digest}"
    ok = (verify or verify_signature)(ref)
    return {"name": name, "version": version, "reference": ref, "digest": digest, "signature": "VERIFIED (re-verified)" if ok else "FAILED"}


REGISTRY_REF = ["anvilkit-dev-registry:5000"]


def push(helm_bin: str, pkg: pathlib.Path, path: str, registry: str, password: str) -> dict:
    out = must([helm_bin, "push", str(pkg), f"oci://{HOST_REGISTRY}/{path}", *(["--plain-http"] if SIGNING["insecure"] else [])])
    digest = re.search(r"Digest: (sha256:[0-9a-f]{64})", out)[1]
    name, version = re.match(r"(.+)-(v?\d+\.\d+\.\d+.*)\.tgz$", pkg.name).groups()
    ref = f"{registry}/{path}/{name}@{digest}"
    sign, _ = cosign_flags()
    env = {**os.environ, "COSIGN_PASSWORD": password} if password else dict(os.environ)
    must(cosign_base() + (["-e", "COSIGN_PASSWORD"] if password else []) + [COSIGN, "sign", "--yes", *sign, ref], env=env)  # never on a command line
    return {"name": name, "version": version, "reference": ref, "digest": digest, "package": sha256(pkg),
            "signature": "VERIFIED" if verify_signature(ref) else "FAILED"}


def verify_upstream(helm_bin: str, chart: dict, pkg: pathlib.Path) -> str:
    """An upstream chart's provenance before it is mirrored (P3.2): a cosign identity for
    an OCI chart or a Helm provenance keyring; UNVERIFIED when the entry declares none."""
    v = chart.get("verify") or {}
    if "cosign" in v:
        ref = chart["upstream"].removeprefix("oci://") + f"/{chart['name']}:{chart['version']}"
        p = subprocess.run(["docker", "run", "--rm", COSIGN, "verify", "--certificate-identity-regexp", v["cosign"]["identity"],
                            "--certificate-oidc-issuer", v["cosign"]["issuer"], ref], capture_output=True, text=True)
        return "VERIFIED (cosign)" if p.returncode == 0 else "FAILED"
    if "keyring" in v:
        p = subprocess.run([helm_bin, "verify", str(pkg), "--keyring", str(ROOT / v["keyring"])], capture_output=True, text=True)
        return "VERIFIED (helm provenance)" if p.returncode == 0 else "FAILED"
    return "UNVERIFIED (no upstream provenance declared)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--environment", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--revision", type=int, default=1, help="the environment chart's revision within the run (a published version is never overwritten)")
    ap.add_argument("--overlay", type=pathlib.Path, help="per-application values merged over the combination (a probe revision)")
    ap.add_argument("--self-test", action="store_true", help="prove the release gate on synthetic inputs and exit")
    if "--self-test" in sys.argv[1:]:
        return self_test()
    a = ap.parse_args()
    helm_bin = helm()
    if not helm_bin or not shutil.which("docker"):
        print("UNEXECUTED: helm and docker are required")
        return 2
    combination = yaml.safe_load((ROOT / "deploy/gitops/environments" / f"{a.environment}.yaml").read_text())
    overlay = yaml.safe_load(a.overlay.read_text()) if a.overlay else {}
    unknown = set(overlay.get("applications") or {}) - {app["name"] for app in combination["applications"]}
    if unknown:
        raise SystemExit(f"the overlay names applications the combination lacks: {sorted(unknown)}")
    release_dir = ROOT / "outputs/qualification" / a.run / "release"
    manifest = json.loads((release_dir / "release-manifest.json").read_text())
    development = bool(combination.get("development"))
    pinned = combination.get("release")
    if pinned and pinned.get("run") and pinned["run"] != a.run:
        raise SystemExit(f"the combination pins release run {pinned['run']}, not {a.run}: promotion is a reviewed change of its release field")
    problems = release_gate(manifest, development, pinned, sha256(release_dir / "release-manifest.json"))
    if problems:
        for problem in problems:
            print(f"REFUSED {problem}")
        return 1
    global HOST_REGISTRY
    HOST_REGISTRY = combination.get("registryPush", HOST_REGISTRY)
    registry = combination["registry"]
    REGISTRY_REF[0] = registry
    signing = combination.get("signing") or {}
    SIGNING.update({"mode": signing.get("mode", "key"), "key": signing.get("key"), "identity": signing.get("certificateIdentityRegexp"),
                    "issuer": signing.get("certificateOidcIssuer"), "insecure": HOST_REGISTRY in DEVELOPMENT_REGISTRIES or registry in DEVELOPMENT_REGISTRIES,
                    "tlog": signing.get("mode") == "keyless" or bool(signing.get("transparencyLog")), "network": signing.get("network", "kind")})
    out = ROOT / "outputs/qualification" / a.run / "gitops"
    out.mkdir(parents=True, exist_ok=True)
    password = (KEYS / "cosign.password").read_text() if SIGNING["mode"] == "key" and not SIGNING["key"] else ""
    published: dict[str, dict] = {}
    resolved_apps = []
    version = env_version = f"0.1.0-{a.environment}.{a.run}" + (f".{a.revision}" if a.revision > 1 else "")
    if published_digest("environments", f"anvilkit-environment-{a.environment}", version):
        raise SystemExit(f"anvilkit-environment-{a.environment} {version} is already published: pass the next --revision")
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp)
        env_chart = work / "anvilkit-environment"
        shutil.copytree(ROOT / "deploy/gitops/environment", env_chart)
        for app in combination["applications"]:
            entry = {k: v for k, v in app.items() if k not in ("imageDigests", "imageRefs", "releaseFiles", "environmentRevision")}
            values = merge(copy.deepcopy(app.get("values") or {}), copy.deepcopy(((overlay.get("applications") or {}).get(app["name"]) or {}).get("values") or {}))
            chart = app["chart"]
            if "upstream" in chart:
                key = f"{chart['name']}-{chart['version']}"
                existing = published_digest("platform", chart["name"], chart["version"])
                if key not in published and existing:
                    published[key] = reuse_published("platform", chart["name"], chart["version"], existing) | {"upstream": chart["upstream"]}
                    if published[key]["signature"] == "FAILED":
                        raise SystemExit(f"{app['name']}: the published chart {key} does not verify with the release signing; refusing to reuse it")
                if key not in published:
                    pull = ["pull", chart["upstream"], "--version", chart["version"], "-d", str(work)]
                    if not chart["upstream"].startswith("oci://"):
                        pull = ["pull", chart["name"], "--repo", chart["upstream"], "--version", chart["version"], "-d", str(work)]
                    if (chart.get("verify") or {}).get("keyring"):
                        pull.append("--prov")
                    must([helm_bin, *pull])
                    pkg = next(work.glob(f"{chart['name']}-*.tgz"))
                    provenance = verify_upstream(helm_bin, chart, pkg)
                    if provenance == "FAILED" or (not development and provenance.startswith("UNVERIFIED")):
                        raise SystemExit(f"{app['name']}: upstream chart {chart['name']} {chart['version']}: provenance {provenance}")
                    published[key] = push(helm_bin, pkg, "platform", registry, password) | {"upstream": chart["upstream"], "upstreamProvenance": provenance}
                    pkg.unlink()
                    for prov in work.glob(f"{chart['name']}-*.tgz.prov"):
                        prov.unlink()
                entry["chart"] = {"path": "platform", "name": chart["name"], "version": chart["version"]}
            elif "source" in chart:
                src = ROOT / chart["source"]
                base = re.search(r"^version:\s*(\S+)", (src / "Chart.yaml").read_text(), re.M)[1]
                chart_version = f"{base}-{a.environment}.{a.run}"
                key = f"{chart['name']}-{chart_version}"
                existing = published_digest("platform", chart["name"], chart_version)
                if key not in published and existing:
                    published[key] = reuse_published("platform", chart["name"], chart_version, existing) | {"source": chart["source"]}
                    if published[key]["signature"] == "FAILED":
                        raise SystemExit(f"{app['name']}: the published chart {key} does not verify with the release signing; refusing to reuse it")
                if key not in published:
                    pkg = pathlib.Path(must([helm_bin, "package", str(src), "-d", str(work), "--version", chart_version]).strip().split(": ")[-1])
                    published[key] = push(helm_bin, pkg, "platform", registry, password) | {"source": chart["source"]}
                entry["chart"] = {"path": "platform", "name": chart["name"], "version": chart_version}
            else:
                rel = manifest["charts"].get(chart["release"])
                if problem := artifact_problem("chart", chart["release"], rel):
                    raise SystemExit(f"{app['name']}: {problem}")
                entry["chart"] = {"path": "charts", "name": chart["release"], "version": rel["version"]}
            for path, image in (app.get("imageDigests") or {}).items():
                img = manifest["images"].get(image)
                if problem := artifact_problem("image", image, img):
                    raise SystemExit(f"{app['name']}: {problem}")
                set_path(values, path, img["digest"])
            for path, image in (app.get("imageRefs") or {}).items():
                img = manifest["images"].get(image)
                if problem := artifact_problem("image", image, img):
                    raise SystemExit(f"{app['name']}: {problem}")
                set_path(values, path, img["reference"])
            for path, name in (app.get("releaseFiles") or {}).items():
                set_path(values, path, (release_dir / name).read_text())
            if app.get("environmentRevision"):
                set_path(values, app["environmentRevision"], env_version)
            if app.get("valuesFile"):
                dest = env_chart / app["valuesFile"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(ROOT / app["valuesFile"], dest)
            if values:
                entry["values"] = values
            resolved_apps.append(entry)
        resolved = {k: v for k, v in combination.items() if k != "applications"} | {"applications": resolved_apps}
        (env_chart / "values.yaml").write_text(yaml.safe_dump(resolved, sort_keys=False))
        (env_chart / "Chart.yaml").write_text(re.sub(r"^name:.*$", f"name: anvilkit-environment-{a.environment}",
                                                     (env_chart / "Chart.yaml").read_text(), flags=re.M))
        must([helm_bin, "template", "check", str(env_chart)])
        pkg = pathlib.Path(must([helm_bin, "package", str(env_chart), "-d", str(work), "--version", env_version]).strip().split(": ")[-1])
        env_entry = push(helm_bin, pkg, "environments", registry, password)
        shutil.copy(env_chart / "values.yaml", out / "environment-values.yaml")
    root = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": f"anvilkit-{a.environment}", "namespace": combination["argocdNamespace"],
                     "finalizers": ["resources-finalizer.argocd.argoproj.io"]},
        "spec": {"project": "default",
                 "source": {"repoURL": f"{registry}/environments", "chart": env_entry["name"], "targetRevision": env_entry["version"]},
                 "destination": {"server": "https://kubernetes.default.svc", "namespace": combination["argocdNamespace"]},
                 # Argo CD adds its own pre-delete finalizers to a child whose chart has
                 # PreDelete hooks (Kyverno): the children's finalizers are the controller's.
                 "ignoreDifferences": [{"group": "argoproj.io", "kind": "Application", "jsonPointers": ["/metadata/finalizers"]}],
                 "syncPolicy": root_sync_policy(development, combination.get("automation"))},
    }
    (out / "root-application.yaml").write_text(yaml.safe_dump(root, sort_keys=False))
    record = {"schemaVersion": 1, "environment": a.environment, "run": a.run, "registry": registry, "development": development,
              "release": {"run": a.run, "status": manifest.get("status"), "pinnedByCombination": bool(pinned)},
              "releaseManifest": sha256(release_dir / "release-manifest.json"),
              "environmentChart": env_entry, "platformCharts": published,
              "overlay": {"path": str(a.overlay), "sha256": sha256(a.overlay), "applications": sorted(overlay.get("applications") or {})} if a.overlay else None,
              "applications": [{"name": x["name"], "wave": x["wave"], "layer": x["layer"], "chart": x["chart"]} for x in resolved_apps]}
    failed = [c for c in [env_entry, *published.values()] if not c["signature"].startswith("VERIFIED")]
    record["status"] = "FAILED" if failed else ("PUBLISHED" if manifest.get("status") == "SIGNED" and not development else "PUBLISHED_DEVELOPMENT_ONLY")
    (out / "environment-manifest.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"environment chart {env_entry['name']} {env_entry['version']} {env_entry['digest'][:19]}… signature {env_entry['signature']}")
    for key, c in published.items():
        print(f"  platform {key:<52} {c['digest'][:19]}… {c['signature']}")
    print(f"{record['status']}: outputs/qualification/{a.run}/gitops/environment-manifest.json")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
