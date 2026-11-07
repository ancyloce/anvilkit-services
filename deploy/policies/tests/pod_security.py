#!/usr/bin/env python3
"""RB-S09: the Pod Security Admission labels of the namespaces deploy/ defines, and every
workload deploy/ places into a labelled namespace, evaluated at that namespace's level.

Usage: python3 deploy/policies/tests/pod_security.py ROOT KYVERNO HELM WORK [--self-test]

Namespaces, one file per placement: deploy/dev/k8s/namespaces.yaml (development foundation),
deploy/qualification/namespaces.yaml (qualification; bootstrap.py applies it),
deploy/compose/jobs/cluster.yaml and probe.yaml (the Compose placement's Jobs node).

Rules:
  * floor: anvilkit-apps, anvilkit-parsing and anvilkit-gateway enforce restricted wherever a
    placement defines them; anvilkit-components enforces at least baseline. Every labelled
    namespace pins enforce, audit and warn with their versions (VERSION) and audits
    restricted, and a namespace enforcing less than restricted names its exemption
    (the anvilkit.io/pod-security-exemption annotation).
  * consistency: a namespace that several placements define carries the same labels.
  * workloads: what each placement puts into a labelled namespace is rendered and evaluated
    by the pinned Kyverno CLI's podSecurity rule (the upstream pod-security-admission
    library), at that namespace's enforce level and version. The workloads are:
    - qualification: the eight service charts with their qualification values, and every
      application of deploy/gitops/environments/qualification.yaml with a chart in the
      checkout (inline values, imageRefs and the publish.py inputs as placeholders); a release
      chart must be one of the lock's services (rendered as above), and an upstream chart may not
      target a labelled namespace, since it is not rendered offline;
    - development: the four charts deploy/dev/*-chart.sh install, with the scripts'
      placements as placeholders;
    - every placement: the launchers' Job Pods (tests/resources/*-pod.yaml) in the namespace
      they name;
    - Compose Jobs node: the workloads of cluster.yaml and probe.yaml.
    A workload aimed at an unlabelled namespace is counted, not evaluated.

Prints one PASS or FAIL line per case. --self-test applies known defects to copies of the
inputs and requires each one to be reported. Exit 1 on any failure.
"""
from __future__ import annotations

import copy
import pathlib
import re
import subprocess
import sys

import yaml

VERSION = "v1.36"
# The evaluator for VERSION: the pinned Kyverno CLI bundles k8s.io/pod-security-admission
# v0.36.2 (the v1.36 standards), but its podSecurity.version field accepts at most v1.32 and a
# later one silently matches nothing. So VERSION is evaluated as "latest", and only with this CLI.
EVALUATOR = {"cli": "1.19.1", VERSION: "latest"}
RESTRICTED = {"anvilkit-apps", "anvilkit-parsing", "anvilkit-gateway"}
AT_LEAST_BASELINE = {"anvilkit-components"}
EXEMPTION = "anvilkit.io/pod-security-exemption"
PSA = "pod-security.kubernetes.io/"
LEVELS = {"privileged": 0, "baseline": 1, "restricted": 2}
WORKLOADS = {"Pod", "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob"}
PLACEMENT_FILES = {"development": ["deploy/dev/k8s/namespaces.yaml"],
                   "qualification": ["deploy/qualification/namespaces.yaml"],
                   "compose-jobs-node": ["deploy/compose/jobs/cluster.yaml", "deploy/compose/jobs/probe.yaml"]}
DIGEST = "sha256:" + "0" * 64
# Values deploy/gitops/publish.py injects into the combination (never in the checkout).
PUBLISHED = {"anvilkit-admission": ["publicKey=placeholder"], "anvilkit-probes": ["environmentRevision=placeholder"]}
# The placements deploy/dev/*-chart.sh pass on the command line (from .local/dev/env.sh there).
DEV_CHARTS = {"anvilkit-agent-api": ("services/agent/api/deploy/chart", ["control.address=172.18.0.1:9101"]),
              "anvilkit-agent-control": ("services/agent/control/deploy/chart",
                                         ["temporal.address=t:7233", "events.nats.url=tls://n:4222",
                                          "inventory.s3.endpoint=https://s:9000", "artifacts.s3.endpoint=https://s:9000"]),
              "anvilkit-agent-workflow": ("services/agent/workflow/deploy/chart", ["temporal.address=t:7233", "buildId=dev-0"]),
              "anvilkit-agent-model-proxy": ("services/agent/model-proxy/deploy/chart", ["store.s3.endpoint=https://s:9000"])}


