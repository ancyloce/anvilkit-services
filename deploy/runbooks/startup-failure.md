# A service does not start, or restarts

**Alerts.** `AnvilKitStartupFailing` (critical): a service container has waited in a failed-start state for 5 minutes (`CrashLoopBackOff`, `CreateContainerConfigError`, `CreateContainerError`, `RunContainerError`, `ErrImagePull`, `ImagePullBackOff`, `InvalidImageName`). `AnvilKitAbnormalRestart` (warning): a container was restarted in the last 15 minutes. Related: `AnvilKitTargetDown`, and `AnvilKitConfigGenerationRejected` for Knowledge and MCP, which keep serving on the previous configuration generation when a rotated input is rejected.

**Dashboards.** `anvilkit-overview` (Scrape targets up, Alerts firing), the dashboard of the affected service.

**What it means.** API, Control, Workflow, Model Proxy and Inference load their configuration once, at start, and validate all of it before they serve. A configuration, secret or certificate they reject is therefore a process that exits before it becomes ready, not a counter: these services export `config_generation` 1 and no rejection counter. The signal is the container's state. A failing start during a rollout leaves the previous Pods serving (the rollout stops at its first unready Pod); a failing start after a node or Pod loss reduces capacity. A single restart of a running container means its process exited or was killed (a failed start that then succeeded, an out-of-memory kill, a failed liveness probe); in-flight work of that replica was cut, and the callers' retries and Temporal's Activity retries carry it.

**Diagnose (Kubernetes).**

```sh
$K -n anvilkit-apps get pods -o wide | grep -vE 'Running|Completed'
$K -n anvilkit-apps describe pod <pod> | sed -n '/Containers:/,/Conditions:/p'      # State, Last State, Reason, Exit Code, Restart Count
$K -n anvilkit-apps logs <pod> -c <container> --previous --tail=50                  # the run that ended
$K -n anvilkit-apps get events --field-selector involvedObject.name=<pod> --sort-by=.lastTimestamp | tail -20
```

Read the last lines of the ended run. A rejected configuration names the key and the rule it broke, never the value (for example `retention.operation_events 0s outside [24h, 87600h]`, or `retention.database.url is required outside development`). `CreateContainerConfigError` names a missing Secret or key. `ErrImagePull` and `ImagePullBackOff` name the image reference. Exit code 137 with reason `OOMKilled` is the memory limit.

**Diagnose (Compose placement).** The same two alerts are evaluated from Docker's state by `deploy/compose/alerts.py`, by hand, from `verify.sh`, or by the `anvilkit-alerts` timer:

```sh
python3 deploy/compose/alerts.py
journalctl -t anvilkit-alerts -p warning --since -1h          # when the timer is installed
docker ps -a --filter label=com.docker.compose.project=anvilkit --format '{{.Names}}\t{{.Status}}'
docker logs --tail 50 <container>
```

**Remedy.**

1. A rejected configuration, secret or certificate: correct the input the log names. In Kubernetes that is the environment combination or the secret platform ([secret-rotation.md](secret-rotation.md)); on Compose it is the file under `deploy/compose/config/` or the restricted secret file. Never start a service with validation weakened or with `development.enabled` switched on to get past a refusal.
2. A failed start that began with a promotion: roll back ([rollback.md](rollback.md)). The previous release's images run against the newer schema.
3. An image that cannot be pulled or admitted: check the digest against the signed release manifest; a digest outside the manifest is never deployed.
4. An out-of-memory kill or a failed liveness probe: read the service's dashboard for the load that preceded it before changing a limit; a limit is an environment input.
5. After a restart of Model Proxy, Workflow or Control, check what the cut replica held: [proxy-fleet-restart.md](proxy-fleet-restart.md) and [unresolved-effect-age.md](unresolved-effect-age.md). Never turn an UNKNOWN into a success or failure without its evidence.

**Verify.** Kubernetes: every Pod of the service is Ready, `kube_pod_container_status_waiting_reason` has no failed-start reason for it, and `increase(kube_pod_container_status_restarts_total[15m])` is back to zero. Compose: `python3 deploy/compose/alerts.py` exits 0 and `verify.sh` passes its alerts section.

**Owner.** The service's owner corrects a rejected input; the environment owner supplies secrets, certificates and limits.
