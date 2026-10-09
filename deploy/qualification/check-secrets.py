"""P0.6 checks of secret delivery and data-plane transport (DEVELOPMENT_ONLY).

  .local/verification-venv/bin/python deploy/qualification/check-secrets.py --rendered [--out DIR]
  .local/verification-venv/bin/python deploy/qualification/check-secrets.py --cluster --run RUN

--rendered (AC1/AC2 on what the environment would apply): renders every service
chart of the checkout with its qualification values (deploy/qualification/values)
and every application of the qualification combination (deploy/gitops/environments/
qualification.yaml) with its inline values (platform charts from the checkout,
upstream charts from the environment registry's mirror at their pinned versions,
else their upstream repository), then fails on
  * a Secret manifest carrying data (credentials reach the Pods from OpenBao through
    the CSI driver; a chart never ships one),
  * any http://, nats://, redis:// or sslmode= other than verify-full in the rendered
    manifests, unless the occurrence matches one of the reasoned, development-labelled
    exceptions below,
and checks the credentials bootstrap.py writes to OpenBao (kv_payloads on throwaway
values): every DSN sslmode=verify-full with the trust bundle, every queue URL
rediss://, no plaintext endpoint.

--cluster (AC1 on a live environment): lists every Secret of the cluster and
classifies it (cert-manager, CSI-synced, Helm release, ServiceAccount token, an
operator's own); a Secret labelled anvilkit.io/bootstrap or of no known origin in an
anvilkit namespace is a finding. The listing goes to outputs/qualification/<run>/secrets/.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMBINATION = ROOT / "deploy/gitops/environments/qualification.yaml"
VALUES = ROOT / "deploy/qualification/values"
REGISTRY = "localhost:5001"
DIGEST = "sha256:" + "0" * 64
# The service charts of the checkout and the placeholders publish.py fills from the
# release manifest (images by digest).
SERVICES = {
    "anvilkit-agent-control": ("services/agent/control/deploy/chart", {"image.digest": DIGEST}),
    "anvilkit-agent-api": ("services/agent/api/deploy/chart", {"image.digest": DIGEST}),
    "anvilkit-agent-workflow": ("services/agent/workflow/deploy/chart", {"image.digest": DIGEST}),
    "anvilkit-agent-model-proxy": ("services/agent/model-proxy/deploy/chart", {"image.digest": DIGEST}),
    "anvilkit-agent-knowledge": ("services/agent/knowledge/deploy/chart", {"image.digest": DIGEST, "forwarder.image.digest": DIGEST, "relay.image.digest": DIGEST}),
    "anvilkit-agent-mcp": ("services/agent/mcp/deploy/chart", {"image.digest": DIGEST, "relay.image.digest": DIGEST}),
    "anvilkit-agent-background-worker": ("services/agent/background-worker/deploy/chart", {"image.digest": DIGEST}),
}
# The development-labelled exceptions left in this environment, each with its reason.
# A match is (source, regex over the matching line).
EXCEPTIONS = [
    ("*", r"http://anvilkit-qualification-dr:9000", "the DR store is a container outside the cluster (ENV-02); its TLS is not part of this combination"),
    ("*", r"anvilkit-otel-collector\.anvilkit-observability\.svc\.cluster\.local:431[78]", "the OTLP export stays development-labelled (telemetry.otlpTls.mode development; B-36)"),
    ("*", r"http://127\.0\.0\.1|http://localhost|http://0\.0\.0\.0", "a Pod-local health or metrics listener (no network peer)"),
    ("*", r"\bhttp://\[::\]|http_port|\"http_port\"", "a Pod-local listener definition"),
    ("anvilkit-telemetry", r"http://", "the Collector's own receivers and Kubernetes discovery (observability, B-36)"),
    ("kyverno", r"http://", "Kyverno's in-cluster webhooks and metrics (its chart)"),
    ("cert-manager", r"http://", "cert-manager's own health and metrics endpoints"),
    ("metrics-server", r"http://", "metrics-server's own endpoints"),
    ("cloudnative-pg", r"http://", "the operator's own endpoints"),
    ("plugin-barman-cloud", r"http://", "the plugin's own endpoints"),
    ("anvilkit-temporal", r"http://", "Temporal's own HTTP endpoints (pprof/metrics)"),
    ("openbao", r"http://", "OpenBao's chart helpers and documentation strings"),
    ("anvilkit-nats", r"http://|nats://", "NATS's own monitor endpoint and route URL scheme in templates"),
    ("anvilkit-qdrant", r'QDRANT_URL="http://', "the chart's Helm test script (a test hook Argo CD does not run), which switches to https:// under TLS"),
    ("anvilkit-probes", r"api=http://anvilkit-agent-api\.", "the API's public HTTP listener, whose TLS the environment's gateway terminates (ENV-03); the in-cluster probes call it directly"),
    ("secrets-store-csi-driver", r"http://", "the driver's own health endpoints"),
]
# Secrets a platform component keeps for itself (never a workload credential).
COMPONENT_OWNED = {
    ("anvilkit-platform", "openbao-csi-provider-hmac-key"): "the OpenBao CSI provider's own HMAC key (versions of the mounted secrets)",
}
PATTERN = re.compile(r"http://[^\s\"'`,)]+|nats://[^\s\"'`,)]+|redis://[^\s\"'`,)]+|sslmode=[a-z-]+")


def helm() -> str:
    for c in (ROOT / ".local/bin/helm", shutil.which("helm")):
        if c and pathlib.Path(c).exists():
            return str(c)
    raise SystemExit("UNEXECUTED: helm not found")


def run(argv: list[str]) -> str:
    p = subprocess.run(argv, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:4])}: {(p.stderr or p.stdout).strip()[-600:]}")
    return p.stdout


def fetch(chart: dict, cache: pathlib.Path) -> pathlib.Path:
    """An upstream chart at its pinned version: the environment registry's mirror, else
    its repository."""
    dest = cache / f"{chart['name']}-{chart['version']}"
    if (dest / chart["name"]).exists():
        return dest / chart["name"]
    dest.mkdir(parents=True, exist_ok=True)
    try:
        run([helm(), "pull", f"oci://{REGISTRY}/platform/{chart['name']}", "--version", str(chart["version"]), "--plain-http", "--untar", "-d", str(dest)])
    except RuntimeError:
        upstream = chart["upstream"]
        if upstream.startswith("oci://"):
            run([helm(), "pull", upstream, "--version", str(chart["version"]), "--untar", "-d", str(dest)])
        else:
            run([helm(), "pull", chart["name"], "--repo", upstream, "--version", str(chart["version"]), "--untar", "-d", str(dest)])
    return dest / chart["name"]


def set_path(values: dict, dotted: str, value) -> None:
    *parents, leaf = dotted.split(".")
    for part in parents:
        values = values.setdefault(part, {})
    values[leaf] = value


def render_all(cache: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, (chart, sets) in SERVICES.items():
            argv = [helm(), "template", name, str(ROOT / chart), "-n", "anvilkit-apps", "-f", str(VALUES / f"{name}.yaml")]
            for k, v in sets.items():
                argv += ["--set-string", f"{k}={v}"]
            out[name] = run(argv)
        combo = yaml.safe_load(COMBINATION.read_text())
        for app in combo["applications"]:
            chart = app["chart"]
            if "release" in chart:
                continue  # the service charts above
            path = ROOT / chart["source"] if "source" in chart else fetch(chart, cache)
            # The values publish.py resolves from the release (placeholders here).
            values = json.loads(json.dumps(app.get("values") or {}))
            for key in app.get("imageDigests") or {}:
                set_path(values, key, DIGEST)
            for key, image in (app.get("imageRefs") or {}).items():
                set_path(values, key, f"{REGISTRY}/{image}@{DIGEST}")
            for key in app.get("releaseFiles") or {}:
                set_path(values, key, "release file placeholder")
            if app.get("environmentRevision"):
                set_path(values, app["environmentRevision"], "0.0.0-check")
            vf = pathlib.Path(tmp) / f"{app['name']}.yaml"
            vf.write_text(yaml.safe_dump(values))
            out[app["name"]] = run([helm(), "template", app["name"], str(path), "-n", app["namespace"], "-f", str(vf)])
    return out


def excepted(source: str, line: str) -> str | None:
    for src, rx, reason in EXCEPTIONS:
        if (src == "*" or src == source) and re.search(rx, line):
            return reason
    return None


def scan(rendered: dict[str, str]) -> tuple[list[str], list[dict]]:
    findings, exceptions = [], []
    for source, text in rendered.items():
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict) and doc.get("kind") == "Secret" and (doc.get("data") or doc.get("stringData")):
                findings.append(f"{source}: Secret {doc['metadata'].get('name')} carries data in the rendered manifests")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for m in PATTERN.finditer(line):
                hit = m.group(0)
                if hit == "sslmode=verify-full":
                    continue
                reason = excepted(source, line)
                if reason:
                    exceptions.append({"source": source, "match": hit, "reason": reason})
                else:
                    findings.append(f"{source}:{n}: {hit} ({line.strip()[:140]})")
    return findings, exceptions


def bootstrap_payloads() -> list[str]:
    """The URLs bootstrap.py writes to OpenBao, built from throwaway credentials."""
    sys.path.insert(0, str(ROOT / "deploy/qualification"))
    import bootstrap  # noqa: E402
    fake = {"roles": {r: "pw" for r in bootstrap.ROLES}, "objectsRoot": {"user": "u", "password": "p"},
            "objects": {u: {"id": "i", "secret": "s"} for u in bootstrap.OBJECT_USERS},
            "dr": {u: {"id": "i", "secret": "s"} for u in bootstrap.DR_USERS}, "queue": "q", "qdrant": "k",
            "principals": {"probe": "a", "tenantA": "b", "tenantB": "c", "modelProxyWorkflow": "d"},
            "nats": {u: {"seed": "SU", "public": "U"} for u in bootstrap.NATS_USERS}}
    findings = []
    paths = {path for _, _, path in bootstrap.OPENBAO_ROLES}
    for path, data in bootstrap.kv_payloads(fake).items():
        if path not in paths:
            findings.append(f"OpenBao kv/{path}: no role reads it")
        for key, value in data.items():
            for m in PATTERN.finditer(value):
                hit = m.group(0)
                if hit != "sslmode=verify-full":
                    findings.append(f"OpenBao kv/{path} {key}: {hit}")
            if value.startswith("postgres://") and f"sslrootcert={bootstrap.TRUST}" not in value:
                findings.append(f"OpenBao kv/{path} {key}: a DSN without the trust bundle")
    for path in paths - set(bootstrap.kv_payloads(fake)):
        findings.append(f"OpenBao role path kv/{path}: nothing written there")
    return findings


def rendered(out: pathlib.Path | None) -> int:
    cache = pathlib.Path(tempfile.mkdtemp(prefix="anvilkit-charts-"))
    try:
        docs = render_all(cache)
    finally:
        shutil.rmtree(cache, ignore_errors=True)
    findings, exceptions = scan(docs)
    findings += bootstrap_payloads()
    summary = {"recordedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sources": sorted(docs), "findings": findings,
               "exceptions": sorted({(e["source"], e["reason"]) for e in exceptions})}
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "rendered.json").write_text(json.dumps(summary, indent=2, default=list) + "\n")
    print(f"rendered: {len(docs)} sources, {sum(t.count(chr(10)) for t in docs.values())} lines; {len(exceptions)} development-labelled occurrences "
          f"({len(summary['exceptions'])} reasons); {len(findings)} findings")
    for src, reason in summary["exceptions"]:
        print(f"  EXCEPTION {src}: {reason}")
    for f in findings:
        print(f"  FINDING {f}")
    return 1 if findings else 0


def cluster(run_id: str, kubeconfig: str | None) -> int:
    argv = ["kubectl"] + (["--kubeconfig", kubeconfig] if kubeconfig else []) + ["get", "secrets", "-A", "-o", "json"]
    items = json.loads(run(argv))["items"]
    rows, findings = [], []
    for s in items:
        md = s["metadata"]
        labels, ann = md.get("labels") or {}, md.get("annotations") or {}
        if labels.get("anvilkit.io/bootstrap"):
            origin = "bootstrap (finding)"
        elif labels.get("secrets-store.csi.k8s.io/managed") == "true":
            origin = "CSI-synced"
        elif "cert-manager.io/certificate-name" in ann or "cert-manager.io/issuer-name" in ann:
            origin = "cert-manager"
        elif s.get("type") == "helm.sh/release.v1":
            origin = "Helm release record"
        elif s.get("type") == "kubernetes.io/service-account-token":
            origin = "ServiceAccount token"
        elif (md["namespace"], md["name"]) in COMPONENT_OWNED:
            origin = "component-owned: " + COMPONENT_OWNED[(md["namespace"], md["name"])]
        elif labels.get("app.kubernetes.io/part-of") == "argocd":
            origin = "Argo CD's own"
        elif md.get("ownerReferences"):
            origin = "owned by " + ",".join(o["kind"] for o in md["ownerReferences"])
        else:
            origin = "unclassified"
        rows.append({"namespace": md["namespace"], "name": md["name"], "type": s.get("type"), "origin": origin})
        if origin in ("bootstrap (finding)", "unclassified") and md["namespace"].startswith("anvilkit-"):
            findings.append(f"{md['namespace']}/{md['name']}: {origin}")
    out = ROOT / "outputs/qualification" / run_id / "secrets"
    out.mkdir(parents=True, exist_ok=True)
    (out / "secrets.json").write_text(json.dumps({"recordedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "secrets": rows, "findings": findings}, indent=2) + "\n")
    print(f"cluster: {len(rows)} Secrets, {len(findings)} findings -> {out.relative_to(ROOT)}/secrets.json")
    for f in findings:
        print(f"  FINDING {f}")
    return 1 if findings else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rendered", action="store_true")
    mode.add_argument("--cluster", action="store_true")
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--run")
    ap.add_argument("--kubeconfig")
    a = ap.parse_args()
    if a.rendered:
        return rendered(a.out)
    if not a.run:
        ap.error("--cluster needs --run")
    return cluster(a.run, a.kubeconfig)


if __name__ == "__main__":
    sys.exit(main())