def namespaces(root: pathlib.Path) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for placement, files in PLACEMENT_FILES.items():
        for f in files:
            for d in yaml.safe_load_all((root / f).read_text()):
                if d and d.get("kind") == "Namespace":
                    out.setdefault(placement, {})[d["metadata"]["name"]] = d["metadata"]
    return out


def level(md: dict) -> tuple[str, str] | None:
    labels = md.get("labels") or {}
    if PSA + "enforce" not in labels:
        return None
    return labels[PSA + "enforce"], labels.get(PSA + "enforce-version", "")


def check_labels(ns: dict[str, dict[str, dict]]) -> list[tuple[str, str]]:
    """(case, failure or '') per rule."""
    cases = []
    for placement, defs in sorted(ns.items()):
        for name, md in sorted(defs.items()):
            labels = md.get("labels") or {}
            lv = level(md)
            case = f"labels {placement}/{name}"
            if lv is None:
                bad = "no Pod Security labels" if name in RESTRICTED | AT_LEAST_BASELINE else ""
                if bad or any(k.startswith(PSA) for k in labels):
                    cases.append((case, bad or "Pod Security labels without enforce"))
                continue
            problems = []
            if lv[0] not in LEVELS:
                problems.append(f"enforce {lv[0]!r} is not a level")
            if name in RESTRICTED and lv[0] != "restricted":
                problems.append(f"enforces {lv[0]}, needs restricted")
            if name in AT_LEAST_BASELINE and LEVELS.get(lv[0], 0) < LEVELS["baseline"]:
                problems.append(f"enforces {lv[0]}, needs at least baseline")
            for mode in ("enforce", "audit", "warn"):
                if labels.get(PSA + f"{mode}-version") != VERSION:
                    problems.append(f"{mode}-version is not {VERSION}")
            if labels.get(PSA + "audit") != "restricted":
                problems.append("audit is not restricted")
            if lv[0] != "restricted" and not (md.get("annotations") or {}).get(EXEMPTION):
                problems.append(f"enforces {lv[0]} without the {EXEMPTION} annotation")
            cases.append((case, "; ".join(problems)))
    names = {n for defs in ns.values() for n in defs}
    for name in sorted(names):
        sets = {p: {k: v for k, v in (defs[name].get("labels") or {}).items() if k.startswith(PSA)}
                for p, defs in ns.items() if name in defs}
        if len(sets) > 1:
            distinct = {tuple(sorted(s.items())) for s in sets.values()}
            cases.append((f"consistent {name}", "" if len(distinct) == 1 else
                          "placements differ: " + ", ".join(f"{p}={s.get(PSA + 'enforce', 'none')}" for p, s in sorted(sets.items()))))
    return cases


def helm(helm_bin: str, root: pathlib.Path, name: str, chart: str, ns: str, *args: str, values: str = "") -> list[dict]:
    p = subprocess.run([helm_bin, "template", name, str(root / chart), "-n", ns, "-f", "-", *args],
                       input=values or "{}\n", capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"{name}: {p.stderr.strip().splitlines()[-1] if p.stderr.strip() else 'helm template failed'}")
    return [d for d in yaml.safe_load_all(p.stdout) if d]


