#!/usr/bin/env python3
"""Publish one environment's pinned combination as signed OCI charts (P23-04).

  .local/verification-venv/bin/python deploy/gitops/publish.py --environment qualification --run RUN

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
HOST_REGISTRY = "localhost:5001"
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


def published_digest(path: str, name: str, version: str) -> str | None:
    """The digest of an already published chart version: a published tag is never
    overwritten, so a republish reuses it (and its signature) unchanged."""
    req = urllib.request.Request(f"http://{HOST_REGISTRY}/v2/{path}/{name}/manifests/{version.replace('+', '_')}", method="HEAD",
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


def push(helm_bin: str, pkg: pathlib.Path, path: str, registry: str, password: str) -> dict:
    out = must([helm_bin, "push", str(pkg), f"oci://{HOST_REGISTRY}/{path}", "--plain-http"])
    digest = re.search(r"Digest: (sha256:[0-9a-f]{64})", out)[1]
    name, version = re.match(r"(.+)-(v?\d+\.\d+\.\d+.*)\.tgz$", pkg.name).groups()
    ref = f"{registry}/{path}/{name}@{digest}"
    flags = ["--allow-http-registry", "--allow-insecure-registry"]
    base = ["docker", "run", "--rm", "--network", "kind", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{KEYS}:/keys:ro"]
    must(base + ["-e", "COSIGN_PASSWORD", COSIGN, "sign", "--yes", "--key", "/keys/cosign.key", "--tlog-upload=false", *flags, ref],
         env={**os.environ, "COSIGN_PASSWORD": password})  # never on a command line
    verified = must(base + [COSIGN, "verify", "--key", "/keys/cosign.pub", "--insecure-ignore-tlog=true", *flags, ref])
    return {"name": name, "version": version, "reference": ref, "digest": digest, "package": sha256(pkg),
            "signature": "VERIFIED" if digest in verified else "FAILED"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--environment", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--revision", type=int, default=1, help="the environment chart's revision within the run (a published version is never overwritten)")
    ap.add_argument("--overlay", type=pathlib.Path, help="per-application values merged over the combination (a probe revision)")
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
    out = ROOT / "outputs/qualification" / a.run / "gitops"
    out.mkdir(parents=True, exist_ok=True)
    password = (KEYS / "cosign.password").read_text()
    registry = combination["registry"]
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
                    published[key] = {"name": chart["name"], "version": chart["version"], "reference": f"{registry}/platform/{chart['name']}@{existing}",
                                      "digest": existing, "signature": "VERIFIED (published earlier)", "upstream": chart["upstream"]}
                if key not in published:
                    pull = ["pull", chart["upstream"], "--version", chart["version"], "-d", str(work)]
                    if not chart["upstream"].startswith("oci://"):
                        pull = ["pull", chart["name"], "--repo", chart["upstream"], "--version", chart["version"], "-d", str(work)]
                    must([helm_bin, *pull])
                    pkg = next(work.glob(f"{chart['name']}-*.tgz"))
                    published[key] = push(helm_bin, pkg, "platform", registry, password) | {"upstream": chart["upstream"]}
                    pkg.unlink()
                entry["chart"] = {"path": "platform", "name": chart["name"], "version": chart["version"]}
            elif "source" in chart:
                src = ROOT / chart["source"]
                base = re.search(r"^version:\s*(\S+)", (src / "Chart.yaml").read_text(), re.M)[1]
                chart_version = f"{base}-{a.environment}.{a.run}"
                key = f"{chart['name']}-{chart_version}"
                existing = published_digest("platform", chart["name"], chart_version)
                if key not in published and existing:
                    published[key] = {"name": chart["name"], "version": chart_version, "reference": f"{registry}/platform/{chart['name']}@{existing}",
                                      "digest": existing, "signature": "VERIFIED (published earlier)", "source": chart["source"]}
                if key not in published:
                    pkg = pathlib.Path(must([helm_bin, "package", str(src), "-d", str(work), "--version", chart_version]).strip().split(": ")[-1])
                    published[key] = push(helm_bin, pkg, "platform", registry, password) | {"source": chart["source"]}
                entry["chart"] = {"path": "platform", "name": chart["name"], "version": chart_version}
            else:
                rel = manifest["charts"].get(chart["release"])
                if not rel or rel.get("signature") != "VERIFIED":
                    raise SystemExit(f"{app['name']}: the release manifest has no verified chart {chart['release']}")
                entry["chart"] = {"path": "charts", "name": chart["release"], "version": rel["version"]}
            for path, image in (app.get("imageDigests") or {}).items():
                img = manifest["images"].get(image)
                if not img or img.get("signature") != "VERIFIED":
                    raise SystemExit(f"{app['name']}: the release manifest has no verified image {image}")
                set_path(values, path, img["digest"])
            for path, image in (app.get("imageRefs") or {}).items():
                img = manifest["images"].get(image)
                if not img or img.get("signature") != "VERIFIED":
                    raise SystemExit(f"{app['name']}: the release manifest has no verified image {image}")
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
                 "syncPolicy": {"automated": {"prune": True, "selfHeal": True}, "syncOptions": ["RespectIgnoreDifferences=true"]}},
    }
    (out / "root-application.yaml").write_text(yaml.safe_dump(root, sort_keys=False))
    record = {"schemaVersion": 1, "environment": a.environment, "run": a.run, "registry": registry,
              "releaseManifest": sha256(release_dir / "release-manifest.json"),
              "environmentChart": env_entry, "platformCharts": published,
              "overlay": {"path": str(a.overlay), "sha256": sha256(a.overlay), "applications": sorted(overlay.get("applications") or {})} if a.overlay else None,
              "applications": [{"name": x["name"], "wave": x["wave"], "layer": x["layer"], "chart": x["chart"]} for x in resolved_apps]}
    failed = [c for c in [env_entry, *published.values()] if not c["signature"].startswith("VERIFIED")]
    record["status"] = "FAILED" if failed else "PUBLISHED_DEVELOPMENT_ONLY"
    (out / "environment-manifest.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"environment chart {env_entry['name']} {env_entry['version']} {env_entry['digest'][:19]}… signature {env_entry['signature']}")
    for key, c in published.items():
        print(f"  platform {key:<52} {c['digest'][:19]}… {c['signature']}")
    print(f"{record['status']}: outputs/qualification/{a.run}/gitops/environment-manifest.json")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
