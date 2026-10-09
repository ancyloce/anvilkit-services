"""P0.4 network policy check of the communication matrix.

  .local/verification-venv/bin/python deploy/qualification/check-network.py --offline [--values FILE]
  .local/verification-venv/bin/python deploy/qualification/check-network.py --cluster --kubeconfig FILE --run RUN [--values FILE]

--offline renders deploy/qualification/platform/network (with the
environment's values, if given) and evaluates the rendered NetworkPolicies
with the standard semantics (a pod is isolated in a direction once a
policy of that type selects it; a connection needs an egress rule at its
source and an ingress rule at its target admitting the peer and the port)
for every ordered pair of components and every port either component
serves. It fails unless exactly the matrix's flows are allowed: every flow
between two in-cluster components, and nothing else — an unlabeled pod of
each namespace reaches no component. It evaluates the policy objects, not a
CNI: it proves the rules implement the matrix, not that a cluster enforces
them.

--cluster deploys one stand-in pod per in-cluster component (the
component's labels, a listener on each of its ports) plus an unlabeled pod
per namespace into a cluster where the chart is installed, then probes
every flow (must connect) and every other pair and port (must time out or
be refused) from the source's stand-in, and records the results under
outputs/qualification/<run>/network/. It needs a NetworkPolicy-enforcing
CNI (Cilium on the target platform); kindnet does not enforce, and the
probe of the unlabeled pod's DNS-only reach tells the two apart.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import itertools
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/qualification/platform/network"
UNLABELED = "unlabeled"
PROBE_IMAGE = "docker.io/library/busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0"


def helm() -> str:
    for c in (ROOT / ".local/bin/helm", shutil.which("helm")):
        if c and pathlib.Path(c).exists():
            return str(c)
    raise SystemExit("UNEXECUTED: helm not found (.local/bin/helm or PATH)")


def load_values(extra: pathlib.Path | None) -> dict:
    v = yaml.safe_load((CHART / "values.yaml").read_text())
    if extra:
        def merge(a, b):
            for k, x in b.items():
                a[k] = merge(a.get(k, {}), x) if isinstance(x, dict) and isinstance(a.get(k), dict) else x
            return a
        v = merge(v, yaml.safe_load(extra.read_text()) or {})
    return v


def render(extra: pathlib.Path | None) -> list[dict]:
    argv = [helm(), "template", "anvilkit-network", str(CHART)]
    if extra:
        argv += ["-f", str(extra)]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d and d.get("kind") == "NetworkPolicy"]


def matches(sel: dict | None, labels: dict) -> bool:
    sel = sel or {}
    if sel.get("matchExpressions"):
        raise ValueError("matchExpressions are not used by this chart")
    return all(labels.get(k) == v for k, v in (sel.get("matchLabels") or {}).items())


def peer_admits(peer: dict, pod: dict) -> bool:
    if "ipBlock" in peer:
        return False  # in-cluster pods are never admitted by a CIDR of this chart
    ns_ok = matches(peer["namespaceSelector"], {"kubernetes.io/metadata.name": pod["namespace"]}) if "namespaceSelector" in peer else pod["namespace"] == peer["_ns"]
    pod_ok = matches(peer["podSelector"], pod["labels"]) if "podSelector" in peer else True
    return ns_ok and pod_ok


def ports_admit(rule: dict, port: int) -> bool:
    ps = rule.get("ports")
    return not ps or any(p.get("port") == port and p.get("protocol", "TCP") == "TCP" for p in ps)


def allowed(policies: list[dict], src: dict, dst: dict, port: int) -> bool:
    def side(pod: dict, kind: str, peer: dict) -> bool:
        selecting = [p for p in policies if p["metadata"]["namespace"] == pod["namespace"] and kind in p["spec"]["policyTypes"] and matches(p["spec"]["podSelector"], pod["labels"])]
        if not selecting:
            return True  # not isolated in this direction
        key, peers = ("egress", "to") if kind == "Egress" else ("ingress", "from")
        for p in selecting:
            for rule in p["spec"].get(key) or []:
                if ports_admit(rule, port) and any(peer_admits(dict(x, _ns=p["metadata"]["namespace"]), peer) for x in rule.get(peers) or []):
                    return True
        return False
    return side(src, "Egress", dst) and side(dst, "Ingress", src)


def pods(values: dict) -> dict[str, dict]:
    out = {name: {"namespace": c["namespace"], "labels": dict(c.get("selector") or {})} for name, c in values["components"].items() if not c.get("external")}
    for ns in values["namespaces"]:
        out[f"{UNLABELED}@{ns}"] = {"namespace": ns, "labels": {"anvilkit.io/probe": "unlabeled"}}
    return out


def component_ports(values: dict) -> dict[str, set[int]]:
    ports: dict[str, set[int]] = {}
    for f in values["flows"]:
        if f.get("to"):
            ports.setdefault(f["to"], set()).update(f.get("ports") or [])
    return ports


def members(values: dict, ps: dict[str, dict], name: str) -> list[str]:
    """The pods that are the component: its stand-in and, for a component
    selecting every pod of its namespace (the Job namespaces), that
    namespace's unlabeled pod too."""
    c = values["components"][name]
    return [name] + ([f"{UNLABELED}@{c['namespace']}"] if not c.get("selector") else [])