def workloads(root: pathlib.Path, helm_bin: str) -> tuple[list[tuple[str, str, dict]], list[tuple[str, str]]]:
    """(placement, source, workload) and the rule cases rendering itself produces."""
    out: list[tuple[str, str, dict]] = []
    cases: list[tuple[str, str]] = []

    def take(placement: str, source: str, default_ns: str, docs: list[dict]) -> None:
        for d in docs:
            if d.get("kind") in WORKLOADS:
                d.setdefault("metadata", {}).setdefault("namespace", default_ns)
                out.append((placement, source, d))

    def render(placement: str, source: str, ns: str, *args: str, values: str = "", chart: str) -> None:
        try:
            take(placement, source, ns, helm(helm_bin, root, source, chart, ns, *args, values=values))
        except RuntimeError as e:
            cases.append((f"render {placement}/{source}", str(e)))

    lock = yaml.safe_load((root / "deploy/gitops/lock.yaml").read_text())
    for app in lock["applications"]:
        render("qualification", app["name"], "anvilkit-apps", "-f", str(root / f"deploy/qualification/values/{app['name']}.yaml"),
               "--set", f"image.digest={DIGEST}", chart=app["chart"])
    env = yaml.safe_load((root / "deploy/gitops/environments/qualification.yaml").read_text())
    for app in env["applications"]:
        if "source" not in app["chart"]:
            continue
        args = [a for k, img in (app.get("imageRefs") or {}).items() for a in ("--set", f"{k}=registry/{img}@{DIGEST}")]
        args += [a for kv in PUBLISHED.get(app["name"], []) for a in ("--set", kv)]
        render("qualification", app["name"], app["namespace"], *args, values=yaml.safe_dump(app.get("values") or {}), chart=app["chart"]["source"])
    for name, (chart, sets) in DEV_CHARTS.items():
        render("development", name, "anvilkit-apps", "-f", str(root / f"deploy/dev/values/{name}.yaml"),
               *[a for kv in sets for a in ("--set", kv)], chart=chart)
    for f in sorted((root / "deploy/policies/tests/resources").glob("*-pod.yaml")):
        docs = [d for d in yaml.safe_load_all(f.read_text()) if d]
        for placement in PLACEMENT_FILES:
            take(placement, f.name, "anvilkit-components", copy.deepcopy(docs))
    for f in PLACEMENT_FILES["compose-jobs-node"]:
        take("compose-jobs-node", f, "default", [d for d in yaml.safe_load_all((root / f).read_text()) if d])
    return out, cases


def evaluate(kyverno: str, work: pathlib.Path, ns: dict[str, dict[str, dict]],
             items: list[tuple[str, str, dict]], upstream_ns: dict[str, str]) -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    batches: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    unlabelled = 0
    for i, (placement, source, d) in enumerate(items):
        target = d["metadata"]["namespace"]
        md = ns.get(placement, {}).get(target)
        lv = level(md) if md else None
        if lv is None:
            unlabelled += 1
            continue
        d = copy.deepcopy(d)
        case = f"{placement}/{target} {d['kind']}/{d['metadata'].get('name')} ({source})"
        d["metadata"]["name"] = f"w{i:03d}"
        batches.setdefault(lv, []).append((case, d))
    for name, target in sorted(upstream_ns.items()):
        if any(level(defs[target]) for defs in ns.values() if target in defs):
            cases.append((f"upstream {name}", f"targets the labelled namespace {target}, which no offline render covers"))
    cli = subprocess.run([kyverno, "version"], capture_output=True, text=True).stdout
    cli_ok = re.search(r"^Version: (\S+)", cli, re.M)
    for (lvl, version), batch in sorted(batches.items()):
        if lvl == "privileged":
            cases += [(c, "") for c, _ in batch]
            continue
        if not cli_ok or cli_ok.group(1) != EVALUATOR["cli"] or version not in EVALUATOR:
            cases.append((f"evaluate {lvl}:{version}", f"no evaluator: the {version} standards need Kyverno CLI {EVALUATOR['cli']} "
                          f"(found {cli_ok.group(1) if cli_ok else 'none'})"))
            continue
        policy = work / f"pss-{lvl}.yaml"
        policy.write_text(yaml.safe_dump({"apiVersion": "kyverno.io/v1", "kind": "ClusterPolicy", "metadata": {"name": f"pss-{lvl}"},
                                          "spec": {"validationFailureAction": "Enforce", "background": True, "rules": [{
                                              "name": lvl, "match": {"any": [{"resources": {"kinds": ["Pod"]}}]},
                                              "validate": {"podSecurity": {"level": lvl, "version": EVALUATOR[version]}}}]}}))
        resources = work / f"resources-{lvl}.yaml"
        resources.write_text(yaml.safe_dump_all([d for _, d in batch]))
        p = subprocess.run([kyverno, "apply", str(policy), "--resource", str(resources)], capture_output=True, text=True)
        out = p.stdout + p.stderr
        summary = re.search(r"pass: (\d+), fail: (\d+), warn: \d+, error: (\d+), skip: (\d+)", out)
        failed = {}
        for m in re.finditer(r"-> resource \S+/\S+/(w\d{3}) failed:\s*\n\s*\d+ - \S+ Validation rule '\S+' failed\. (.*)", out):
            failed[m.group(1)] = m.group(2)
        # Every resource evaluated exactly once, no parse error, every reported failure attributed:
        # an unknown version or a malformed batch otherwise passes by evaluating nothing.
        if (not summary or "parsing error" in out or int(summary.group(3)) or int(summary.group(4))
                or int(summary.group(1)) + int(summary.group(2)) != len(batch) or int(summary.group(2)) != len(failed)):
            cases.append((f"evaluate {lvl}:{version}", f"the Kyverno CLI did not evaluate the {len(batch)} workloads: "
                          + (summary.group(0) if summary else out.strip()[-300:])))
            continue
        for case, d in batch:
            why = failed.get(d["metadata"]["name"], "")
            cases.append((f"{lvl} {case}", why[:300]))
    cases.append((f"counted, not evaluated: {unlabelled} workloads in unlabelled namespaces", ""))
    return cases


