# Control capacity

**Alerts.** `AnvilKitControlCapacityExhausted` (RPCs refused with CAPACITY_EXHAUSTED, by pool), `AnvilKitControlPoolSaturated` (a gRPC pool or the database pool above the bound), `AnvilKitRelayBacklog` (a relay lane's pending intents sustained), `AnvilKitAPILatencyP95High` (public latency, often Control-bound). `AnvilKitExecutionPoolSaturated`: every permit of a database-backed execution pool has been held for the bound, so new work waits for a permit (`anvilkit_control_execution_pool_active_permits` against `anvilkit_control_execution_pool_limit`, and the oldest permit's age).

**Dashboards.** `anvilkit-control` (gRPC capacity pools, CAPACITY_EXHAUSTED refusals, Database pool, Relay backlog, RPC p95 by method), `anvilkit-api` (p95 by route), `anvilkit-overview`.

**What it means.** Control bounds its work by three pools (P1.4 (architecture `delivery.md`)): `control` (operations and queries, with a reserve for SubmitCommand), `execution` (dispatch, effects, artifacts, Jobs) and `streams` (event streams, also bounded per tenant). A refusal is retryable for the caller and is not a budget decision; sustained refusals mean the replicas or the pool bounds are below the load.

**Diagnose.**

```sh
$K -n anvilkit-apps get deploy anvilkit-agent-control -o jsonpath='{.status.readyReplicas}/{.spec.replicas}{"\n"}'
$K -n anvilkit-apps get hpa anvilkit-agent-control
$K -n anvilkit-apps top pods -l app.kubernetes.io/name=anvilkit-agent-control
# The configured bounds of the running generation:
$K -n anvilkit-apps get configmap anvilkit-agent-control -o jsonpath='{.data.config\.yaml}' | grep -A8 '^grpc:'
# Database side: active and waiting sessions of the Control role
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -d anvilkit_control -Atc \
  "SELECT state, wait_event_type, count(*) FROM pg_stat_activity WHERE usename = 'anvilkit_control_app' GROUP BY 1,2"
```

**Remedy.**

1. Streams pool saturated by viewers: the API caps streams per principal (`sse.max_streams_per_principal`); check `anvilkit_api_requests_total{route=~".*/events"}` for a client reconnecting in a loop and throttle it at the edge.
2. Execution or control pool saturated with healthy pods: scale out within the lock's bound (connection budget, `deploy/gitops/lock.yaml`):
   `$K -n anvilkit-apps scale deploy/anvilkit-agent-control --replicas=<n ≤ HPA max>` (temporary; the reviewed change is the combination's replica value, see [rollback.md](rollback.md) for publishing it).
3. Database pool saturated: look for long transactions (`pg_stat_activity` above) before raising `database.max_conns`; Control never holds network I/O under a lock, so a long transaction is a defect to report.
4. Relay backlog: a lane holds intents it could not deliver (Temporal unavailable or an Update waiting); check the Workflow and Temporal frontends (`$K -n anvilkit-platform get pods -l app.kubernetes.io/name=temporal`) and the Control logs for `relay`.

**Verify.** `rate(anvilkit_control_capacity_refusals_total[5m])` returns to 0; `anvilkit:control_grpc_pool_utilization` below the bound; `max by (lane) (anvilkit_control_relay_backlog)` drains; the API p95 recovers.

**Owner.** Control; Platform for replicas and the connection budget.
