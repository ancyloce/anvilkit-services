#!/usr/bin/env python3
"""Alert-firing drill of the qualification environment (P3.6 AC2).

  .local/verification-venv/bin/python deploy/qualification/alert-drill.py --run RUN [--wait 2700]

Injects every AnvilKit alert's condition and proves that each alert fires and
reaches the receiver its severity routes to. The injection is a drill Pod in
anvilkit-observability (busybox httpd, the digest the Job profiles pin) that
serves the conditions as Prometheus metrics, scraped through a PodMonitor with
the metrics stack's release label: the rules aggregate over every series of a
name, so the injected series raise the alerts exactly as a real condition
would, without disturbing a service. For each alert the drill waits until
Alertmanager holds it (GET /api/v2/alerts) and records its receivers; the
drill Pod, its ConfigMap and PodMonitor are deleted afterwards (also on
failure). Writes outputs/qualification/<RUN>/alerts/alert-drill.json. PASS
only when every alert of the rules fired and reached the receiver of its
severity; an alert instance already active before the injection proves
nothing and does not count (a real condition can hold an alert the drill
then cannot raise anew). It creates and deletes resources in anvilkit-observability: run it
only with that environment operator's authorization. Exit 0 PASS, 1 FAIL, 2
when the metrics stack is not deployed.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[2]
KUBECONFIG = ROOT / ".local/qualification/kubeconfig"
KUBECTL = str(ROOT / ".local/bin/kubectl")
NS = "anvilkit-observability"
BUSYBOX = "docker.io/library/busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0"
NOW = int(time.time())
# Each alert's injected condition (exposition lines) and the severity its rule carries.
CONDITIONS = {
    "AnvilKitAPIErrorRatioHigh": ("critical", ['anvilkit_api_requests_total{code="503",method="GET",route="/drill"} {n}']),
    "AnvilKitAPILatencyP95High": ("warning", ['anvilkit_api_request_duration_seconds_bucket{le="1",method="GET",route="/drill"} 0',
                                              'anvilkit_api_request_duration_seconds_bucket{le="5",method="GET",route="/drill"} {n}',
                                              'anvilkit_api_request_duration_seconds_bucket{le="+Inf",method="GET",route="/drill"} {n}']),
    "AnvilKitControlCapacityExhausted": ("warning", ['anvilkit_control_capacity_refusals_total{pool="drill"} {n}']),
    "AnvilKitControlPoolSaturated": ("warning", ['anvilkit_control_grpc_pool_in_use{pool="drill"} 10', 'anvilkit_control_grpc_pool_limit{pool="drill"} 10']),
    "AnvilKitRelayBacklog": ("warning", ['anvilkit_control_relay_backlog{lane="drill"} 1000']),
    "AnvilKitUnresolvedObligationAgeHigh": ("critical", ['anvilkit_control_unresolved_obligation_oldest_seconds{class="drill",state="unknown"} 999999']),
    "AnvilKitModelProxyUnknownOutcomes": ("warning", ['anvilkit_model_proxy_call_outcomes_total{outcome="unknown",error_code="DRILL"} {n}']),
    "AnvilKitModelProxySenderLost": ("warning", ['anvilkit_model_proxy_call_outcomes_total{outcome="unknown",error_code="SENDER_LOST"} {n}']),
    "AnvilKitModelProxyPendingObservations": ("warning", ["anvilkit_model_proxy_pending_observations 1"]),
    "AnvilKitMCPUnknownCalls": ("critical", ["anvilkit_mcp_tool_call_unknown_oldest_seconds 999999"]),
    "AnvilKitMCPRecoveryRestricted": ("warning", ["anvilkit_mcp_recovery_restricted_servers 1"]),
    "AnvilKitOutboxOldestAgeHigh": ("warning", ["anvilkit_knowledge_outbox_oldest_unforwarded_seconds 999999"]),
    "AnvilKitQueueOldestAgeHigh": ("warning", ['anvilkit_background_queue_oldest_waiting_seconds{queue="drill"} 999999']),
    "AnvilKitDurableRequestDivergence": ("warning", ["anvilkit_mcp_background_overdue_retries 1"]),
    "AnvilKitKnowledgeIndexFailures": ("warning", ['anvilkit_knowledge_index_batches_total{outcome="failed"} {n}']),
    "AnvilKitKnowledgeProjectionLag": ("warning", ["anvilkit_knowledge_memory_projection_lag_seconds 999999"]),
    "AnvilKitConfigGenerationRejected": ("warning", ["anvilkit_mcp_config_rejections_total {n}"]),
    "AnvilKitForcedStop": ("warning", ["anvilkit_mcp_shutdown_forced 1"]),
    "AnvilKitTargetDown": ("critical", []),  # raised by the drill's own unscrapeable target below
    "AnvilKitBackupStale": ("critical", ['kube_cronjob_status_last_schedule_time{cronjob="anvilkit-backup-drill",namespace="anvilkit-observability"} 1']),
}


def k(*args: str, stdin: str | None = None, check: bool = True) -> str:
    p = subprocess.run([KUBECTL, "--kubeconfig", str(KUBECONFIG), *args], input=stdin, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {p.stderr.strip()[-400:]}")
    return p.stdout


def proxy(service: str, port: int, path: str) -> object:
    return json.loads(k("get", "--raw", f"/api/v1/namespaces/{NS}/services/http:{service}:{port}/proxy{path}"))


def manifests(counter: int) -> str:
    lines = [line.replace("{n}", str(counter)) for _, (_, ls) in CONDITIONS.items() for line in ls]
    body = "\n".join(lines) + "\n"
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "anvilkit-alert-drill", "namespace": NS}, "data": {"metrics": body}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "anvilkit-alert-drill", "namespace": NS, "labels": {"app.kubernetes.io/name": "anvilkit-alert-drill"}},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": "anvilkit-alert-drill"}},
                  "template": {"metadata": {"labels": {"app.kubernetes.io/name": "anvilkit-alert-drill"}},
                               "spec": {"automountServiceAccountToken": False, "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "seccompProfile": {"type": "RuntimeDefault"}},
                                        "containers": [{"name": "metrics", "image": BUSYBOX, "command": ["httpd", "-f", "-p", "8080", "-h", "/www"],
                                                        "ports": [{"name": "metrics", "containerPort": 8080}, {"name": "dead", "containerPort": 8081}],
                                                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                                                        "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "32Mi"}},
                                                        "volumeMounts": [{"name": "www", "mountPath": "/www", "readOnly": True}]}],
                                        "volumes": [{"name": "www", "configMap": {"name": "anvilkit-alert-drill", "items": [{"key": "metrics", "path": "metrics"}]}}]}}}},
        {"apiVersion": "monitoring.coreos.com/v1", "kind": "PodMonitor", "metadata": {"name": "anvilkit-alert-drill", "namespace": NS, "labels": {"release": "anvilkit-metrics"}},
         "spec": {"selector": {"matchLabels": {"app.kubernetes.io/name": "anvilkit-alert-drill"}},
                  # busybox httpd sends no Content-Type, which Prometheus 3 refuses
                  # without a fallback protocol (every scrape failed on the first run).
                  "fallbackScrapeProtocol": "PrometheusText0.0.4",
                  # The second endpoint has no listener: its up == 0 raises AnvilKitTargetDown
                  # (the rule's namespace label is rewritten to the applications' namespace).
                  "podMetricsEndpoints": [{"port": "metrics", "path": "/metrics", "interval": "15s"},
                                          {"port": "dead", "path": "/metrics", "interval": "15s",
                                           "relabelings": [{"targetLabel": "namespace", "replacement": "anvilkit-apps", "action": "replace"}]}]}},
    ]})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--wait", type=int, default=2700,
                    help="seconds to wait for every alert (the longest `for` is 30m, plus the Pod's start, the first scrape and evaluation)")
    a = ap.parse_args()
    try:
        proxy("anvilkit-metrics-alertmanager", 9093, "/api/v2/status")
    except (RuntimeError, ValueError):
        print("UNEXECUTED: the metrics stack (anvilkit-metrics Alertmanager) is not deployed")
        return 2
    out = ROOT / "outputs/qualification" / a.run / "alerts"
    out.mkdir(parents=True, exist_ok=True)
    # The operator names an AlertmanagerConfig's receivers <namespace>/<config>/<receiver>.
    route = {s: f"{NS}/anvilkit/{r}" for s, r in {"critical": "anvilkit-development-critical", "warning": "anvilkit-development"}.items()}
    up = 'up{job="%s/anvilkit-alert-drill",endpoint="metrics"} == 1' % NS
    record: dict = {"run": a.run, "startedAt": NOW, "alerts": {}}
    started = time.time()
    counter = 1
    try:
        # Instances active before the injection prove nothing: only new ones count.
        before = proxy("anvilkit-metrics-alertmanager", 9093, "/api/v2/alerts?active=true")
        known = {x["fingerprint"] for x in before}
        record["activeBefore"] = sorted({x["labels"].get("alertname") for x in before} & set(CONDITIONS))
        k("apply", "-f", "-", stdin=manifests(counter))
        pending = set(CONDITIONS)
        while pending and time.time() - started < a.wait:
            # Counters advance so every increase() window sees the injection.
            counter += 100
            k("apply", "-f", "-", stdin=manifests(counter))
            if "injectionScrapedAfterSeconds" not in record:
                if proxy("anvilkit-metrics-prometheus", 9090, "/api/v1/query?query=" + urllib.parse.quote(up, safe=""))["data"]["result"]:
                    record["injectionScrapedAfterSeconds"] = round(time.time() - started)
                elif time.time() - started > 300:
                    record["injectionScraped"] = False  # every pending alert stays notFired
                    break
            firing = proxy("anvilkit-metrics-alertmanager", 9093, "/api/v2/alerts?active=true")
            for alert in firing:
                name = alert["labels"].get("alertname")
                if name in pending and alert["fingerprint"] not in known:
                    receivers = sorted(r["name"] for r in alert.get("receivers", []))
                    severity = alert["labels"].get("severity")
                    record["alerts"][name] = {"severity": severity, "receivers": receivers, "firedAfterSeconds": round(time.time() - started),
                                              "routedAsDesigned": severity == CONDITIONS[name][0] and route.get(severity) in receivers}
                    pending.discard(name)
            time.sleep(30)
        record["notFired"] = sorted(pending)
    finally:
        k("delete", "-n", NS, "configmap/anvilkit-alert-drill", "deployment/anvilkit-alert-drill", "podmonitor/anvilkit-alert-drill", "--ignore-not-found", check=False)
    record["status"] = "PASS" if not record["notFired"] and all(v["routedAsDesigned"] for v in record["alerts"].values()) else "FAIL"
    (out / "alert-drill.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"fired": len(record["alerts"]), "notFired": record["notFired"], "status": record["status"]}, indent=1))
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
