# Async backlog (outbox, queues, durable requests)

**Alerts.** `AnvilKitOutboxOldestAgeHigh` (an owner's oldest unforwarded outbox row), `AnvilKitQueueOldestAgeHigh` (the oldest waiting BullMQ job of a queue), `AnvilKitDurableRequestDivergence` (overdue durable retries or relay inbox gaps).

**Dashboards.** `anvilkit-overview` (Outbox oldest, Queues oldest), `anvilkit-control`/`anvilkit-knowledge`/`anvilkit-mcp` (outbox panels), `anvilkit-background-worker` (Jobs, Stalled, Relay inbox duplicates and gaps).

**What it means.** Outbox rows are written in the owner's transaction and forwarded to NATS JetStream; consumers dedupe by inbox; the BullMQ queue is rebuilt from durable requests, never the other way round (DD-09 (architecture `platform.md`)). A cursor is never skipped.

**Diagnose.**

```sh
$K -n anvilkit-data get pods -l app.kubernetes.io/name=nats
$K -n anvilkit-data exec anvilkit-nats-0 -c nats -- nats-server --version >/dev/null && echo nats-up
$K -n anvilkit-apps logs deploy/anvilkit-agent-knowledge -c forwarder --since=30m | tail -20
$K -n anvilkit-apps logs deploy/anvilkit-agent-control -c control --since=30m | grep -i forward | tail -20
# Long transactions that hold the outbox back:
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -Atc \
  "SELECT datname, usename, now() - xact_start FROM pg_stat_activity WHERE xact_start < now() - interval '1 minute' ORDER BY xact_start"
$K -n anvilkit-data get pods -l app.kubernetes.io/name=valkey
```

**Remedy.**

1. NATS unavailable or a stream full: restore NATS (`$K -n anvilkit-data rollout status statefulset/anvilkit-nats`); the forwarders resume from their cursor.
2. A forwarder failing on one row (poison row): its log names the row's event id; report it to the owner — the row is never deleted or skipped by hand.
3. Queue backlog with healthy Valkey: scale the worker within its concurrency bound (`$K -n anvilkit-apps scale deploy/anvilkit-agent-background-worker --replicas=<n>`).
4. Valkey failover lost queue writes: the relay reconstructs them from the durable requests under the same id and generation (`anvilkit_background_relay_reconstructed_total` rises); no manual enqueue.

**Verify.** `anvilkit:outbox_oldest_unforwarded_seconds` and `anvilkit:queue_oldest_waiting_seconds` below their bounds; `anvilkit_(knowledge|mcp)_background_overdue_retries` 0; `increase(anvilkit_background_relay_inbox_gaps_total[15m]) == 0`.

**Owner.** The domain owner of the outbox (Control, Knowledge, MCP); Background Worker for the queues; Platform for NATS/Valkey.
