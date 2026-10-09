#!/usr/bin/env python3
"""Redacted-telemetry probe of the qualification environment (P23-06).

  .local/verification-venv/bin/python deploy/qualification/check-telemetry.py --run RUN

What the services emit and what the Collector keeps must never contain a secret
(security.md "data classification, logging and deletion"). The probe:
  1. reads every raw container log of the anvilkit-* namespaces (what the services
     write, before any redaction) and the Collector's exported logs, metrics and
     traces from every node (/var/lib/anvilkit-telemetry/*.jsonl);
  2. searches all of it for every actual secret value of the environment
     (.local/qualification/credentials.json: database role passwords, object-store
     and DR keys, the queue password, every bearer principal) and for secret shapes
     (bearer headers, credentialed URLs, presigned-URL signatures, PEM private keys,
     access-key ids, provider API keys);
  3. requires that each signal actually flowed (spans from the services, metrics
     from their endpoints, logs from their containers) so an empty pipeline never
     passes as a clean one;
  4. checks span attributes against the allowlist the services promise.
Writes outputs/qualification/<RUN>/telemetry/telemetry.json with counts only (a
finding names the source and the secret's class, never the value). Exit 1 on any
finding or an empty signal.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / ".local/qualification"
KUBECONFIG = STATE / "kubeconfig"
KUBECTL = str(ROOT / ".local/bin/kubectl")
CLUSTER = "anvilkit-qualification"
SHAPES = {
    "bearer header": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}"),
    "credentialed URL": re.compile(r"[a-z][a-z0-9+.-]*://[^\s:/@\"']+:[^\s@/\"']+@"),
    "presigned signature": re.compile(r"(?i)x-amz-signature=[0-9a-f]{16,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "provider api key": re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
}
ALLOWED_SPAN_KEYS = {
    "http.request.method", "http.route", "http.response.status_code",
    "rpc.system", "rpc.system.name", "rpc.service", "rpc.method", "rpc.grpc.status_code", "rpc.response.status_code",
    "messaging.system", "messaging.destination.name", "anvilkit.delivery.outcome",
    "temporalWorkflowID", "temporalRunID", "temporalActivityID", "temporalUpdateID",
}


def k(*args: str) -> str:
    return subprocess.run([KUBECTL, "--kubeconfig", str(KUBECONFIG), *args], capture_output=True, text=True, check=True).stdout


def secrets_of(creds: dict) -> dict[str, str]:
    """value -> class, for every secret the environment generated."""
    out: dict[str, str] = {}
    for role, pw in creds["roles"].items():
        out[pw] = f"database password ({role})"
    out[creds["objectsRoot"]["password"]] = "object-store root password"
    for user, o in creds["objects"].items():
        out[o["secret"]] = f"object-store secret ({user})"
    for user, o in creds["dr"].items():
        out[o["secret"]] = f"DR store secret ({user})"
    out[creds["queue"]] = "queue password"
    if creds.get("qdrant"):
        out[creds["qdrant"]] = "Qdrant API key"
    for name, token in creds["principals"].items():
        out[token] = f"bearer principal ({name})"
    return out


def scan(source: str, text: str, secrets: dict[str, str], findings: list[str]) -> None:
    for value, cls in secrets.items():
        if value and value in text:
            findings.append(f"{source}: contains a {cls}")
    for cls, pattern in SHAPES.items():
        if pattern.search(text):
            findings.append(f"{source}: contains a {cls} shape")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    creds = json.loads((STATE / "credentials.json").read_text())
    secrets = secrets_of(creds)
    findings: list[str] = []
    counts: dict[str, int] = {}

    pods = json.loads(k("get", "pods", "-A", "-o", "json"))["items"]
    for p in pods:
        ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
        if not ns.startswith("anvilkit-") or ns == "anvilkit-observability":
            continue
        for c in p["spec"].get("containers", []) + p["spec"].get("initContainers", []):
            proc = subprocess.run([KUBECTL, "--kubeconfig", str(KUBECONFIG), "-n", ns, "logs", name, "-c", c["name"], "--tail=-1"],
                                  capture_output=True, text=True)
            if proc.returncode == 0:
                counts["rawLogBytes"] = counts.get("rawLogBytes", 0) + len(proc.stdout)
                scan(f"log {ns}/{name}/{c['name']}", proc.stdout, secrets, findings)

    exported: dict[str, list[dict]] = {"logs": [], "metrics": [], "traces": []}
    nodes = subprocess.run(["docker", "ps", "--filter", f"label=io.x-k8s.kind.cluster={CLUSTER}", "--format", "{{.Names}}"],
                           capture_output=True, text=True).stdout.split()
    for node in nodes:
        for signal in exported:
            proc = subprocess.run(["docker", "exec", node, "cat", f"/var/lib/anvilkit-telemetry/{signal}.jsonl"], capture_output=True, text=True)
            if proc.returncode != 0:
                continue
            scan(f"collector {signal} on {node}", proc.stdout, secrets, findings)
            for line in proc.stdout.splitlines():
                if line.strip():
                    exported[signal].append(json.loads(line))

    # The Collector's redaction processor (summary: debug) reports on each span the
    # keys it removed (redaction.redacted.keys) and those whose value it masked
    # (redaction.masked.keys): removed keys are what the services emit beyond the
    # allowlist (recorded); a masked value means a service put a secret shape into
    # an allowed attribute (a finding even though the export is clean).
    span_count, span_services, foreign_keys = 0, set(), set()
    removed_keys: dict[str, int] = {}
    masked: list[str] = []
    for batch in exported["traces"]:
        for rs in batch.get("resourceSpans", []):
            svc = next((a["value"].get("stringValue") for a in rs.get("resource", {}).get("attributes", []) if a["key"] == "service.name"), "?")
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    span_count += 1
                    span_services.add(svc)
                    attrs = {attr["key"]: attr["value"].get("stringValue", "") for attr in span.get("attributes", [])}
                    for key in filter(None, attrs.get("redaction.redacted.keys", "").split(",")):
                        removed_keys[key] = removed_keys.get(key, 0) + 1
                    if attrs.get("redaction.masked.keys"):
                        masked.append(f"{svc}: {attrs['redaction.masked.keys']}")
                    foreign_keys |= {key for key in attrs if not key.startswith("redaction.")} - ALLOWED_SPAN_KEYS
    metric_services = set()
    for batch in exported["metrics"]:
        for rm in batch.get("resourceMetrics", []):
            for attr in rm.get("resource", {}).get("attributes", []):
                if attr["key"] in ("service.name", "service_name") or attr["key"].endswith("job"):
                    metric_services.add(attr["value"].get("stringValue", ""))
    log_records = sum(len(sl.get("logRecords", [])) for b in exported["logs"] for rl in b.get("resourceLogs", []) for sl in rl.get("scopeLogs", []))
    counts.update({"spans": span_count, "logRecords": log_records, "metricBatches": len(exported["metrics"])})
    if foreign_keys:
        findings.append(f"spans carry attributes outside the allowlist: {sorted(foreign_keys)}")
    for m in sorted(set(masked)):
        findings.append(f"a span attribute held a secret shape (masked by the Collector): {m}")
    for signal, n in (("spans", span_count), ("log records", log_records), ("metric batches", len(exported["metrics"]))):
        if n == 0:
            findings.append(f"no {signal} reached the Collector: an empty pipeline is not a redacted one")

    out = ROOT / "outputs/qualification" / a.run / "telemetry"
    out.mkdir(parents=True, exist_ok=True)
    record = {"run": a.run, "secretsSearched": len(secrets), "shapesSearched": sorted(SHAPES), "counts": counts,
              "spanServices": sorted(span_services), "metricSources": sorted(metric_services),
              "attributesRemovedByCollector": dict(sorted(removed_keys.items())), "findings": findings,
              "placement": "DEVELOPMENT_ONLY: the Collector exports to node files; no Loki, Tempo or Prometheus backend"}
    (out / "telemetry.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({k: v for k, v in record.items() if k != "findings"}, indent=1))
    for f in findings:
        print(f"FINDING {f}")
    print(f"{len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
