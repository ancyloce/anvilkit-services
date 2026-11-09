# Model Proxy fleet restart

**Alerts.** `AnvilKitModelProxySenderLost` (sends reclaimed as UNKNOWN/SENDER_LOST: a sender stopped mid-send) and `AnvilKitModelProxyPendingObservations` (usage observations not accepted by Control for the bound).

**Dashboards.** `anvilkit-model-proxy` (Call outcomes, Pending observations, Sweep actions, Sweep duration), `anvilkit-control` (CAPACITY_EXHAUSTED refusals: a full Control pool refuses observations).

**What it means.** Every replica shares one call store; a replica that dies or is killed with a send in flight leaves a `sending` record that the sweep reclaims as `unknown/SENDER_LOST` after the deadline and grace. Pending observations are usage the Proxy must deliver to Control; they are retried by the sweep and never dropped.

**Diagnose.**

```sh
$K -n anvilkit-apps get pods -l app.kubernetes.io/name=anvilkit-agent-model-proxy -o wide
$K -n anvilkit-apps get events --field-selector involvedObject.kind=Pod --sort-by=.lastTimestamp | grep model-proxy | tail -20
$K -n anvilkit-apps logs deploy/anvilkit-agent-model-proxy -c model-proxy --since=1h | grep -E 'sweep pass|send reclaimed|observation'
```

**Planned restart (rollout) without new SENDER_LOST.** A Proxy drains in-flight sends up to its shutdown bound before it exits; the chart's grace period covers that bound.

1. Check that the longest route bound fits the drain: every route's send timeout in the running configuration (`$K -n anvilkit-apps get configmap anvilkit-agent-model-proxy -o jsonpath='{.data.config\.yaml}' | grep -n -i timeout`) must be below the Pod's `terminationGracePeriodSeconds` (`$K -n anvilkit-apps get deploy anvilkit-agent-model-proxy -o jsonpath='{.spec.template.spec.terminationGracePeriodSeconds}'`); otherwise a rollout cuts sends that are still running.
2. `$K -n anvilkit-apps rollout restart deploy/anvilkit-agent-model-proxy && $K -n anvilkit-apps rollout status deploy/anvilkit-agent-model-proxy --timeout=15m`
3. The PodDisruptionBudget keeps one replica serving; never delete Pods with `--grace-period=0`.

**After an unplanned loss.** Do not resend anything: the reclaimed calls are UNKNOWN under their original dispatch identities and follow [unresolved-effect-age.md](unresolved-effect-age.md). Pending observations: confirm Control is reachable and not refusing (`anvilkit_control_capacity_refusals_total`), then let the sweep resubmit (`sweep_entries_total{action="resubmitted"}` rises, `pending_observations` falls).

**Verify.** `increase(anvilkit_model_proxy_call_outcomes_total{error_code="SENDER_LOST"}[15m]) == 0` after the rollout; `anvilkit_model_proxy_pending_observations` returns to 0; every replica Ready.

**Owner.** Model Proxy; Control for observation acceptance.
