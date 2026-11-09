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
cluster's max_connections. The lock must name all eight services (SERVICES) and every
DD-10 §3 platform row (PLATFORM) with a production status of REQUIRED and its ENV inputs.
Every combination must carry every lock application as a release chart, unless the
lock's environment entry records an exemption naming it with its reason
(environments.<environment>.exemptions: [{application, reason}]; RB-A12).

Every application and platform row carries the capacity worksheet's row (RB-A11;
platform.md "Capacity worksheet"): an owner from the lock's closed `owners` list; for an
application each rendered container's requests and limits and every emptyDir's size
limit, equal to the render; for a platform row its storage and resources by values path,
equal to the combination's inline values (or the in-repo chart's values.yaml), or
REQUIRED with its ENV inputs where the qualification placement sets none; and a peak
whose measured value and headroom are REQUIRED with their inputs or concrete with a
source. The theoretical capacity (replicas x requests and limits, declared PVC sizes x
counts) is derived and printed, never stored.

DEVELOPMENT_ONLY stand-ins (RB-A01; lock `standIns`: MinIO, the Distribution registry,
single-binary Loki, Tempo's local backend, kind's local-path storage and kindnet) never
enter a production combination or the production skeleton. Each catalog entry must match
the qualification combination (a stale matcher fails) and be named by the platform row it
replaces (`qualificationStandIns`). The production skeleton (lock
environments.production.skeleton) is the production layer's reviewed structure with its
values still ENV inputs: it must be marked `skeleton: true` and `development: false`,
contain no stand-in, carry the lock's production pins, provide every lock platform row
(or name the pending decision), carry all eight services, and every REQUIRED marker must
name its ENV inputs or its decision.

Production fields that are REQUIRED are listed as NOT_VERIFIED; they never pass.
Once ENV-01..ENV-10 supply them (P3.8), a production row is SUPPLIED with its source
(the ENV record) and concrete values: every platform row names the application of the
production combination that provides it, every service's production autoscaling has a
minimum of at least two and a maximum, every connection budget a production
max_connections. Then the production combination (lock environments.production) is
checked like the qualification one, with the production values in place of the
qualification ones: the charts rendered with the production values must meet the
replica floor, and every connection budget must fit. Without a production combination
the production check is NOT_RUN (named, never a pass).
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
            "cache-valkey", "nats-jetstream", "apollo-mysql", "contextforge", "openbao", "ceph", "network-policies", "secret-delivery",
            # RB-A01: the production layer's components qualification does not run.
            "gateway", "load-balancer", "cluster-backup"}
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


def quantity(value, memory: bool) -> float:
    """A Kubernetes quantity in cores or bytes (the forms the charts use)."""
    text = str(value)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(m|Ki|Mi|Gi|Ti|k|M|G|T)?", text)
    if not m:
        raise ValueError(f"not a quantity: {value!r}")
    n, unit = float(m[1]), m[2] or ""
    if not memory:
        if unit not in ("", "m"):
            raise ValueError(f"not a CPU quantity: {value!r}")
        return n / 1000 if unit == "m" else n
    return n * {"": 1, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}[unit]


def same_resources(want: dict, got: dict) -> bool:
    """Requests and limits equal as quantities, with no key on one side only."""
    try:
        for side in ("requests", "limits"):
            w, g = (want or {}).get(side) or {}, (got or {}).get(side) or {}
            if set(w) != set(g) or any(quantity(w[k], k == "memory") != quantity(g[k], k == "memory") for k in w):
                return False
        return True
    except ValueError:
        return False


def check_peak(where: str, peak, findings: list[str]) -> None:
    """A measured peak and its headroom are REQUIRED with their inputs, or
    concrete with the source they were measured in; never a guess."""
    if not isinstance(peak, dict):
        findings.append(f"{where}: capacity has no peak (measured, headroom)")
        return
    for key in ("measured", "headroom"):
        v = peak.get(key)
        if v == "REQUIRED":
            if not peak.get("inputs"):
                findings.append(f"{where}: peak {key} is REQUIRED without its ENV inputs")
        elif not (isinstance(v, (int, float, str)) and v != "" and peak.get("source")):
            findings.append(f"{where}: peak {key} {v!r} is neither REQUIRED with inputs nor a value with its source")