def run(root: pathlib.Path, kyverno: str, helm_bin: str, work: pathlib.Path,
        ns: dict | None = None, items: list | None = None) -> list[tuple[str, str]]:
    ns = ns if ns is not None else namespaces(root)
    cases = check_labels(ns)
    render_cases: list[tuple[str, str]] = []
    if items is None:
        items, render_cases = workloads(root, helm_bin)
    env = yaml.safe_load((root / "deploy/gitops/environments/qualification.yaml").read_text())
    lock_apps = {a["name"] for a in yaml.safe_load((root / "deploy/gitops/lock.yaml").read_text())["applications"]}
    # A release chart is a service chart of the checkout, rendered above through the lock.
    for a in env["applications"]:
        if "release" in a["chart"] and a["chart"]["release"] not in lock_apps:
            cases.append((f"release {a['name']}", f"release chart {a['chart']['release']} is not a lock application (not rendered)"))
    upstream_ns = {a["name"]: a["namespace"] for a in env["applications"] if not ({"source", "release"} & set(a["chart"]))}
    work.mkdir(parents=True, exist_ok=True)
    return cases + render_cases + evaluate(kyverno, work, ns, items, upstream_ns)


def self_test(root: pathlib.Path, kyverno: str, helm_bin: str, work: pathlib.Path) -> list[tuple[str, str]]:
    ns0 = namespaces(root)
    items0, _ = workloads(root, helm_bin)
    clean = {c for c, why in run(root, kyverno, helm_bin, work / "clean", ns=ns0, items=items0) if why}

    def privileged(items, pick):
        items = copy.deepcopy(items)
        i = next(i for i, (p, s, d) in enumerate(items) if pick(p, s, d))
        spec = items[i][2]["spec"]["template"]["spec"] if items[i][2]["kind"] != "Pod" else items[i][2]["spec"]
        spec["containers"][0].setdefault("securityContext", {})["privileged"] = True
        return items

    def relabel(placement, name, key, value):
        ns = copy.deepcopy(ns0)
        md = ns[placement][name]
        if key == EXEMPTION:
            md.get("annotations", {}).pop(EXEMPTION, None)
        elif value is None:
            md["labels"].pop(key, None)
        else:
            md["labels"][key] = value
        return ns

    defects = {
        "privileged container in anvilkit-apps (qualification service)":
            (None, privileged(items0, lambda p, s, d: p == "qualification" and d["metadata"]["namespace"] == "anvilkit-apps" and d["kind"] == "Deployment")),
        "privileged Job Pod in anvilkit-components (baseline)":
            (None, privileged(items0, lambda p, s, d: d["metadata"]["namespace"] == "anvilkit-components" and d["kind"] == "Pod")),
        "anvilkit-apps without the enforce label (development)":
            (relabel("development", "anvilkit-apps", PSA + "enforce", None), None),
        "anvilkit-parsing at baseline (compose Jobs node)":
            (relabel("compose-jobs-node", "anvilkit-parsing", PSA + "enforce", "baseline"), None),
        "anvilkit-components without its exemption (qualification)":
            (relabel("qualification", "anvilkit-components", EXEMPTION, None), None),
    }
    out = []
    for label, (ns, items) in defects.items():
        found = {c for c, why in run(root, kyverno, helm_bin, work / re.sub(r"\W+", "-", label), ns=ns or ns0, items=items or items0) if why} - clean
        out.append((f"self-test {label}", "" if found else "NOT DETECTED"))
    return out


def main() -> int:
    root, kyverno, helm_bin, work = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], pathlib.Path(sys.argv[4])
    cases = self_test(root, kyverno, helm_bin, work) if "--self-test" in sys.argv[5:] else run(root, kyverno, helm_bin, work)
    failures = 0
    for case, why in cases:
        if why:
            failures += 1
            print(f"FAIL pod-security {case}: {why}")
        else:
            print(f"PASS pod-security {case}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
