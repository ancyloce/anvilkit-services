# Memory evaluation cases (P17, DEVELOPMENT_ONLY)

Facts, decisions and recall cases for Knowledge's confirmed long-term memory
([delivery.md P17](../../../docs/architecture/delivery.md), [DD-07 §5](../../../docs/architecture/knowledge.md#knowledge),
SEC-06 in [security.md](../../../docs/architecture/security.md)). Written by the
implementing agent, not by a business owner: it exercises the evaluator on the
real pipeline and produces comparable figures, but it is **not** the approved
business-labeled corpus G-09 requires, and no quality threshold is derived from it.

| Path | Content |
|---|---|
| `cases.json` | 14 facts in two tenants (actor, project and tenant subjects; model, worker and user origins; confirmed, revoked, deleted, expired, rejected and never-confirmed fates) and 15 recall cases in strata: preference, Chinese, project, tenant isolation (including identical content in two tenants), actor authorization with a positive control, removal, false confirmation and unanswerable |

`TestMemory` (`tests/integration/memory_integration_test.go`) proposes every fact
through `MemoryService` (a model or worker identity proposes; the named user
decides), then runs the adversarial decisions — self-confirmation and
confirmation by any identity that speaks for a model or worker, cross-tenant and
cross-actor decisions, stale revisions, re-confirmation after revocation,
duplicate confirmation and concurrent decision races — and counts every one that
succeeds as a **false confirmation**. After the projections settle through the
real lane (outbox, forwarder, JetStream, owner relay, BullMQ, the Background
Worker's `memory-project` handler, the PostgresStore and Qdrant) it checks that
no removed fact left content in the Store or a point in any generation
(**stale revival**), that every live fact has exactly one point per generation
(**duplicate projection**), and runs every recall case three times as its
reader, counting any returned fact of another tenant or one the reader may not
read as **tenant contamination / unauthorized disclosure**. Reported, never
asserted: hit rate of the expected facts, no-answer behavior, recall latency,
projection lag and deletion purge time. The report is written under
`outputs/memory-eval/` (ignored by Git).

```sh
sh deploy/dev/up.sh && . .local/dev/env.sh
(cd services/agent/knowledge && pnpm run build) && (cd services/agent/background-worker && pnpm run build)
cd tests/integration && go test -tags integration -run TestMemory -count=1 -timeout 60m .
```
