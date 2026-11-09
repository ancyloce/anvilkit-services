# Knowledge rebuild

**Alerts.** `AnvilKitKnowledgeIndexFailures` (index batches failed or quarantined), `AnvilKitKnowledgeProjectionLag` (memory projections not accepted within the bound, or failed), `AnvilKitIndexStale` (`anvilkit_knowledge_index_freshness_seconds`: an accepted revision has waited longer than the bound for the active index generation).

**Dashboards.** `anvilkit-knowledge` (Index batches by outcome, Memory projections, Projection lag, Background requests by state), `anvilkit-background-worker` (Oldest waiting job, Jobs).

**What it means.** Qdrant points and the PostgresStore projections are rebuildable copies; the authorities are the accepted index generation and the confirmed `MemoryFact` rows (DD-07 (architecture `knowledge.md`)). Retrieval and recall recheck authorization against the authority, so a stale projection never discloses; a missing one makes results incomplete.

**Diagnose.**

```sh
$K -n anvilkit-apps logs deploy/anvilkit-agent-knowledge -c knowledge --since=1h | grep -E 'index|quarantin|projection'
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -d anvilkit_knowledge -Atc \
  "SELECT state, count(*), min(updated_at) FROM memory_projections GROUP BY state"
$K -n anvilkit-data get pods -l app.kubernetes.io/name=qdrant
```

**Remedy.**

1. Transient Qdrant or Inference loss: index batches are retried with backoff and quarantined after `index.max_attempts`; restore the dependency first (`$K -n anvilkit-data get pods -l app.kubernetes.io/name=qdrant`, `$K -n anvilkit-apps get pods -l app.kubernetes.io/name=anvilkit-agent-inference`).
2. Rewrite the document points of the accepted generation (new task generations the Background Worker completes; idempotent):
   `$K -n anvilkit-apps exec deploy/anvilkit-agent-knowledge -c knowledge -- node /anvilkit/knowledge/dist/knowledgectl.js rebuild documents`
   (`--generation N` for a specific generation). Output is one JSON line with the scheduled tasks.
3. Memory projections: the reconciliation re-drives failed rows from the authoritative fact (a projection never writes what its task carried) after `memory.retry_delay`, but only while the row's epoch is below `memory.max_projection_epochs` (default 5); a row at the bound stays `failed`, and `anvilkit_knowledge_memory_projections_failed` keeps the alert firing. Once the cause is restored (step 1: a vector target needs Qdrant and Inference), request the target again from the authoritative facts under a new epoch (`0` is the PostgresStore, `g` the vector collection of generation `g`; revoked, expired and deleted facts become tombstones, never revived):
   `$K -n anvilkit-apps exec deploy/anvilkit-agent-knowledge -c knowledge -- node /anvilkit/knowledge/dist/memoryctl.js rebuild --target <0 or g>`
   Find the target of the failed rows with `SELECT target, count(*) FROM memory_projections WHERE state = 'failed' GROUP BY target` (database `anvilkit_knowledge`). A backlog that does not drain points at the Store or Qdrant — step 1.

**Verify.** `increase(anvilkit_knowledge_index_batches_total{outcome=~"failed|quarantined"}[30m]) == 0`; `anvilkit_knowledge_index_points_missing` 0; `anvilkit_knowledge_memory_projection_lag_seconds` below the bound and `anvilkit_knowledge_memory_projections_failed` 0; a retrieval case of the evaluation registry (`IT-RETRIEVAL`) passes.

**Owner.** Knowledge.