def expected_pairs(values: dict, ps: dict[str, dict], served: dict[str, set[int]]) -> set[tuple[str, str, int]]:
    out = set()
    for f in values["flows"]:
        if f.get("from") and f.get("to") and not values["components"][f["from"]].get("external"):
            for src in members(values, ps, f["from"]):
                out |= {(src, f["to"], p) for p in f.get("ports") or []}
    # The stateful namespaces admit every pod of the namespace to every
    # other (replication, membership, raft): intraNamespace in the values.
    for src, dst in itertools.permutations(ps, 2):
        ns = ps[src]["namespace"]
        if ns == ps[dst]["namespace"] and values["namespaces"].get(ns, {}).get("intraNamespace"):
            out |= {(src, dst, p) for p in served.get(dst, set())}
    return out


def offline(extra: pathlib.Path | None) -> int:
    values, policies = load_values(extra), render(extra)
    ps, served = pods(values), component_ports(values)
    expect = expected_pairs(values, ps, served)
    findings, checked = [], 0
    for src, dst in itertools.permutations(ps, 2):
        for port in sorted(served.get(dst, set())):
            checked += 1
            got, want = allowed(policies, ps[src], ps[dst], port), (src, dst, port) in expect
            if got != want:
                findings.append(f"{src} -> {dst}:{port} {'allowed' if got else 'refused'}, the matrix says {'allowed' if want else 'refused'}")
    print(f"offline: {len(policies)} policies, {len(ps)} pods (with one unlabeled pod per namespace), {checked} (source, target, port) cases, {len(expect)} matrix flows")
    for f in findings:
        print(f"  FINDING {f}")
    if findings:
        return 1
    print("offline: exactly the matrix's in-cluster flows (and the stateful namespaces' own traffic) are allowed; every other pair and port is refused, and an unlabeled pod of the service namespace reaches nothing")
    return 0