def check_capacity_app(app: dict, lock: dict, spec: dict, findings: list[str]) -> None:
    """RB-A11: the application's capacity row equals the rendered Pod."""
    name = app["name"]
    cap = app.get("capacity")
    if not isinstance(cap, dict):
        findings.append(f"{name}: no capacity row (owner, containers, ephemeral, peak)")
        return
    if cap.get("owner") not in (lock.get("owners") or []):
        findings.append(f"{name}: capacity owner {cap.get('owner')!r} is not one of the lock's owners")
    declared = cap.get("containers") or {}
    rendered = {c["name"]: c.get("resources") or {} for c in spec.get("containers", [])}
    for c in sorted(set(rendered) - set(declared)):
        findings.append(f"{name}: container {c} is rendered but has no capacity entry")
    for c in sorted(set(declared) - set(rendered)):
        findings.append(f"{name}: capacity entry {c} names no rendered container")
    for c in sorted(set(declared) & set(rendered)):
        if not same_resources(declared[c], rendered[c]):
            findings.append(f"{name}: container {c} renders {rendered[c]}, the capacity row declares {declared[c]}")
    temp = {v["name"]: (v["emptyDir"] or {}).get("sizeLimit", "unbounded") for v in spec.get("volumes", []) if "emptyDir" in v}
    want = {k: str(v) for k, v in (cap.get("ephemeral") or {}).items()}
    if {k: str(v) for k, v in temp.items()} != want:
        findings.append(f"{name}: temporary volumes render {temp}, the capacity row declares {want}")
    check_peak(name, cap.get("peak"), findings)


