#!/usr/bin/env python3
"""RB-A07, SEC-09: the Job launchers' RBAC as every placement installs it, offline.

Usage: python3 deploy/policies/tests/launcher_rbac.py ROOT HELM [--self-test]

The Workflow's Job launcher (services/agent/workflow/deploy/chart, templates/rbac.yaml) and
Knowledge's parser launcher (services/agent/knowledge/deploy/chart, the same template) are
rendered the way each placement installs them:
  * qualification: the lock's charts with their qualification values (as pod_security.py);
  * development: the deploy/dev/*-chart.sh placements (pod_security.DEV_CHARTS);
  * Compose Jobs node: the `$H template` commands of deploy/compose/jobs/jobs-node.sh
    identities(), read from the script itself so that its flags cannot drift from this check.
Every RBAC object they render must keep every rule of RULES: a launcher creates and observes
Jobs; it never runs a command in, attaches to or injects a container into a Pod, never mints a
ServiceAccount token, never reads a Secret, never escalates, binds or impersonates, uses no
wildcard and holds no cluster-scoped permission. Both launchers' Roles must render somewhere.

Prints one PASS or FAIL line per rendered RBAC object (FAIL names the broken rules).
--self-test adds each forbidden grant to a copy of a rendered launcher Role and requires its
rule to report it. Exit 1 on any failure.
"""
from __future__ import annotations

import copy
import pathlib
import re
import shlex
import subprocess
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pod_security import DEV_CHARTS, DIGEST, helm  # noqa: E402

RBAC = {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}
LAUNCHERS = {"workflow launcher": re.compile(r"^anvilkit-agent-workflow.*-launcher$"),
             "parser launcher": re.compile(r"^anvilkit-agent-knowledge.*-parser-launcher$")}
# Subresources that run a command in, attach to, forward into or change a running Pod.
POD_ACCESS = {"pods/exec", "pods/attach", "pods/portforward", "pods/proxy", "pods/ephemeralcontainers", "nodes/proxy", "services/proxy"}


def grants(doc: dict) -> list[tuple[set[str], set[str], set[str]]]:
    return [(set(r.get("apiGroups") or []), set(r.get("resources") or []), set(r.get("verbs") or [])) for r in doc.get("rules") or []]


RULES = [
    ("no-cluster-scope", lambda d: d["kind"].startswith("Cluster")),
    ("no-pod-exec-attach-or-ephemeral", lambda d: any(res & POD_ACCESS for _, res, _ in grants(d))),
    ("no-token-minting", lambda d: any("serviceaccounts/token" in res or ("serviceaccounts" in res and verbs & {"create", "update", "patch"})
                                       for _, res, verbs in grants(d))),
    ("no-secret-read", lambda d: any("secrets" in res and verbs & {"get", "list", "watch"} for _, res, verbs in grants(d))),
    ("no-escalate-bind-impersonate", lambda d: any(verbs & {"escalate", "bind", "impersonate"} for _, _, verbs in grants(d))),
    ("no-wildcard", lambda d: any("*" in groups | res | verbs for groups, res, verbs in grants(d))
                              or any(r.get("nonResourceURLs") for r in d.get("rules") or [])),
]


def broken(doc: dict) -> list[str]:
    return [rule for rule, violates in RULES if violates(doc)]


def compose_commands(root: pathlib.Path) -> list[list[str]]:
    """The `$H template …` invocations of jobs-node.sh identities(), without the `| $K` apply."""
    text = (root / "deploy/compose/jobs/jobs-node.sh").read_text()
    body = text[text.index("identities() {"):]
    body = body[:body.index("\n}\n")]
    out = []
    for m in re.finditer(r"\$H template (.*?)\|", body.replace("\\\n", " "), re.S):
        out.append([a.replace("$ROOT", str(root)) for a in shlex.split(m.group(1))])
    return out


