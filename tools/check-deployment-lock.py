#!/usr/bin/env python3
"""Deployment lock check (P23-01): deploy/gitops/lock.yaml against the eight charts.

  .local/verification-venv/bin/python tools/check-deployment-lock.py [--self-test]

For the qualification environment every application chart is rendered with its
environment values (deploy/qualification/values/<name>.yaml) and the rendered objects
must carry the lock's limits: at least the minimum replicas (or an HPA owning the
replica count with the lock's minimum and exact maximum), a PodDisruptionBudget with
the lock's minAvailable that selects only the runtime Pods, the hostname and zone
spread constraints, requests and limits on every container, startup/readiness/liveness
probes on the service container, a termination grace above every shutdown bound the
rendered configuration declares, and the exact per-replica pools (configuration keys
from the rendered ConfigMap, fixed pools from their source lines). Each connection
budget (pools x open generations x autoscaling maximum + reserves) must fit the
cluster's max_connections. The lock must name all eight services and every DD-10 §3
platform row with a production status of REQUIRED and its ENV inputs.

Production fields that are REQUIRED are listed as NOT_VERIFIED; they never pass.
--self-test applies known defects to copies of the rendered objects and the lock
and requires each to be reported. Exit 0 when the qualification environment satisfies
the lock, 1 on any finding, 2 when helm is unavailable (UNEXECUTED).
"""
from __future__ import annotations

import argparse
import copy
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pyenv_check  # noqa: E402

pyenv_check.require("yaml")
import yaml  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCK = ROOT / "deploy/gitops/lock.yaml"
SERVICES = [f"anvilkit-agent-{s}" for s in ("api", "control", "workflow", "model-proxy", "knowledge", "mcp", "background-worker", "inference")]
PLATFORM = {"kubernetes-control-plane", "business-postgresql", "temporal-postgresql", "temporal-server", "qdrant", "queue-valkey",
            "cache-valkey", "nats-jetstream", "apollo-mysql", "contextforge", "openbao", "ceph", "network-policies", "secret-delivery"}
# Platform charts every environment combination carries: the default deny
# NetworkPolicies of the communication matrix (P0.4) and the Secrets the
# third-party charts read, synced from OpenBao by the CSI driver (P0.6).
REQUIRED_CHARTS = {"deploy/qualification/platform/network": "anvilkit-network",
                   "deploy/qualification/platform/secrets": "anvilkit-secrets"}
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


def helm() -> str | None:
    local = ROOT / ".local/bin/helm"
    return str(local) if local.exists() else shutil.which("helm")


def render(helm_bin: str, app: dict, values: pathlib.Path) -> list[dict]:
    p = subprocess.run([helm_bin, "template", app["name"], str(ROOT / app["chart"]), "-n", "anvilkit-apps", "-f", str(values),
                        "--set", f"image.digest={PLACEHOLDER_DIGEST}"], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "helm template failed")
    return [d for d in yaml.safe_load_all(p.stdout) if d]


