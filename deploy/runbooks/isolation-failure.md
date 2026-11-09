# Isolation failure

**Alerts.** `AnvilKitIsolationAdmissionRefused`: an AnvilKit admission policy (`anvilkit-*`) refused a workload or could not evaluate it in the last 15 minutes, by `policy_name` (Kyverno 1.19.1's `kyverno_validating_policy_results_total` and `kyverno_image_validating_policy_results_total` with `result` `fail` or `error`, scraped from the admission controller by the observability chart's `anvilkit-kyverno` PodMonitor). The other signals are Kyverno's policy reports, the Workflow's refused launches, and runtime findings. `AnvilKitTargetDown` and `AnvilKitForcedStop` may accompany a node-level failure ([rollback.md](rollback.md)).

**Dashboards.** `anvilkit-workflow` (launches, observations), `anvilkit-overview`.

**What it means.** A failure of the candidate-code boundary (trust regions (architecture `security.md`)) is any of the following:
- a candidate Pod admitted with a substituted template, image, service account or capability;
- a sandbox escape, or a candidate reaching the network, a credential or a trusted socket;
- a profile running outside its runtime class (gVisor for candidate code).

The admission policies (`anvilkit-components-pods`, `-jobs`, `-ephemeral`; `anvilkit-parsing-*`) and the launcher's least-privilege RBAC are the first line; a failure means one of them did not hold.

**Diagnose.**

```sh
$K get policyreports -n anvilkit-components -o wide; $K get policyreports -n anvilkit-parsing -o wide
$K -n anvilkit-components get pods -L anvilkit.io/profile-id,anvilkit.io/candidate-code -o wide
$K -n anvilkit-components get events --field-selector reason=PolicyViolation --sort-by=.lastTimestamp | tail -20
$K -n anvilkit-apps logs deploy/anvilkit-agent-workflow --since=1h | grep -iE 'admission|refused|denied' | tail -30
```

Check the label names against the deployed launcher (`internal/adapters/kubernetes` of the Workflow).

**Remedy.**

1. Suspend the affected profile: remove it from the Workflow's `kubernetes.enabled_profiles` in the environment's values and roll the Workflow. No new Job of the profile is created: the Workflow's launcher refuses a profile it does not enable (`PROFILE_UNQUALIFIED`).
2. Terminate the profile's running instances (delete their Jobs in `anvilkit-components`). The attempts end with what the evidence supports, and their sends were fenced by Control.
3. Revoke and rotate the credentials the boundary protects: the access sidecar's identity and the per-launch Secrets ([secret-rotation.md](secret-rotation.md)); provider and MCP credentials if a send path was reached.
4. Never re-enable the profile with a weaker template, a privileged container or another runtime class as a fallback. Requalification is a Security and Platform decision (G-04), with the policy suite (`deploy/policies/check.sh`, including `--cluster`) passing on the fixed template.

**Verify.** No Pod of the profile runs; the policy reports show no new violation; the profile is absent from the Workflow's enabled list until it is requalified.

**Owner.** Security and Platform requalify; the Workflow owner suspends and terminates.