def render(root: pathlib.Path, helm_bin: str) -> tuple[list[tuple[str, dict]], list[str]]:
    """(placement, RBAC object) for every launcher chart render, and render failures."""
    out: list[tuple[str, dict]] = []
    errors: list[str] = []

    def take(placement: str, docs: list[dict]) -> None:
        out.extend((placement, d) for d in docs if d.get("kind") in RBAC)

    charts = {"services/agent/workflow/deploy/chart", "services/agent/knowledge/deploy/chart"}
    lock = yaml.safe_load((root / "deploy/gitops/lock.yaml").read_text())
    for app in lock["applications"]:
        if app["chart"] in charts:
            try:
                take("qualification", helm(helm_bin, root, app["name"], app["chart"], "anvilkit-apps",
                                           "-f", str(root / f"deploy/qualification/values/{app['name']}.yaml"), "--set", f"image.digest={DIGEST}"))
            except RuntimeError as e:
                errors.append(f"qualification {app['name']}: {e}")
    for name, (chart, sets) in DEV_CHARTS.items():
        if chart in charts:
            try:
                take("development", helm(helm_bin, root, name, chart, "anvilkit-apps", "-f", str(root / f"deploy/dev/values/{name}.yaml"),
                                         *[a for kv in sets for a in ("--set", kv)]))
            except RuntimeError as e:
                errors.append(f"development {name}: {e}")
    commands = compose_commands(root)
    if len(commands) != 2:
        errors.append(f"compose-jobs-node: jobs-node.sh identities() renders {len(commands)} charts, not the two launchers")
    for argv in commands:
        p = subprocess.run([helm_bin, "template", *argv], capture_output=True, text=True)
        if p.returncode:
            errors.append(f"compose-jobs-node {argv[0]}: {p.stderr.strip().splitlines()[-1] if p.stderr.strip() else 'helm template failed'}")
            continue
        take("compose-jobs-node", [d for d in yaml.safe_load_all(p.stdout) if d])
    return out, errors


def check(items: list[tuple[str, dict]], errors: list[str]) -> list[tuple[str, str]]:
    """(case, why): why is empty for a pass."""
    cases = [(f"render {e}", e) for e in errors]
    for placement, d in items:
        md = d.get("metadata") or {}
        bad = broken(d)
        cases.append((f"{placement}/{md.get('namespace', '-')} {d['kind']}/{md.get('name')}", ", ".join(bad)))
    roles = [d["metadata"]["name"] for _, d in items if d["kind"] == "Role"]
    for launcher, pattern in LAUNCHERS.items():
        if not any(pattern.match(n) for n in roles):
            cases.append((f"coverage {launcher}", "its Role is rendered by no placement"))
    return cases


def self_test(items: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    base = next(d for p, d in items if p == "compose-jobs-node" and d["kind"] == "Role" and LAUNCHERS["workflow launcher"].match(d["metadata"]["name"]))
    if broken(base):
        return [("self-test baseline", f"the rendered launcher Role already breaks {broken(base)}")]

    def grant(groups: list[str], resources: list[str], verbs: list[str]):
        def mutate(d: dict) -> None:
            d.setdefault("rules", []).append({"apiGroups": groups, "resources": resources, "verbs": verbs})
        return mutate

    probes = {
        "pods/exec create": ("no-pod-exec-attach-or-ephemeral", grant([""], ["pods/exec"], ["create"])),
        "ephemeral container patch": ("no-pod-exec-attach-or-ephemeral", grant([""], ["pods/ephemeralcontainers"], ["patch", "update"])),
        "pods/attach create": ("no-pod-exec-attach-or-ephemeral", grant([""], ["pods/attach"], ["create"])),
        "token request": ("no-token-minting", grant([""], ["serviceaccounts/token"], ["create"])),
        "secret get": ("no-secret-read", grant([""], ["secrets"], ["get"])),
        "role bind": ("no-escalate-bind-impersonate", grant(["rbac.authorization.k8s.io"], ["roles"], ["bind"])),
        "impersonation": ("no-escalate-bind-impersonate", grant([""], ["serviceaccounts"], ["impersonate"])),
        "wildcard verb": ("no-wildcard", grant(["batch"], ["jobs"], ["*"])),
        "cluster role": ("no-cluster-scope", lambda d: d.__setitem__("kind", "ClusterRole")),
    }
    cases = []
    for label, (rule, mutate) in probes.items():
        d = copy.deepcopy(base)
        mutate(d)
        found = broken(d)
        cases.append((f"self-test {label}", "" if rule in found else f"not reported by {rule} (reported: {found})"))
    gone = [(p, d) for p, d in items if not LAUNCHERS["workflow launcher"].match((d.get("metadata") or {}).get("name", ""))]
    missing = [c for c, why in check(gone, []) if c == "coverage workflow launcher" and why]
    cases.append(("self-test launcher Role rendered nowhere", "" if missing else "coverage not reported"))
    return cases


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    root, helm_bin = pathlib.Path(sys.argv[1]).resolve(), sys.argv[2]
    items, errors = render(root, helm_bin)
    cases = self_test(items) if "--self-test" in sys.argv[3:] else check(items, errors)
    for case, why in cases:
        print(f"{'FAIL' if why else 'PASS'} launcher-rbac {case}" + (f": {why}" if why else ""))
    return 1 if any(why for _, why in cases) else 0


if __name__ == "__main__":
    sys.exit(main())
