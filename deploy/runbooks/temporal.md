# Temporal unavailable

**Alerts.** `AnvilKitTemporalDatabaseDown` (no instance of Temporal's PostgreSQL cluster `anvilkit-temporal` is up) and `AnvilKitTemporalRequestsFailing` (the Workflow worker's Temporal client requests fail above the threshold).

**Dashboards.** `anvilkit-workflow` (Temporal client requests and failures, task slots), `anvilkit-control` (operations by lifecycle).

**What it means.** Temporal alone advances business steps (DD-01 (architecture `execution.md`)). While it is down, no workflow task runs, no Update is applied, and the relay of new operations stays pending. Control's durable state is unaffected, and the API answers only what that state proves. Durable timers and the original deadlines continue from history once Temporal is back; nothing is restarted under a new identity.

**Diagnose.**

```sh
$K -n anvilkit-platform get pods -l app.kubernetes.io/instance=anvilkit-temporal -o wide
$K -n anvilkit-data get clusters.postgresql.cnpg.io anvilkit-temporal -o jsonpath='{.status.phase}{"\n"}'
$K -n anvilkit-apps logs deploy/anvilkit-agent-workflow --since=30m | grep -iE 'temporal|poll|unavailable' | tail -30
```

The admin tools Pod of the `anvilkit-temporal` release answers cluster health (`temporal operator cluster health`). Confirm the Deployment and CLI names in the chart version the combination pins before relying on them.

**Remedy.**

1. Restore Temporal's database first (the same CloudNativePG procedure as [control-pg.md](control-pg.md), cluster `anvilkit-temporal`), then the frontend, history and matching services; the workers reconnect by themselves.
2. Do not start, reset or terminate a workflow by hand to "unstick" an operation: a second start or a reset would replay effects under a new run. Workflow IDs are the operation IDs, and Control refuses a duplicate start.
3. After recovery, the Workflow owner checks that each running operation has exactly one open workflow, that its tracked Updates (hold, resume, definition change) were applied or are still pending, and that the original deadlines stand.

**Verify.** `AnvilKitTemporalRequestsFailing` clears; `temporal_worker_task_slots_available` recovers; a held or queued operation advances; Control's relay backlog drains ([control-capacity.md](control-capacity.md), `AnvilKitRelayBacklog`).

**Owner.** Platform for Temporal and its database; the Workflow owner verifies unique starts, tracked Updates and the original clocks.
