# Source or memory access revoked

**Alerts.** `AnvilKitMemoryRemovalUnreconciled`: Knowledge's memory recall is closed (`anvilkit_knowledge_memory_removals_reconciled` 0) because a removal or revocation is not reconciled with its inventory. Related: `AnvilKitKnowledgeProjectionLag` ([knowledge-rebuild.md](knowledge-rebuild.md)).

**Dashboards.** `anvilkit-knowledge` (memory removals by outcome, projection backlog and lag, index generation).

**What it means.** Knowledge owns the source ACL and the confirmed memory (DD-07 (architecture `knowledge.md`)). A revoked source or a removed memory fact must stop reaching any retrieval and any model send. Until the removal inventory is reconciled, memory recall stays closed rather than risk serving a removed fact. Retrieval rechecks the ACL before text is read and again before it is returned, so a revoked source is refused on the next search.

**Diagnose.**

```sh
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -d anvilkit_knowledge -Atc \
  "SELECT tenant_id, fact_id, decision, state, recorded_at FROM memory_removals WHERE state = 'pending' ORDER BY recorded_at"
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -d anvilkit_knowledge -Atc \
  "SELECT tenant_id, source_id, decision, acl_revision, recorded_at FROM source_removals ORDER BY recorded_at DESC LIMIT 20"
$K -n anvilkit-apps logs deploy/anvilkit-agent-knowledge -c knowledge --since=1h | grep -iE 'removal|revocation|inventory' | tail -30
```

`anvilkit_knowledge_memory_removals_total{outcome="uncertain"}` or `{outcome="conflict"}` names a removal whose inventory write is not confirmed.

**Remedy.**

1. Keep the removal: never restore a removed fact or source by hand, and never reopen recall by editing the database.
2. An `uncertain` or `conflict` inventory outcome is resolved with the removal inventory's owner (its endpoint, ENV-09), under the removal's original command; the reconciliation then re-drives it.
3. Stop affected projections and clean caches and indexes by the documented paths ([knowledge-rebuild.md](knowledge-rebuild.md)). In-flight output that already carried the revoked content is recorded for audit with the minimal evidence the data classification allows (architecture `security.md`).

**Verify.** `anvilkit_knowledge_memory_removals_reconciled` returns to 1; no `pending` row older than the reconciliation interval remains; a search as the affected principal no longer returns the revoked source.

**Owner.** Knowledge; the data owner (ENV-09) for the removal inventory.
