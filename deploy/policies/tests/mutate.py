#!/usr/bin/env python3
"""Derives the negative admission cases from the launcher's rendered fixtures.

Usage: python3 deploy/policies/tests/mutate.py RESOURCES_DIR OUT_DIR

Each case is one substitution a request must not be able to make: another image, a
tag instead of a digest, an entrypoint change, extra arguments, an extra or missing
environment variable, a forged Pod UID, a hostPath or Secret volume, an extra mount,
a subPath or mount propagation, a lifecycle hook, an exec probe, a capability,
privilege, a writable root, a missing or foreign seccomp profile, a ServiceAccount
token, host namespaces, an ephemeral or extra container, a foreign registry (the
native sidecar included), a replaced or altered fixture command, a profile id that
does not match the layout, the candidate-code label or the RuntimeClass (a candidate
profile relabeled candidate-code=false without its RuntimeClass), Job retries, and
the same template substitutions submitted as a Job (which Kyverno autogen evaluates
at Job admission). OUT_DIR receives one YAML per case (name = case id) and
expectations.json (case -> expected outcome).
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys
import subprocess
import time
import uuid

import yaml  # PyYAML (the verification venv)


def load(path: pathlib.Path):
    return yaml.safe_load(path.read_text())


def container(obj, name):
    for c in obj["spec"].get("containers", []) + obj["spec"].get("initContainers", []):
        if c["name"] == name:
            return c
    raise KeyError(name)


def cases(resources: pathlib.Path):
    harness = load(resources / "harness-wiring-dev-v1-pod.yaml")
    candidate = load(resources / "codegen-fixed-v1-pod.yaml")
    fixture = load(resources / "local-check-v1-pod.yaml")
    job = load(resources / "harness-wiring-dev-v1-job.yaml")
    fixture_job = load(resources / "local-check-v1-job.yaml")
    candidate_job = load(resources / "codegen-fixed-v1-job.yaml")
    validator = load(resources / "validator-fixed-dev-v1-pod.yaml")
    validator_job = load(resources / "validator-fixed-dev-v1-job.yaml")
    out = {}

    def add(case, base, mutate, expect="fail"):
        obj = copy.deepcopy(base)
        mutate(obj)
        obj["metadata"]["name"] = case
        out[case] = (obj, expect)

    # Positives: exactly what the launcher renders.
    add("valid-harness-pod", harness, lambda o: None, "pass")
    add("valid-candidate-pod", candidate, lambda o: None, "pass")
    add("valid-fixture-pod", fixture, lambda o: None, "pass")
    add("valid-harness-job", job, lambda o: None, "pass")
    add("valid-fixture-job", fixture_job, lambda o: None, "pass")
    add("valid-validator-pod", validator, lambda o: None, "pass")
    add("valid-validator-job", validator_job, lambda o: None, "pass")
    # The fixed validator (P10d): its command and image bind its profile id;
    # the codegen supervisor's command or image under the validator profile,
    # the validator's under a codegen profile, or a validator relabeled as
    # candidate code are refused.
    add("validator-with-codegen-command", validator, lambda o: container(o, "supervisor").__setitem__("command", ["/usr/local/bin/anvilkit-codegen-supervisor"]))
    add("validator-with-codegen-image", validator, lambda o: container(o, "supervisor").__setitem__("image", container(harness, "supervisor")["image"]))
    add("validator-command-extended", validator, lambda o: container(o, "supervisor")["command"].append("--unsafe"))
    add("validator-claims-harness-profile", validator, lambda o: o["metadata"]["labels"].__setitem__("anvilkit.io/profile-id", "harness-wiring-dev-v1"))
    add("harness-claims-validator-profile", harness, lambda o: o["metadata"]["labels"].__setitem__("anvilkit.io/profile-id", "validator-fixed-dev-v1"))
    add("validator-as-candidate-code", validator, lambda o: o["metadata"]["labels"].__setitem__("anvilkit.io/candidate-code", "true"))
    add("validator-with-runtime-class", validator, lambda o: o["spec"].__setitem__("runtimeClassName", "gvisor"))
    add("validator-job-with-codegen-command", validator_job, lambda o: container(o["spec"]["template"], "supervisor").__setitem__("command", ["/usr/local/bin/anvilkit-codegen-supervisor"]))
    add("valid-candidate-job", candidate_job, lambda o: None, "pass")

    def sup(o):
        return container(o, "supervisor")

    def side(o):
        return container(o, "access-sidecar")

    add("image-other-digest", harness, lambda o: sup(o).__setitem__("image", sup(o)["image"][:-8] + "deadbeef"))
    add("image-tag-not-digest", harness, lambda o: sup(o).__setitem__("image", "localhost:5001/anvilkit-codegen:dev"))
    add("image-foreign-registry", harness, lambda o: sup(o).__setitem__("image", "docker.io/evil/" + sup(o)["image"].split("/", 1)[1]))
    add("sidecar-image-swapped", harness, lambda o: side(o).__setitem__("image", sup(o)["image"]))
    add("entrypoint-changed", harness, lambda o: sup(o).__setitem__("command", ["/bin/sh", "-c", "id"]))
    add("args-added", harness, lambda o: sup(o).__setitem__("args", ["--unsafe"]))
    add("env-added", harness, lambda o: sup(o)["env"].append({"name": "AWS_SECRET_ACCESS_KEY", "value": "x"}))
    add("env-removed", harness, lambda o: sup(o)["env"].pop())
    add("envfrom-added", harness, lambda o: sup(o).__setitem__("envFrom", [{"secretRef": {"name": "s"}}]))
    add("pod-uid-forged", harness, lambda o: [e.update({"value": "forged"}) or e.pop("valueFrom", None) for e in side(o)["env"] if e["name"] == "ANVILKIT_SIDECAR_POD_UID"])
    add("sidecar-env-added", harness, lambda o: side(o)["env"].append({"name": "ANVILKIT_SIDECAR_SOCKETS_DIR", "value": "/tmp"}))
    add("volume-hostpath", harness, lambda o: o["spec"]["volumes"].append({"name": "host", "hostPath": {"path": "/"}}))
    add("volume-secret", harness, lambda o: o["spec"]["volumes"].__setitem__(0, {"name": "workspace", "secret": {"secretName": "s"}}))
    add("volume-csi", harness, lambda o: o["spec"]["volumes"].append({"name": "csi", "csi": {"driver": "secrets-store.csi.k8s.io"}}))
    add("mount-added-to-sidecar", harness, lambda o: side(o)["volumeMounts"].append({"name": "verdict", "mountPath": "/anvilkit/verdict"}))
    add("sockets-writable-in-supervisor", harness, lambda o: [m.__setitem__("readOnly", False) for m in sup(o)["volumeMounts"] if m["name"] == "sockets"])
    add("capability-added", harness, lambda o: sup(o)["securityContext"]["capabilities"]["add"].append("NET_ADMIN"))
    add("capability-sidecar", harness, lambda o: side(o)["securityContext"]["capabilities"].__setitem__("add", ["SETUID"]))
    add("capabilities-not-dropped", harness, lambda o: sup(o)["securityContext"]["capabilities"].__setitem__("drop", []))
    add("privileged", harness, lambda o: sup(o)["securityContext"].__setitem__("privileged", True))
    add("privilege-escalation", harness, lambda o: sup(o)["securityContext"].__setitem__("allowPrivilegeEscalation", True))
    add("writable-root", harness, lambda o: sup(o)["securityContext"].__setitem__("readOnlyRootFilesystem", False))
    add("seccomp-missing", harness, lambda o: sup(o)["securityContext"].pop("seccompProfile"))
    add("seccomp-other-profile", harness, lambda o: sup(o)["securityContext"]["seccompProfile"].__setitem__("localhostProfile", "other/profile.json"))
    add("seccomp-unconfined", harness, lambda o: sup(o)["securityContext"].__setitem__("seccompProfile", {"type": "Unconfined"}))
    add("sidecar-as-root", harness, lambda o: side(o)["securityContext"].update({"runAsUser": 0, "runAsNonRoot": False}))
    add("supervisor-supplemental-groups", harness, lambda o: o["spec"]["securityContext"].__setitem__("supplementalGroups", [0, 10001, 10002]))
    add("sa-token", harness, lambda o: o["spec"].__setitem__("automountServiceAccountToken", True))
    add("service-account", harness, lambda o: o["spec"].__setitem__("serviceAccountName", "anvilkit-agent-workflow"))
    add("host-network", harness, lambda o: o["spec"].__setitem__("hostNetwork", True))
    add("host-pid", harness, lambda o: o["spec"].__setitem__("hostPID", True))
    add("shared-process-namespace", harness, lambda o: o["spec"].__setitem__("shareProcessNamespace", True))
    add("restart-always", harness, lambda o: o["spec"].__setitem__("restartPolicy", "Always"))
    add("node-pool-changed", harness, lambda o: o["spec"].__setitem__("nodeSelector", {}))
    add("node-name", harness, lambda o: o["spec"].__setitem__("nodeName", "unreviewed-node"))
    add("scheduler-unreviewed", harness, lambda o: o["spec"].__setitem__("schedulerName", "unreviewed-scheduler"))
    add("valid-default-scheduler", harness, lambda o: o["spec"].__setitem__("schedulerName", "default-scheduler"), "pass")
    # Both containers and all temporary volumes, as Pods and at Job admission.
    for prefix, base, spec in [("pod", harness, lambda o: o), ("job", job, lambda o: o["spec"]["template"])]:
        add(prefix + "-node-name", base, lambda o: spec(o)["spec"].__setitem__("nodeName", "unreviewed-node"))
        add(prefix + "-scheduler-unreviewed", base, lambda o: spec(o)["spec"].__setitem__("schedulerName", "unreviewed-scheduler"))
        for name in ["supervisor", "access-sidecar"]:
            add(prefix + "-" + name + "-resources-removed", base, lambda o: container(spec(o), name).pop("resources"))
            add(prefix + "-" + name + "-limits-removed", base, lambda o: container(spec(o), name)["resources"].pop("limits"))
            for field in ["requests", "limits"]:
                for resource, value in [("cpu", "10m" if field == "requests" else "2"), ("memory", "16Mi" if field == "requests" else "8Gi")]:
                    add(prefix + "-" + name + "-" + field + "-" + resource, base,
                        lambda o: container(spec(o), name)["resources"][field].__setitem__(resource, value))
        for name in ["workspace", "verdict", "sockets"]:
            def volume(o):
                return next(v["emptyDir"] for v in spec(o)["spec"]["volumes"] if v["name"] == name)
            add(prefix + "-" + name + "-size-removed", base, lambda o: volume(o).pop("sizeLimit"))
            add(prefix + "-" + name + "-size-changed", base, lambda o: volume(o).__setitem__("sizeLimit", "8Gi"))
            add(prefix + "-" + name + "-medium-changed", base, lambda o: volume(o).__setitem__("medium", "" if name == "sockets" else "Memory"))
        add(prefix + "-sockets-medium-removed", base, lambda o: next(v["emptyDir"] for v in spec(o)["spec"]["volumes"] if v["name"] == "sockets").pop("medium"))
    add("extra-container", harness, lambda o: o["spec"]["containers"].append(copy.deepcopy(sup(o)) | {"name": "extra"}))
    add("init-container", harness, lambda o: o["spec"]["initContainers"].append({"name": "init", "image": sup(o)["image"], "command": ["true"]}))
    add("sidecar-not-native", harness, lambda o: side(o).pop("restartPolicy"))
    add("sidecar-as-main-container", harness, lambda o: (o["spec"]["containers"].append(o["spec"]["initContainers"].pop()), o["spec"].pop("initContainers")))
    add("supervisor-restarts", harness, lambda o: sup(o).__setitem__("restartPolicy", "Always"))
    add("fixture-with-init", fixture, lambda o: o["spec"].__setitem__("initContainers", [{"name": "init", "image": container(o, "fixture")["image"], "command": ["true"]}]))
    add("ephemeral-container", harness, lambda o: o["spec"].__setitem__("ephemeralContainers", [{"name": "debug", "image": sup(o)["image"]}]))
    add("labels-missing", harness, lambda o: o["metadata"]["labels"].pop("anvilkit.io/launch-key"))
    add("marker-missing", harness, lambda o: o["metadata"].pop("annotations"))
    add("candidate-without-runtimeclass", candidate, lambda o: o["spec"].pop("runtimeClassName"))
    add("candidate-runc-runtimeclass", candidate, lambda o: o["spec"].__setitem__("runtimeClassName", "runc"))
    add("wiring-claims-gvisor", harness, lambda o: o["spec"].__setitem__("runtimeClassName", "gvisor"))
    add("wiring-labelled-candidate", harness, lambda o: o["metadata"]["labels"].__setitem__("anvilkit.io/candidate-code", "true"))
    add("fixture-with-privilege", fixture, lambda o: container(o, "fixture")["securityContext"]["capabilities"].__setitem__("add", ["SETUID"]))
    add("fixture-with-volume", fixture, lambda o: o["spec"].__setitem__("volumes", [{"name": "w", "emptyDir": {}}]))
    add("fixture-other-image", fixture, lambda o: container(o, "fixture").__setitem__("image", "docker.io/library/alpine@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b"))
    # Lifecycle hooks, probes and mount extensions on any container.
    add("poststart-hook", harness, lambda o: sup(o).__setitem__("lifecycle", {"postStart": {"exec": {"command": ["/bin/sh", "-c", "id"]}}}))
    add("prestop-hook-sidecar", harness, lambda o: side(o).__setitem__("lifecycle", {"preStop": {"exec": {"command": ["/bin/sh", "-c", "id"]}}}))
    add("liveness-exec-probe", harness, lambda o: sup(o).__setitem__("livenessProbe", {"exec": {"command": ["/bin/sh", "-c", "id"]}}))
    add("readiness-exec-probe-sidecar", harness, lambda o: side(o).__setitem__("readinessProbe", {"exec": {"command": ["/bin/true"]}}))
    add("startup-probe", harness, lambda o: sup(o).__setitem__("startupProbe", {"httpGet": {"path": "/", "port": 8080}}))
    add("fixture-poststart-hook", fixture, lambda o: container(o, "fixture").__setitem__("lifecycle", {"postStart": {"exec": {"command": ["/bin/sh", "-c", "id"]}}}))
    add("fixture-exec-probe", fixture, lambda o: container(o, "fixture").__setitem__("livenessProbe", {"exec": {"command": ["/bin/true"]}}))
    add("subpath-mount", harness, lambda o: [m.__setitem__("subPath", "x") for m in sup(o)["volumeMounts"] if m["name"] == "workspace"])
    add("subpathexpr-mount", harness, lambda o: [m.__setitem__("subPathExpr", "$(ANVILKIT_LAUNCH_ID)") for m in sup(o)["volumeMounts"] if m["name"] == "verdict"])
    add("mount-propagation", harness, lambda o: [m.__setitem__("mountPropagation", "Bidirectional") for m in side(o)["volumeMounts"]])
    # The fixture command is the reviewed entrypoint, verbatim.
    add("fixture-command-replaced", fixture, lambda o: container(o, "fixture").__setitem__("command", ["/bin/sh", "-c", "id > /dev/termination-log"]))
    add("fixture-command-altered", fixture, lambda o: container(o, "fixture")["command"].__setitem__(2, container(o, "fixture")["command"][2].replace('"sizeBytes":"24"', '"sizeBytes":"25"')))
    add("fixture-command-extra-word", fixture, lambda o: container(o, "fixture")["command"].append("extra"))
    # The registry allowlist covers the native sidecar (an init container).
    add("sidecar-foreign-registry", harness, lambda o: side(o).__setitem__("image", "docker.io/evil/" + side(o)["image"].split("/", 1)[1]))
    # The profile id binds the layout, the candidate-code label and the
    # RuntimeClass: relabeling cannot remove the gVisor requirement.
    add("candidate-relabeled-no-runtimeclass", candidate, lambda o: (o["metadata"]["labels"].__setitem__("anvilkit.io/candidate-code", "false"), o["spec"].pop("runtimeClassName")))
    add("candidate-relabeled-keeps-runtimeclass", candidate, lambda o: o["metadata"]["labels"].__setitem__("anvilkit.io/candidate-code", "false"))
    add("harness-claims-candidate-profile", harness, lambda o: o["metadata"]["labels"].__setitem__("anvilkit.io/profile-id", "codegen-fixed-v1"))
    add("fixture-claims-harness-profile", fixture, lambda o: o["metadata"]["labels"].__setitem__("anvilkit.io/profile-id", "harness-wiring-dev-v1"))
    add("harness-claims-fixture-profile", harness, lambda o: o["metadata"]["labels"].__setitem__("anvilkit.io/profile-id", "local-check-v1"))
    add("profile-unknown", harness, lambda o: o["metadata"]["labels"].__setitem__("anvilkit.io/profile-id", "codegen-fixed-v2"))
    # The same substitutions submitted as a Job: Kyverno autogen evaluates
    # the Pod rules against the template at Job admission, with the
    # launcher's own identity in the cluster check.
    tmpl = lambda o: o["spec"]["template"]

    def tsup(o):
        return [c for c in tmpl(o)["spec"]["containers"] if c["name"] == "supervisor"][0]

    add("job-template-env-added", job, lambda o: tsup(o)["env"].append({"name": "AWS_SECRET_ACCESS_KEY", "value": "x"}))
    add("job-template-poststart-hook", job, lambda o: tsup(o).__setitem__("lifecycle", {"postStart": {"exec": {"command": ["/bin/sh", "-c", "id"]}}}))
    add("job-template-exec-probe", job, lambda o: tsup(o).__setitem__("livenessProbe", {"exec": {"command": ["/bin/true"]}}))
    add("job-template-subpath", job, lambda o: [m.__setitem__("subPath", "x") for m in tsup(o)["volumeMounts"] if m["name"] == "workspace"])
    add("job-template-sa-token", job, lambda o: tmpl(o)["spec"].__setitem__("automountServiceAccountToken", True))
    add("job-template-hostpath", job, lambda o: tmpl(o)["spec"]["volumes"].append({"name": "host", "hostPath": {"path": "/"}}))
    add("job-template-capability", job, lambda o: tsup(o)["securityContext"]["capabilities"]["add"].append("NET_ADMIN"))
    add("job-template-sidecar-foreign-registry", job, lambda o: [c.__setitem__("image", "docker.io/evil/" + c["image"].split("/", 1)[1]) for c in tmpl(o)["spec"]["initContainers"]])
    add("job-fixture-command-replaced", fixture_job, lambda o: tmpl(o)["spec"]["containers"][0].__setitem__("command", ["/bin/sh", "-c", "id > /dev/termination-log"]))
    add("job-fixture-poststart-hook", fixture_job, lambda o: tmpl(o)["spec"]["containers"][0].__setitem__("lifecycle", {"postStart": {"exec": {"command": ["/bin/sh", "-c", "id"]}}}))
    add("job-candidate-relabeled-no-runtimeclass", candidate_job, lambda o: (o["metadata"]["labels"].__setitem__("anvilkit.io/candidate-code", "false"), tmpl(o)["metadata"]["labels"].__setitem__("anvilkit.io/candidate-code", "false"), tmpl(o)["spec"].pop("runtimeClassName")))
    add("job-candidate-without-runtimeclass", candidate_job, lambda o: tmpl(o)["spec"].pop("runtimeClassName"))
    add("job-retries", job, lambda o: o["spec"].__setitem__("backoffLimit", 3))
    add("job-parallel", job, lambda o: o["spec"].__setitem__("parallelism", 2))
    add("job-no-deadline", job, lambda o: o["spec"].pop("activeDeadlineSeconds"))
    add("job-ttl", job, lambda o: o["spec"].__setitem__("ttlSecondsAfterFinished", 0))
    return out


def cluster_lifecycle(resources, context, launcher):
    """Use real scheduler binding, Pod UPDATE and ephemeralcontainers UPDATE.

    Only this probe's Job is created/deleted. The launcher creates Jobs; the
    administrator observes and tests subresources the launcher cannot access.
    """
    admin = ["kubectl", "--context", context]
    identity = ["kubectl", "--kubeconfig", launcher]
    namespace = "anvilkit-components"

    def run(who, args, obj=None):
        return subprocess.run(who + args, input=None if obj is None else json.dumps(obj),
                              capture_output=True, text=True, timeout=90)

    def passed(proc, label):
        if proc.returncode:
            raise RuntimeError(label + ": " + proc.stderr)
        print("PASS cluster " + label, flush=True)
        return proc.stdout

    job = load(resources / "harness-wiring-dev-v1-job.yaml")
    job["metadata"]["name"] = "p09-policy-" + uuid.uuid4().hex[:10]
    name = job["metadata"]["name"]
    passed(run(identity, ["create", "-f", "-"], job), "launcher creates scheduling probe")
    try:
        deadline = time.monotonic() + 60
        pod = None
        while time.monotonic() < deadline:
            result = run(admin, ["get", "pods", "-n", namespace, "-l", "batch.kubernetes.io/job-name=" + name, "-o", "json"])
            if result.returncode:
                raise RuntimeError(result.stderr)
            items = json.loads(result.stdout)["items"]
            if items and items[0]["spec"].get("nodeName"):
                pod = items[0]
                break
            time.sleep(0.5)
        if pod is None:
            raise RuntimeError("scheduler did not bind the probe Pod")
        print("PASS cluster normal default-scheduler binding to " + pod["spec"]["nodeName"], flush=True)
        # The existing nodeName must not prevent an otherwise valid update.
        passed(run(admin, ["annotate", "pod", pod["metadata"]["name"], "-n", namespace,
                            "anvilkit.io/p09-update=verified", "--overwrite"]), "scheduled Pod UPDATE")
        pod = json.loads(passed(run(admin, ["get", "pod", pod["metadata"]["name"], "-n", namespace, "-o", "json"]), "read scheduled Pod"))
        path = "/api/v1/namespaces/" + namespace + "/pods/" + pod["metadata"]["name"] + "/ephemeralcontainers"
        pod["spec"]["ephemeralContainers"] = [{"name": "p09-debug", "image": pod["spec"]["containers"][0]["image"], "command": ["/bin/true"]}]
        denied = run(identity, ["replace", "--raw", path, "-f", "-"], pod)
        if denied.returncode == 0 or "cannot update resource" not in denied.stderr or '"pods/ephemeralcontainers"' not in denied.stderr:
            raise RuntimeError("expected launcher RBAC rejection: " + denied.stderr)
        print("PASS cluster ephemeralcontainers UPDATE launcher: RBAC rejection", flush=True)
        denied = run(admin, ["replace", "--raw", path, "-f", "-"], pod)
        if denied.returncode == 0 or "anvilkit-components-ephemeral" not in denied.stderr or "denied" not in denied.stderr:
            raise RuntimeError("expected admin policy rejection (not structural/RBAC): " + denied.stderr)
        print("PASS cluster ephemeralcontainers UPDATE admin: policy rejection", flush=True)
    finally:
        passed(run(identity, ["delete", "job", name, "-n", namespace, "--wait=true", "--timeout=60s"]), "remove only the probe Job")
    return 0


def main() -> int:
    if sys.argv[1] == "--cluster-lifecycle":
        return cluster_lifecycle(pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    resources, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    expectations = {}
    for case, (obj, expect) in cases(resources).items():
        (out / f"{case}.yaml").write_text(yaml.safe_dump(obj, sort_keys=False))
        expectations[case] = expect
    (out / "expectations.json").write_text(json.dumps(expectations, indent=1, sort_keys=True) + "\n")
    print(f"{len(expectations)} cases written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
