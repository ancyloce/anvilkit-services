# Control or its business database unavailable

**Alerts.** `AnvilKitBusinessDatabaseDown`: no instance of the business PostgreSQL cluster `anvilkit-business` reports up, or its metrics vanished. `AnvilKitEventRetentionFailing`: Control's operation-event retention was refused or failed twice within 3 hours (`anvilkit_control_event_retention_failures_total`, reason `identity` when its own pool does not connect as the retention role `anvilkit_control_retention`, reason `delete` when the deletion failed); events past the period are kept until it passes again, nothing is lost. Check the retention role's credential ([secret-rotation.md](secret-rotation.md)) and its grants before anything else; never point the retention at the application role. Related: `AnvilKitControlPoolSaturated`, and `AnvilKitTargetDown` for the Control Pods themselves.

**Dashboards.** `anvilkit-control` (RPC errors by code, DB pool held and empty acquires, unresolved obligations), `anvilkit-overview`.

**What it means.** Control is the authority for admission, permits, costs and effects (DD-02 (architecture `execution.md`)). Without its database it can neither admit a send nor record an outcome. New sends stop by themselves, because no claim can be made. Workflow Activities retry inside their outage windows, and senders that already hold a permit keep their bounded observation and query. Nothing is lost while the database is down, but nothing that depends on a new permit advances.

**Diagnose.**

```sh
$K -n anvilkit-data get clusters.postgresql.cnpg.io anvilkit-business -o jsonpath='{.status.phase}{"\n"}{.status.currentPrimary}{"\n"}'
$K -n anvilkit-data get pods -l cnpg.io/cluster=anvilkit-business -o wide
$K -n anvilkit-data logs "$PRIMARY" -c postgres --since=30m | tail -50
$K -n anvilkit-apps logs deploy/anvilkit-agent-control --since=30m | grep -iE 'database|pool|connect' | tail -30
```

A cluster whose phase is not healthy with no current primary is a database incident. A healthy cluster that Control cannot reach is a network or credential issue: check the NetworkPolicy of `anvilkit-data` and the CSI-synced database Secret ([secret-rotation.md](secret-rotation.md)).

**Remedy.**

1. The database owner restores the cluster: a CloudNativePG failover, or a restarted instance. Do not restart Control repeatedly; its pool reconnects once the primary answers.
2. If data was lost or a restore from backup is needed, stop here and follow [recovery-epoch.md](recovery-epoch.md): a restored database opens under a new recovery epoch with admission closed.
3. Without data loss, once Control answers again, check the obligations whose outcome may have arrived during the outage ([unresolved-effect-age.md](unresolved-effect-age.md)). Never turn an UNKNOWN into a success or failure without its evidence.

**Verify.** `cnpg_collector_up{cluster="anvilkit-business"}` is back to at least one instance; Control's readiness passes; `anvilkit_control_unresolved_obligation_reads_failed_total` stops increasing; new operations are admitted.

**Owner.** The database owner restores consistency; Control's owner verifies the epoch and the ledger before admission reopens.