def kubectl(kubeconfig: str, *args: str, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", "--kubeconfig", kubeconfig, *args], input=stdin, capture_output=True, text=True, check=check)


def cluster(extra: pathlib.Path | None, kubeconfig: str, run: str) -> int:
    values = load_values(extra)
    ps, served = pods(values), component_ports(values)
    expect = expected_pairs(values, ps, served)
    out_dir = ROOT / "outputs/qualification" / run / "network"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifests = []
    for name, p in ps.items():
        listen = sorted(served.get(name, set()))
        script = " ".join(f"httpd -p {port} -h /w;" for port in listen) + " sleep 1d"
        manifests.append({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": f"np-{name.replace('@', '-').lower()}"[:63].rstrip("-"), "namespace": p["namespace"],
                          "labels": dict(p["labels"], **{"anvilkit.io/network-probe": run})},
                          "spec": {"restartPolicy": "Never", "terminationGracePeriodSeconds": 0, "containers": [{"name": "probe", "image": PROBE_IMAGE, "imagePullPolicy": "IfNotPresent",
                                   "command": ["sh", "-c", "mkdir -p /w && echo ok > /w/index.html && " + script]}]}})
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump_all(manifests, f)
    kubectl(kubeconfig, "apply", "-f", f.name)
    results, findings = [], []
    try:
        for m in manifests:
            kubectl(kubeconfig, "-n", m["metadata"]["namespace"], "wait", "--for=condition=Ready", f"pod/{m['metadata']['name']}", "--timeout=180s")
        ip = {}
        for name, m in zip(ps, manifests):
            ip[name] = kubectl(kubeconfig, "-n", m["metadata"]["namespace"], "get", "pod", m["metadata"]["name"], "-o", "jsonpath={.status.podIP}").stdout.strip()
        pod_of = dict(zip(ps, manifests))

        # One exec per source probes its targets in turn (a refused probe
        # costs the full 3 s timeout); the sources probe concurrently.
        def probe(src: str) -> list[tuple[str, int, bool]]:
            cases = [(dst, port) for dst in ps if dst != src for port in sorted(served.get(dst, set()))]
            if not cases:
                return []
            script = "".join(f"if wget -q -T 3 -O /dev/null http://{ip[dst]}:{port}/ 2>/dev/null; then echo {i} 1; else echo {i} 0; fi; " for i, (dst, port) in enumerate(cases))
            m = pod_of[src]
            out = kubectl(kubeconfig, "-n", m["metadata"]["namespace"], "exec", m["metadata"]["name"], "--", "sh", "-c", script).stdout.split("\n")
            got = dict(line.split() for line in out if line.strip())
            if len(got) != len(cases):
                raise SystemExit(f"{src}: {len(got)} of {len(cases)} probe results")
            return [(dst, port, got[str(i)] == "1") for i, (dst, port) in enumerate(cases)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(ps)) as pool:
            probed = dict(zip(ps, pool.map(probe, ps)))
        for src, dst in itertools.permutations(ps, 2):
            for port in sorted(served.get(dst, set())):
                got = next(c for d, p, c in probed[src] if d == dst and p == port)
                want = (src, dst, port) in expect
                results.append({"source": src, "target": dst, "port": port, "connected": got, "matrix": want})
                if got != want:
                    findings.append(f"{src} -> {dst}:{port} {'connected' if got else 'refused'}, the matrix says {'allowed' if want else 'refused'}")
    finally:
        for m in manifests:
            kubectl(kubeconfig, "-n", m["metadata"]["namespace"], "delete", "pod", m["metadata"]["name"], "--wait=false", check=False)
    (out_dir / "results.json").write_text(json.dumps({"run": run, "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results, "findings": findings}, indent=2) + "\n")
    print(f"cluster: {len(results)} probes, {len(findings)} findings -> {out_dir.relative_to(ROOT)}/results.json")
    for f in findings:
        print(f"  FINDING {f}")
    return 1 if findings else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--cluster", action="store_true")
    ap.add_argument("--values", type=pathlib.Path)
    ap.add_argument("--kubeconfig")
    ap.add_argument("--run")
    a = ap.parse_args()
    if a.offline:
        return offline(a.values)
    kubeconfig = a.kubeconfig or os.environ.get("ANVILKIT_NETWORK_KUBECONFIG")
    if not kubeconfig:
        ap.error("--cluster needs --kubeconfig (or ANVILKIT_NETWORK_KUBECONFIG)")
    return cluster(a.values, kubeconfig, a.run or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))


if __name__ == "__main__":
    sys.exit(main())