def platform_values(combination: dict, application: str):
    """The values an application of the combination is rendered with: its inline
    values over the in-repo chart's values.yaml (an upstream chart's defaults are
    not known here)."""
    app = next((a for a in combination.get("applications", []) if a["name"] == application), None)
    if app is None:
        return None
    base = {}
    source = (app.get("chart") or {}).get("source")
    if source and (ROOT / source / "values.yaml").exists():
        base = yaml.safe_load((ROOT / source / "values.yaml").read_text()) or {}

    def merge(a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            return {**a, **{k: merge(a.get(k), v) for k, v in b.items()}}
        return b
    return merge(base, app.get("values") or {})


def check_capacity_platform(lock: dict, combination: dict, findings: list[str]) -> None:
    """RB-A11: every platform row's capacity: owner, peak, and for a qualifying
    row its storage and resources against the combination's values."""
    for p in lock["platform"]:
        where = f"platform {p['name']}"
        cap = p.get("capacity")
        if not isinstance(cap, dict):
            findings.append(f"{where}: no capacity row (owner, storage, resources, peak)")
            continue
        if cap.get("owner") not in (lock.get("owners") or []):
            findings.append(f"{where}: capacity owner {cap.get('owner')!r} is not one of the lock's owners")
        check_peak(where, cap.get("peak"), findings)
        if (p.get("qualification") or {}).get("status") == "NOT_RUN":
            continue
        for key in ("storage", "resources"):
            entries = cap.get(key)
            if isinstance(entries, dict):
                if entries.get("status") != "REQUIRED" or not entries.get("inputs"):
                    findings.append(f"{where}: capacity {key} is a list of entries or REQUIRED with its inputs")
                continue
            if not isinstance(entries, list):
                findings.append(f"{where}: capacity has no {key} (a list, possibly empty, or REQUIRED with inputs)")
                continue
            for e in entries:
                values = platform_values(combination, e.get("application", ""))
                if values is None:
                    findings.append(f"{where}: capacity {key} names {e.get('application')!r}, which is not in the combination")
                    continue
                got = lookup(values, e.get("values", ""))
                if got is None:
                    findings.append(f"{where}: {e['application']} has no value at {e.get('values')}")
                elif key == "storage":
                    try:
                        ok = quantity(got, True) == quantity(e.get("size"), True) and isinstance(e.get("count"), int) and e["count"] > 0
                    except ValueError:
                        ok = False
                    if not ok:
                        findings.append(f"{where}: {e['application']} {e['values']} is {got!r}, the capacity row declares {e.get('size')!r} x {e.get('count')!r}")
                elif not same_resources({"requests": e.get("requests"), "limits": e.get("limits")}, got):
                    findings.append(f"{where}: {e['application']} {e['values']} is {got}, the capacity row declares requests {e.get('requests')} limits {e.get('limits')}")


def capacity_report(lock: dict) -> list[str]:
    """The derived theoretical capacity (qualification): per application, the
    autoscaling maximum times the sum of its containers' requests and limits;
    the declared platform PVCs (size x count). Never stored in the lock."""
    out, cpu_r, mem_r, cpu_l, mem_l = [], 0.0, 0.0, 0.0, 0.0
    for a in lock["applications"]:
        cs = (a.get("capacity") or {}).get("containers") or {}
        mx = a["autoscaling"]["qualification"]["max"]
        try:
            rc = sum(quantity((c.get("requests") or {}).get("cpu", 0), False) for c in cs.values())
            rm = sum(quantity((c.get("requests") or {}).get("memory", 0), True) for c in cs.values())
            lc = sum(quantity((c.get("limits") or {}).get("cpu", 0), False) for c in cs.values())
            lm = sum(quantity((c.get("limits") or {}).get("memory", 0), True) for c in cs.values())
        except ValueError:
            continue
        cpu_r, mem_r, cpu_l, mem_l = cpu_r + rc * mx, mem_r + rm * mx, cpu_l + lc * mx, mem_l + lm * mx
        out.append(f"  {a['name']:<34} x{mx}: requests {rc * mx:5.2f} CPU {rm * mx / 2**30:6.2f} GiB, limits {lc * mx:5.2f} CPU {lm * mx / 2**30:6.2f} GiB")
    out.append(f"  {'applications at their maxima':<34}     requests {cpu_r:5.2f} CPU {mem_r / 2**30:6.2f} GiB, limits {cpu_l:5.2f} CPU {mem_l / 2**30:6.2f} GiB")
    pvc = 0.0
    for p in lock["platform"]:
        for e in ((p.get("capacity") or {}).get("storage") if isinstance((p.get("capacity") or {}).get("storage"), list) else []):
            try:
                pvc += quantity(e["size"], True) * e["count"]
            except (KeyError, TypeError, ValueError):
                pass
    out.append(f"  {'platform PVCs (declared counts)':<34}     {pvc / 2**30:.1f} GiB before storage replication; measured peaks and headroom REQUIRED (ENV-05)")
    return out


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
    check_capacity_app(app, lock, spec, findings)

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


def strings(obj):
    """Every string value of a combination (keys excluded)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


def standin_hits(entry: dict, combination: dict) -> list[str]:
    """Where a stand-in of the lock's catalog shows in a combination."""
    m, apps = entry.get("match") or {}, combination.get("applications") or []
    hits = []
    if "chartSource" in m:
        hits += [a["name"] for a in apps if (a.get("chart") or {}).get("source") == m["chartSource"]]
    if "chart" in m:
        for a in apps:
            if (a.get("chart") or {}).get("name") == m["chart"] and all(lookup(a.get("values") or {}, k) == v for k, v in (m.get("values") or {}).items()):
                hits.append(a["name"])
    if "text" in m:
        hits += [f"the string {s!r}" for s in strings(combination) if m["text"] in s]
    if "absent" in m and not any((a.get("chart") or {}).get("name") == m["absent"] for a in apps):
        hits.append(f"no {m['absent']} application")
    if "absentBootstrap" in m and not any(b.get("name") == m["absentBootstrap"] and (b.get("rke2") or {}).get("cni") == "none"
                                          for b in combination.get("bootstrap") or []):
        hits.append(f"no {m['absentBootstrap']} bootstrap on RKE2 cni none")
    return hits


def check_no_standins(lock: dict, combination: dict, findings: list[str], label: str) -> None:
    """RB-A01: no DEVELOPMENT_ONLY stand-in in a production combination or skeleton."""
    if combination.get("development") is not False:
        findings.append(f"{label}: development is {combination.get('development')!r}, not false")
    for entry in lock.get("standIns") or []:
        for hit in standin_hits(entry, combination):
            findings.append(f"{label}: stand-in {entry['name']} (replaces {entry.get('replaces')}): {hit}")


def check_standin_catalog(lock: dict, qualification: dict, findings: list[str]) -> None:
    """RB-A01: each stand-in matches the qualification combination (a matcher that
    finds nothing is stale) and is named by the platform row it replaces."""
    catalog = {e.get("name"): e for e in lock.get("standIns") or []}
    if not catalog:
        findings.append("the lock has no stand-in catalog (standIns; RB-A01)")
    rows = {p["name"]: p for p in lock["platform"]}
    named = {}
    for p in lock["platform"]:
        for n in p.get("qualificationStandIns") or []:
            named[n] = p["name"]
            if n not in catalog:
                findings.append(f"platform {p['name']}: qualificationStandIns names {n!r}, which the stand-in catalog lacks")
    for n, e in catalog.items():
        if e.get("replaces") not in rows:
            findings.append(f"stand-in {n}: replaces {e.get('replaces')!r}, which is no platform row")
        elif named.get(n) != e["replaces"]:
            findings.append(f"stand-in {n}: the row it replaces ({e['replaces']}) does not name it under qualificationStandIns")
        if not standin_hits(e, qualification):
            findings.append(f"stand-in {n}: its matcher finds nothing in the qualification combination (stale)")


def required_markers(obj, path: str = ""):
    """Every {status: REQUIRED, ...} marker of a skeleton, with its path."""
    if isinstance(obj, dict):
        if obj.get("status") == "REQUIRED":
            yield path, obj
            return
        for k, v in obj.items():
            yield from required_markers(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            name = v.get("name") if isinstance(v, dict) else None
            yield from required_markers(v, f"{path}[{name or i}]")


def check_production_skeleton(lock: dict, skeleton: dict, findings: list[str]) -> list[str]:
    """RB-A01: the production layer's skeleton, before its ENV values exist."""
    label = "production skeleton"
    if skeleton.get("skeleton") is not True or skeleton.get("environment") != "production":
        findings.append(f"{label}: not marked skeleton: true for environment production")
    check_no_standins(lock, skeleton, findings, label)
    entries = (skeleton.get("bootstrap") or []) + (skeleton.get("applications") or [])
    names = [e.get("name") for e in entries]
    for n in sorted({n for n in names if names.count(n) > 1}):
        findings.append(f"{label}: {n} is named twice")
    rows = {p["name"] for p in lock["platform"]}
    provided = {}
    for e in entries:
        for r in e.get("provides") or []:
            if r not in rows:
                findings.append(f"{label}: {e.get('name')} provides {r!r}, which is no lock platform row")
            provided.setdefault(r, []).append(e.get("name"))
    pending = {}
    for pnd in skeleton.get("pending") or []:
        if not re.fullmatch(r"RB-[A-Z]\d+", str(pnd.get("decision", ""))):
            findings.append(f"{label}: pending row {pnd.get('row')!r} names no backlog decision (RB-…)")
        pending[pnd.get("row")] = pnd.get("decision")
    for r in sorted(rows - set(provided) - set(pending)):
        findings.append(f"{label}: no component provides the lock platform row {r}, and no pending decision is named")
    for r in sorted(set(provided) & set(pending)):
        findings.append(f"{label}: {r} is both provided ({', '.join(provided[r])}) and pending {pending[r]}")
    released = {(e.get("chart") or {}).get("release") for e in entries}
    for a in lock["applications"]:
        if a["name"] not in released:
            findings.append(f"{label}: the service {a['name']} is absent (a production combination has no exemption)")
    versions = {}
    for e in entries:
        chart = e.get("chart") or {}
        if chart.get("upstream"):
            if not chart.get("name"):
                findings.append(f"{label}: {e.get('name')} names no chart")
            if not (isinstance(chart.get("version"), str) or (chart.get("version") or {}).get("status") == "REQUIRED"):
                findings.append(f"{label}: {e.get('name')} has no chart version (a pin or REQUIRED with its decision)")
            digest = chart.get("digest")
            if not ((isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)) or (digest or {}).get("status") == "REQUIRED"):
                findings.append(f"{label}: {e.get('name')} has no chart digest (sha256 or REQUIRED with its inputs)")
            v = chart.get("appVersion") or chart.get("version")
            if isinstance(v, str):
                versions[chart["name"]] = v
        if isinstance((e.get("image") or {}).get("version"), str):
            versions[e["name"]] = e["image"]["version"]
        if isinstance(e.get("gatewayApi"), str):
            versions["gateway-api"] = e["gatewayApi"]
        if (chart.get("release") is None and not chart and "image" not in e and e.get("name") not in ("rke2",)):
            findings.append(f"{label}: {e.get('name')} names no chart, image or release")
    for name, pin in ((lock["environments"].get("production") or {}).get("pins") or {}).items():
        if versions.get(name) != str(pin):
            findings.append(f"{label}: {name} is {versions.get(name)!r}, the lock pins {pin}")
    markers = list(required_markers(skeleton))
    for path, m in markers:
        if not (m.get("inputs") or str(m.get("decision") or "").strip()):
            findings.append(f"{label}: {path} is REQUIRED without its ENV inputs or decision")
    decisions = sorted({str(m.get("decision")) for _, m in markers if m.get("decision")})
    return [f"  {len(skeleton.get('bootstrap') or [])} bootstrap and {len(skeleton.get('applications') or [])} applications; "
            f"{len(markers)} values REQUIRED; {len(provided)} platform rows provided, {len(pending)} pending ({', '.join(f'{r}: {d}' for r, d in sorted(pending.items()))})",
            *[f"  version decision: {d}" for d in decisions]]


def check_structure(lock: dict, findings: list[str]) -> list[str]:
    owners = lock.get("owners")
    if not (isinstance(owners, list) and owners and all(isinstance(o, str) and o.strip() for o in owners) and len(set(owners)) == len(owners)):
        findings.append("the lock's owners are not a list of distinct roles (RB-A11)")
    names = [a["name"] for a in lock["applications"]]
    if sorted(names) != sorted(SERVICES):
        findings.append(f"the lock's applications {names} are not exactly the eight services")
    platform = {p["name"] for p in lock["platform"]}
    for missing in sorted(PLATFORM - platform):
        findings.append(f"platform row {missing} (DD-10 §3) is missing")
    not_verified = []
    for p in lock["platform"]:
        prod, qual = p.get("production", {}), p.get("qualification", {})
        if prod.get("status") == "SUPPLIED":
            if not (prod.get("source") and prod.get("application")):
                findings.append(f"platform {p['name']}: a SUPPLIED production row names its source and the combination application providing it")
        elif prod.get("status") != "REQUIRED" or not prod.get("inputs"):
            findings.append(f"platform {p['name']}: production must be REQUIRED with its ENV inputs until they exist")
        if qual.get("status") not in ("DEVELOPMENT_ONLY", "NOT_RUN") or (qual.get("status") == "NOT_RUN" and not qual.get("reason")):
            findings.append(f"platform {p['name']}: qualification is DEVELOPMENT_ONLY or NOT_RUN with a reason")
        not_verified.append(f"  {p['name']:<25} production REQUIRED ({', '.join(prod.get('inputs', []))})")
    for a in lock["applications"]:
        prod = a["autoscaling"]["production"]
        if isinstance(prod.get("max"), int):
            if not (isinstance(prod.get("min"), int) and prod.get("source")):
                findings.append(f"{a['name']}: a supplied production autoscaling names its minimum and its source")
        elif prod.get("max") != "REQUIRED" or not prod.get("inputs"):
            findings.append(f"{a['name']}: the production autoscaling maximum must stay REQUIRED with its inputs")
        not_verified.append(f"  {a['name']:<25} production maximum, resources and placement REQUIRED ({', '.join(prod.get('inputs', []))})")
    for b in lock["connectionBudgets"]:
        not_verified.append(f"  budget {b['cluster']:<18} production max_connections REQUIRED ({', '.join(b['maxConnections'].get('inputs', []))})")
    return not_verified


def production_view(lock: dict) -> dict:
    """The lock with its production values in the qualification slots, so the
    per-application and budget checks apply to the production combination."""
    lk = copy.deepcopy(lock)
    for a in lk["applications"]:
        prod = a["autoscaling"]["production"]
        a["autoscaling"]["qualification"] = {"max": prod.get("max")}
        if isinstance(prod.get("min"), int):
            a["replicas"]["min"] = max(a["replicas"]["min"], prod["min"])
    for b in lk["connectionBudgets"]:
        b["maxConnections"]["qualification"] = b["maxConnections"].get("production")
    return lk


def check_production(lock: dict, combination: dict, rendered: dict[str, list[dict]], findings: list[str]) -> list[str]:
    """P3.8 AC1: concrete production values against the production combination."""
    for a in lock["applications"]:
        prod = a["autoscaling"]["production"]
        if not (isinstance(prod.get("min"), int) and isinstance(prod.get("max"), int) and prod.get("source")):
            findings.append(f"production {a['name']}: autoscaling needs a concrete minimum, maximum and source (got {prod})")
        elif prod["min"] < 2 or prod["max"] < prod["min"]:
            findings.append(f"production {a['name']}: minimum {prod['min']} below two replicas or above the maximum {prod['max']}")
    for b in lock["connectionBudgets"]:
        if not isinstance(b["maxConnections"].get("production"), int):
            findings.append(f"production budget {b['cluster']}: max_connections is not a concrete value")
    apps = {a["name"] for a in combination.get("applications", [])}
    for p in lock["platform"]:
        prod = p.get("production", {})
        if prod.get("status") != "SUPPLIED":
            findings.append(f"production platform {p['name']}: still {prod.get('status')} ({', '.join(prod.get('inputs', []))})")
        elif prod.get("application") not in apps:
            findings.append(f"production platform {p['name']}: its application {prod.get('application')!r} is absent from the production combination")
    check_combination(combination, findings, lock, "production")
    check_no_standins(lock, combination, findings, "production combination")
    if findings:
        return []
    view = production_view(lock)
    maxima = {}
    for app in view["applications"]:
        if app["name"] not in rendered:
            findings.append(f"production {app['name']}: not rendered with production values")
            continue
        before = len(findings)
        maxima[app["name"]] = check_app(app, view, rendered[app["name"]], findings)["max"]
        findings[before:] = [f"production {x}" for x in findings[before:]]
    before = len(findings)
    report = check_budgets(view, maxima, findings)
    findings[before:] = [f"production {x}" for x in findings[before:]]
    return report


def check_combination(combination: dict, findings: list[str], lock: dict, environment: str) -> None:
    sources = {(a.get("chart") or {}).get("source"): a for a in combination.get("applications", [])}
    for source, name in sorted(REQUIRED_CHARTS.items()):
        if source not in sources:
            findings.append(f"the combination lacks the {name} chart ({source})")
    # RB-A12: every lock application is deployed, or its absence is recorded.
    names = {a["name"] for a in lock["applications"]}
    exempt = {}
    for e in (lock["environments"].get(environment) or {}).get("exemptions") or []:
        if e.get("application") not in names:
            findings.append(f"environment {environment}: exemption for {e.get('application')!r}, which is not a lock application")
        elif not str(e.get("reason") or "").strip():
            findings.append(f"environment {environment}: the exemption of {e['application']} records no reason")
        else:
            exempt[e["application"]] = e["reason"]
    released = {(a.get("chart") or {}).get("release") for a in combination.get("applications", [])}
    for name in sorted(names - released - set(exempt)):
        findings.append(f"the {environment} combination lacks the {name} application and records no exemption (lock environments.{environment}.exemptions)")


def evaluate(lock: dict, rendered: dict[str, list[dict]], combination: dict | None = None) -> tuple[list[str], list[str], list[str]]:
    findings: list[str] = []
    not_verified = check_structure(lock, findings)
    if combination is not None:
        check_combination(combination, findings, lock, "qualification")
        check_capacity_platform(lock, combination, findings)
        check_standin_catalog(lock, combination, findings)
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

    # RB-A12: the qualification lock exempts Inference (host capacity); an
    # exemption excuses only the application it names, with its reason.
    def service_absent(r, lk, cb):
        cb["applications"] = [a for a in cb["applications"] if (a.get("chart") or {}).get("release") != "anvilkit-agent-background-worker"]

    def exemption_removed(r, lk, cb):
        lk["environments"]["qualification"]["exemptions"] = []

    def exemption_without_reason(r, lk, cb):
        lk["environments"]["qualification"]["exemptions"] = [{"application": "anvilkit-agent-inference", "reason": " "}]

    def exemption_of_unknown_application(r, lk, cb):
        lk["environments"]["qualification"]["exemptions"] = [{"application": "anvilkit-agent-inferense", "reason": "typo"}]

    # RB-A11: the capacity rows.
    def resources_drift(r, lk):
        dep(r["anvilkit-agent-api"], "anvilkit-agent-api")["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "512Mi"

    def container_unlisted(r, lk):
        next(a for a in lk["applications"] if a["name"] == "anvilkit-agent-knowledge")["capacity"]["containers"].pop("relay")

    def temp_unbounded(r, lk):
        for v in dep(r["anvilkit-agent-inference"], "anvilkit-agent-inference")["spec"]["template"]["spec"]["volumes"]:
            if "emptyDir" in v:
                v["emptyDir"].pop("sizeLimit", None)

    def owner_unknown(r, lk):
        lk["applications"][2]["capacity"]["owner"] = "Nobody"

    def peak_guessed(r, lk):
        lk["applications"][3]["capacity"]["peak"] = {"measured": 40, "headroom": "REQUIRED", "inputs": ["ENV-05"]}

    def owners_missing(r, lk):
        lk.pop("owners")

    def platform_storage_drift(r, lk, cb):
        qdrant = next(a for a in cb["applications"] if a["name"] == "anvilkit-qdrant")
        qdrant["values"]["persistence"]["size"] = "4Gi"

    def platform_value_absent(r, lk, cb):
        row = next(p for p in lk["platform"] if p["name"] == "nats-jetstream")
        row["capacity"]["storage"][0]["values"] = "config.jetstream.fileStore.pvc.sizes"

    def platform_capacity_missing(r, lk, cb):
        next(p for p in lk["platform"] if p["name"] == "openbao").pop("capacity")

    probes = {"PodDisruptionBudget removed": drop_pdb, "zone spread removed": drop_zone_spread,
              "grace below the drain limit": short_grace, "sidecar without limits": sidecar_without_limits,
              "connection budget exceeded": over_budget, "pool size drift": pool_drift,
              "platform row missing": missing_platform, "production maximum guessed": guessed_production,
              "PDB selects the migration Job": pdb_selects_migration, "network policies absent": no_network_policies,
              "secret delivery absent": no_secret_delivery, "service absent, no exemption": service_absent,
              "exemption removed": exemption_removed, "exemption without a reason": exemption_without_reason,
              "exemption of no lock application": exemption_of_unknown_application,
              "container resources drift": resources_drift, "container without capacity entry": container_unlisted,
              "temporary volume unbounded": temp_unbounded, "capacity owner unknown": owner_unknown,
              "measured peak guessed": peak_guessed, "owners list missing": owners_missing,
              "platform storage drift": platform_storage_drift, "platform value path absent": platform_value_absent,
              "platform capacity row missing": platform_capacity_missing}
    combination_probes = (no_network_policies, no_secret_delivery, service_absent, exemption_removed, exemption_without_reason, exemption_of_unknown_application,
                          platform_storage_drift, platform_value_absent, platform_capacity_missing)
    failures = production_self_test(lock, rendered, combination)
    failures += skeleton_self_test(lock, combination)
    # A recorded exemption with its reason admits the absent service.
    r, lk, cb = copy.deepcopy(rendered), copy.deepcopy(lock), copy.deepcopy(combination)
    service_absent(r, lk, cb)
    lk["environments"]["qualification"].setdefault("exemptions", []).append({"application": "anvilkit-agent-background-worker", "reason": "self-test"})
    exempted, _, _ = evaluate(lk, r, cb)
    print(f"  recorded exemption admits the absent service  {'yes' if not exempted else 'NO: ' + exempted[0]}")
    if exempted:
        failures.append("recorded exemption")
    for label, mutate in probes.items():
        r, lk, cb = copy.deepcopy(rendered), copy.deepcopy(lock), copy.deepcopy(combination)
        if mutate in combination_probes:
            mutate(r, lk, cb)
        else:
            mutate(r, lk)
        findings, _, _ = evaluate(lk, r, cb)
        print(f"  probe {label:<32} {'detected: ' + findings[0] if findings else 'NOT DETECTED'}")
        if not findings:
            failures.append(label)
    return failures


def skeleton_of(lock: dict) -> dict:
    return yaml.safe_load((ROOT / lock["environments"]["production"]["skeleton"]).read_text())


def skeleton_self_test(lock: dict, combination: dict) -> list[str]:
    """RB-A01: the skeleton passes; each stand-in or structural defect, and each
    defect of the stand-in catalog, is reported."""
    skeleton = skeleton_of(lock)
    base: list[str] = []
    check_production_skeleton(lock, skeleton, base)
    check_standin_catalog(lock, combination, base)
    print(f"  production skeleton baseline                   {'passes' if not base else 'FAILS: ' + base[0]}")
    failures = ["skeleton baseline"] if base else []
    q = {a["name"]: a for a in combination["applications"]}

    def app(sk, name):
        return next(a for a in sk["applications"] if a["name"] == name)

    def add_minio(sk, lk, cb):
        sk["applications"].append(copy.deepcopy(q["anvilkit-objects"]))

    def dev_registry(sk, lk, cb):
        sk["registry"] = "anvilkit-dev-registry:5000"

    def single_binary_loki(sk, lk, cb):
        app(sk, "anvilkit-logs")["values"]["deploymentMode"] = "SingleBinary"

    def local_tempo(sk, lk, cb):
        app(sk, "anvilkit-traces")["values"]["storage"]["trace"]["backend"] = "local"

    def no_ceph_cluster(sk, lk, cb):
        sk["applications"] = [a for a in sk["applications"] if a["name"] != "rook-ceph-cluster"]
        app(sk, "rook-ceph")["provides"] = ["ceph", "object-store"]

    def no_cilium(sk, lk, cb):
        sk["bootstrap"] = [b for b in sk["bootstrap"] if b["name"] != "cilium"]

    def development(sk, lk, cb):
        sk["development"] = True

    def unprovided(sk, lk, cb):
        app(sk, "envoy-gateway")["provides"] = []

    def service_missing(sk, lk, cb):
        sk["applications"] = [a for a in sk["applications"] if (a.get("chart") or {}).get("release") != "anvilkit-agent-inference"]

    def pin_drift(sk, lk, cb):
        next(b for b in sk["bootstrap"] if b["name"] == "cilium")["chart"]["version"] = "1.19.0"

    def required_bare(sk, lk, cb):
        app(sk, "metallb")["values"] = {"status": "REQUIRED"}

    def pending_unnamed(sk, lk, cb):
        sk["pending"][0]["decision"] = "later"

    def provides_typo(sk, lk, cb):
        app(sk, "velero")["provides"] = ["cluster-backups"]

    def not_marked(sk, lk, cb):
        sk.pop("skeleton")

    def stale_matcher(sk, lk, cb):
        cb["applications"] = [a for a in cb["applications"] if a["name"] != "anvilkit-objects"]

    def unnamed_standin(sk, lk, cb):
        next(p for p in lk["platform"] if p["name"] == "registry").pop("qualificationStandIns")

    def unknown_standin(sk, lk, cb):
        next(p for p in lk["platform"] if p["name"] == "ceph")["qualificationStandIns"] = ["hostpath"]

    for label, mutate in {"skeleton with MinIO": add_minio, "skeleton on the dev registry": dev_registry,
                          "skeleton with single-binary Loki": single_binary_loki, "skeleton with Tempo's local backend": local_tempo,
                          "skeleton without a Ceph cluster": no_ceph_cluster, "skeleton without Cilium (kindnet)": no_cilium,
                          "skeleton marked development": development, "platform row unprovided": unprovided,
                          "skeleton without a service": service_missing, "skeleton pin drift": pin_drift,
                          "REQUIRED without inputs": required_bare, "pending without a decision": pending_unnamed,
                          "provides names no row": provides_typo, "skeleton not marked": not_marked,
                          "stand-in matcher stale": stale_matcher, "stand-in unnamed by its row": unnamed_standin,
                          "row names an unknown stand-in": unknown_standin}.items():
        sk, lk, cb = copy.deepcopy(skeleton), copy.deepcopy(lock), copy.deepcopy(combination)
        mutate(sk, lk, cb)
        found: list[str] = []
        check_production_skeleton(lk, sk, found)
        check_standin_catalog(lk, cb, found)
        print(f"  probe {label:<40} {'detected: ' + found[0] if found else 'NOT DETECTED'}")
        if not found:
            failures.append(label)
    return failures


def production_self_test(lock: dict, rendered: dict[str, list[dict]], combination: dict) -> list[str]:
    """A synthetic SUPPLIED production lock (the qualification values, each named
    as supplied by the self-test, every platform row provided by a combination
    application) passes; each known production defect is reported."""
    supplied = copy.deepcopy(lock)
    # RB-A01: the synthetic production combination is the production skeleton
    # (no stand-in, every service): the qualification one carries stand-ins.
    qualification = combination
    combination = copy.deepcopy(skeleton_of(lock))
    combination.pop("skeleton", None)
    provider = combination["applications"][0]["name"]
    for a in supplied["applications"]:
        a["autoscaling"]["production"] = {"min": a["replicas"]["min"], "max": a["autoscaling"]["qualification"]["max"], "source": "self-test"}
    for b in supplied["connectionBudgets"]:
        b["maxConnections"]["production"] = b["maxConnections"]["qualification"]
    for p in supplied["platform"]:
        p["production"] = {"status": "SUPPLIED", "source": "self-test", "application": provider}
    failures = []
    base: list[str] = []
    check_production(supplied, combination, rendered, base)
    print(f"  production baseline (synthetic SUPPLIED lock)  {'passes' if not base else 'FAILS: ' + base[0]}")
    if base:
        failures.append("production baseline")

    def floor(lk):
        a = next(x for x in lk["applications"] if x["name"] == "anvilkit-agent-api")
        a["autoscaling"]["production"]["min"] = a["autoscaling"]["production"]["max"] + 1
        a["autoscaling"]["production"]["max"] = a["autoscaling"]["production"]["min"]

    def undersized(lk):
        lk["connectionBudgets"][0]["maxConnections"]["production"] = 10

    def absent(lk):
        lk["platform"][0]["production"]["application"] = "anvilkit-not-in-the-combination"

    def unfilled(lk):
        lk["applications"][1]["autoscaling"]["production"] = {"max": "REQUIRED", "inputs": ["ENV-05"]}

    def without_inference(lk):
        return {**combination, "applications": [a for a in combination["applications"] if (a.get("chart") or {}).get("release") != "anvilkit-agent-inference"]}

    def with_minio(lk):
        objects = next(a for a in qualification["applications"] if a["name"] == "anvilkit-objects")
        return {**combination, "applications": combination["applications"] + [objects]}

    for label, mutate in {"production replica floor missing": floor, "production connection budget undersized": undersized,
                          "platform row absent from the combination": absent, "production value not filled": unfilled,
                          "production combination without Inference": without_inference,
                          "production combination with the MinIO stand-in": with_minio}.items():
        lk = copy.deepcopy(supplied)
        cb = mutate(lk) or combination
        found: list[str] = []
        check_production(lk, cb, rendered, found)
        print(f"  probe {label:<40} {'detected: ' + found[0] if found else 'NOT DETECTED'}")
        if not found:
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
    print("theoretical capacity (qualification, derived from the lock's capacity rows):")
    print("\n".join(capacity_report(lock)))
    print("NOT_VERIFIED (production inputs):")
    print("\n".join(not_verified))
    prod_env = lock["environments"].get("production") or {}
    skeleton = None
    if prod_env.get("skeleton"):
        skeleton = yaml.safe_load((ROOT / prod_env["skeleton"]).read_text())
        print(f"production skeleton ({prod_env['skeleton']}; values ENV inputs, never published):")
        print("\n".join(check_production_skeleton(lock, skeleton, findings)))
    prod_combination = ROOT / prod_env["combination"] if prod_env.get("combination") else None
    if prod_combination and prod_combination.exists():
        prod_values = ROOT / prod_env["values"]
        prod_rendered = {}
        for app in lock["applications"]:
            values = prod_values / f"{app['name']}.yaml"
            if values.exists():
                prod_rendered[app["name"]] = render(helm_bin, app, values)
        prod_report = check_production(lock, yaml.safe_load(prod_combination.read_text()), prod_rendered, findings)
        print("connection budgets (production):")
        print("\n".join(prod_report))
    else:
        print("production: NOT_RUN - no production combination (lock environments.production; its values are ENV-01..ENV-10)")
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