def seconds(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", str(value))
    if not m:
        raise ValueError(f"not a duration: {value!r}")
    return float(m[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[m[2]]


def lookup(cfg: dict, dotted: str):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def one(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def subset(sel: dict, labels: dict) -> bool:
    return bool(sel) and all(labels.get(k) == v for k, v in sel.items())


def check_app(app: dict, lock: dict, docs: list[dict], findings: list[str]) -> dict:
    """Returns the effective autoscaling maximum and the verified pools."""
    name = app["name"]
    f = lambda msg: findings.append(f"{name}: {msg}")  # noqa: E731
    deps = [d for d in one(docs, "Deployment") if d["metadata"]["name"] == name]
    if len(deps) != 1:
        f(f"expected one Deployment named {name}, rendered {len(deps)}")
        return {"max": 0}
    dep = deps[0]
    pod = dep["spec"]["template"]
    labels = pod["metadata"].get("labels", {})
    spec = pod["spec"]
    minimum = app["replicas"]["min"]
    want_max = app["autoscaling"]["qualification"]["max"]

    hpas = one(docs, "HorizontalPodAutoscaler")
    if hpas:
        h = hpas[0]["spec"]
        if h["scaleTargetRef"].get("name") != name:
            f("the HPA does not target the Deployment")
        if h["minReplicas"] < minimum:
            f(f"HPA minReplicas {h['minReplicas']} below the lock's minimum {minimum}")
        if h["maxReplicas"] != want_max:
            f(f"HPA maxReplicas {h['maxReplicas']} is not the lock's {want_max}")
        if "replicas" in dep["spec"]:
            f("the Deployment sets replicas while an HPA owns the replica count")
        effective_max = h["maxReplicas"]
    else:
        replicas = dep["spec"].get("replicas", 1)
        if replicas < minimum:
            f(f"replicas {replicas} below the lock's minimum {minimum}")
        if want_max != replicas:
            f(f"no HPA, so the maximum is the fixed replica count {replicas}, not the lock's {want_max}")
        effective_max = replicas

    want_pdb = lock["placement"]["disruption"]["minAvailable"]
    pdbs = one(docs, "PodDisruptionBudget")
    if len(pdbs) != 1:
        f(f"expected one PodDisruptionBudget, rendered {len(pdbs)}")
    else:
        p = pdbs[0]["spec"]
        if p.get("minAvailable") != want_pdb:
            f(f"PDB minAvailable {p.get('minAvailable')} is not {want_pdb}")
        sel = p.get("selector", {}).get("matchLabels", {})
        if not subset(sel, labels):
            f("the PDB selector does not select the runtime Pods")
        for job in one(docs, "Job"):
            if subset(sel, job["spec"]["template"]["metadata"].get("labels", {})):
                f(f"the PDB selector also selects the Job {job['metadata']['name']}")

    constraints = spec.get("topologySpreadConstraints") or []
    for want in lock["placement"]["spread"]:
        match = [c for c in constraints if c.get("topologyKey") == want["topologyKey"]]
        if not match:
            f(f"no topology spread over {want['topologyKey']}")
            continue
        c = match[0]
        if c.get("maxSkew") != want["maxSkew"] or c.get("whenUnsatisfiable") != want["whenUnsatisfiable"]:
            f(f"spread over {want['topologyKey']} is maxSkew {c.get('maxSkew')}/{c.get('whenUnsatisfiable')}, not {want['maxSkew']}/{want['whenUnsatisfiable']}")
        if not subset(c.get("labelSelector", {}).get("matchLabels", {}), labels):
            f(f"spread over {want['topologyKey']} does not select the Deployment's Pods")
        if app.get("component") and c.get("labelSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component") != app["component"]:
            f(f"spread over {want['topologyKey']} also counts Pods outside component {app['component']}")

    containers = spec.get("containers", [])
    for i, c in enumerate(containers):
        res = c.get("resources") or {}
        for side in ("requests", "limits"):
            for r in ("cpu", "memory"):
                if not (res.get(side) or {}).get(r):
                    f(f"container {c['name']} has no {side}.{r}")
        probes = ("startupProbe", "readinessProbe", "livenessProbe") if i == 0 else ("readinessProbe", "livenessProbe")
        for probe in probes:
            if probe not in c:
                f(f"container {c['name']} has no {probe}")

    cms = [d for d in one(docs, "ConfigMap") if d["metadata"]["name"] == name]
    cfg = yaml.safe_load(next(iter(cms[0]["data"].values()))) if cms and cms[0].get("data") else {}
    grace = spec.get("terminationGracePeriodSeconds")
    sd = app["shutdown"]
    for key in [sd["config"], *sd.get("also", [])]:
        value = lookup(cfg, key)
        if value is None:
            f(f"the rendered configuration has no {key}")
            continue
        bound = float(value) if sd.get("unit") == "seconds" and key == sd["config"] else seconds(value)
        if grace is None or grace <= bound:
            f(f"terminationGracePeriodSeconds {grace} does not exceed {key} ({bound:g}s)")

    names = {c["name"] for c in containers}
    for pool in app["pools"]:
        if pool.get("container") and pool["container"] not in names:
            f(f"pool of container {pool['container']} counted, but the Pod has no such container")
        if "config" in pool:
            got = lookup(cfg, pool["config"])
            if got != pool["perReplica"]:
                f(f"pool {pool['config']} is {got!r} in the rendered configuration, the lock budgets {pool['perReplica']}")
        else:
            src = ROOT / pool["fixed"]["source"]
            m = re.search(pool["fixed"]["pattern"], src.read_text()) if src.exists() else None
            if not m or int(m[1]) != pool["perReplica"]:
                f(f"fixed pool in {pool['fixed']['source']} is {m[1] if m else 'not found'}, the lock budgets {pool['perReplica']}")
    return {"max": effective_max}


def check_budgets(lock: dict, maxima: dict, findings: list[str]) -> list[str]:
    report = []
    for b in lock["connectionBudgets"]:
        limit = b["maxConnections"]["qualification"]
        reserve = sum(b["reserve"].values())
        used = 0
        for app in lock["applications"]:
            for pool in app["pools"]:
                if pool["cluster"] == b["cluster"]:
                    used += pool["perReplica"] * pool["generations"] * maxima.get(app["name"], 0)
        total = used + reserve
        report.append(f"  {b['cluster']:<9} pools {used:>4} + reserve {reserve:>3} = {total:>4} of max_connections {limit}")
        if total > limit:
            findings.append(f"connection budget {b['cluster']}: {total} exceeds max_connections {limit}")
    return report


def check_structure(lock: dict, findings: list[str]) -> list[str]:
    names = [a["name"] for a in lock["applications"]]
    if sorted(names) != sorted(SERVICES):
        findings.append(f"the lock's applications {names} are not exactly the eight services")
    platform = {p["name"] for p in lock["platform"]}
    for missing in sorted(PLATFORM - platform):
        findings.append(f"platform row {missing} (DD-10 §3) is missing")
    not_verified = []
    for p in lock["platform"]:
        prod, qual = p.get("production", {}), p.get("qualification", {})
        if prod.get("status") != "REQUIRED" or not prod.get("inputs"):
            findings.append(f"platform {p['name']}: production must be REQUIRED with its ENV inputs until they exist")
        if qual.get("status") not in ("DEVELOPMENT_ONLY", "NOT_RUN") or (qual.get("status") == "NOT_RUN" and not qual.get("reason")):
            findings.append(f"platform {p['name']}: qualification is DEVELOPMENT_ONLY or NOT_RUN with a reason")
        not_verified.append(f"  {p['name']:<25} production REQUIRED ({', '.join(prod.get('inputs', []))})")
    for a in lock["applications"]:
        prod = a["autoscaling"]["production"]
        if prod.get("max") != "REQUIRED" or not prod.get("inputs"):
            findings.append(f"{a['name']}: the production autoscaling maximum must stay REQUIRED with its inputs")
        not_verified.append(f"  {a['name']:<25} production maximum, resources and placement REQUIRED ({', '.join(prod.get('inputs', []))})")
    for b in lock["connectionBudgets"]:
        not_verified.append(f"  budget {b['cluster']:<18} production max_connections REQUIRED ({', '.join(b['maxConnections'].get('inputs', []))})")
    return not_verified


def check_combination(combination: dict, findings: list[str]) -> None:
    sources = {(a.get("chart") or {}).get("source"): a for a in combination.get("applications", [])}
    for source, name in sorted(REQUIRED_CHARTS.items()):
        if source not in sources:
            findings.append(f"the combination lacks the {name} chart ({source})")


def evaluate(lock: dict, rendered: dict[str, list[dict]], combination: dict | None = None) -> tuple[list[str], list[str], list[str]]:
    findings: list[str] = []
    not_verified = check_structure(lock, findings)
    if combination is not None:
        check_combination(combination, findings)
    maxima = {}
    for app in lock["applications"]:
        if app["name"] in rendered:
            maxima[app["name"]] = check_app(app, lock, rendered[app["name"]], findings)["max"]
    budget = check_budgets(lock, maxima, findings)
    return findings, budget, not_verified


def self_test(lock: dict, rendered: dict[str, list[dict]], combination: dict) -> list[str]:
    def dep(docs, name):
        return next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == name)

    def drop_pdb(r, lk):
        r["anvilkit-agent-api"] = [d for d in r["anvilkit-agent-api"] if d["kind"] != "PodDisruptionBudget"]

    def drop_zone_spread(r, lk):
        spec = dep(r["anvilkit-agent-mcp"], "anvilkit-agent-mcp")["spec"]["template"]["spec"]
        spec["topologySpreadConstraints"] = [c for c in spec["topologySpreadConstraints"] if c["topologyKey"] != "topology.kubernetes.io/zone"]

    def short_grace(r, lk):
        dep(r["anvilkit-agent-knowledge"], "anvilkit-agent-knowledge")["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = 25

    def sidecar_without_limits(r, lk):
        dep(r["anvilkit-agent-knowledge"], "anvilkit-agent-knowledge")["spec"]["template"]["spec"]["containers"][1]["resources"].pop("limits")

    def over_budget(r, lk):
        next(a for a in lk["applications"] if a["name"] == "anvilkit-agent-knowledge")["autoscaling"]["qualification"]["max"] = 12
        for d in r["anvilkit-agent-knowledge"]:
            if d["kind"] == "HorizontalPodAutoscaler":
                d["spec"]["maxReplicas"] = 12

    def pool_drift(r, lk):
        next(a for a in lk["applications"] if a["name"] == "anvilkit-agent-control")["pools"][0]["perReplica"] = 4

    def missing_platform(r, lk):
        lk["platform"] = [p for p in lk["platform"] if p["name"] != "openbao"]

    def guessed_production(r, lk):
        lk["applications"][0]["autoscaling"]["production"] = {"max": 6}

    def pdb_selects_migration(r, lk):
        for d in r["anvilkit-agent-control"]:
            if d["kind"] == "PodDisruptionBudget":
                d["spec"]["selector"]["matchLabels"].pop("app.kubernetes.io/component")

    def no_network_policies(r, lk, cb):
        cb["applications"] = [a for a in cb["applications"] if (a.get("chart") or {}).get("source") != "deploy/qualification/platform/network"]

    def no_secret_delivery(r, lk, cb):
        cb["applications"] = [a for a in cb["applications"] if (a.get("chart") or {}).get("source") != "deploy/qualification/platform/secrets"]

    probes = {"PodDisruptionBudget removed": drop_pdb, "zone spread removed": drop_zone_spread,
              "grace below the drain limit": short_grace, "sidecar without limits": sidecar_without_limits,
              "connection budget exceeded": over_budget, "pool size drift": pool_drift,
              "platform row missing": missing_platform, "production maximum guessed": guessed_production,
              "PDB selects the migration Job": pdb_selects_migration, "network policies absent": no_network_policies,
              "secret delivery absent": no_secret_delivery}
    failures = []
    for label, mutate in probes.items():
        r, lk, cb = copy.deepcopy(rendered), copy.deepcopy(lock), copy.deepcopy(combination)
        if mutate in (no_network_policies, no_secret_delivery):  # combination probes
            mutate(r, lk, cb)
        else:
            mutate(r, lk)
        findings, _, _ = evaluate(lk, r, cb)
        print(f"  probe {label:<32} {'detected: ' + findings[0] if findings else 'NOT DETECTED'}")
        if not findings:
            failures.append(label)
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    helm_bin = helm()
    if not helm_bin:
        print("UNEXECUTED: helm not found (.local/bin/helm or PATH)")
        return 2
    lock = yaml.safe_load(LOCK.read_text())
    values_dir = ROOT / lock["environments"]["qualification"]["values"]
    rendered, findings = {}, []
    for app in lock["applications"]:
        values = values_dir / f"{app['name']}.yaml"
        if not values.exists():
            findings.append(f"{app['name']}: no qualification values {values.relative_to(ROOT)}")
            continue
        try:
            rendered[app["name"]] = render(helm_bin, app, values)
        except RuntimeError as e:
            findings.append(f"{app['name']}: render failed: {e}")
    combination = yaml.safe_load((ROOT / lock["environments"]["qualification"]["combination"]).read_text())
    more, budget, not_verified = evaluate(lock, rendered, combination)
    findings += more
    print(f"deployment lock revision {lock['lockRevision']}: {len(lock['applications'])} applications, {len(lock['platform'])} platform rows")
    print("connection budgets (qualification):")
    print("\n".join(budget))
    print("NOT_VERIFIED (production inputs):")
    print("\n".join(not_verified))
    if a.self_test:
        print("self-test:")
        if self_test(lock, rendered, combination):
            findings.append("self-test: a known defect was not detected")
    if findings:
        print("FINDINGS:")
        print("\n".join(f"  {x}" for x in findings))
        return 1
    print("qualification environment satisfies the lock (DEVELOPMENT_ONLY placement; production NOT_VERIFIED)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
