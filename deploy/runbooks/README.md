# Runbooks

Operator procedures for the alerts of the `anvilkit-observability` chart ([rules](../qualification/platform/observability/templates/rules.yaml)) and the incident entry points of platform operations (architecture `platform.md`). Every alert carries `runbook_url` to one of these files; every file names the alerts that send an operator here, the dashboards to read, the exact commands and how to verify the outcome.

Conventions used by every runbook:

- `K="kubectl --kubeconfig <environment kubeconfig>"`; namespaces are those of DD-10 §2 (architecture `platform.md`) (`anvilkit-apps`, `anvilkit-data`, `anvilkit-platform`, `anvilkit-observability`).
- The business database's current primary: `PRIMARY=$($K -n anvilkit-data get pods -l cnpg.io/cluster=anvilkit-business,cnpg.io/instanceRole=primary -o name)`; the diagnostic queries below are read-only and run as the instance's local superuser.
- Dashboards are the Grafana dashboards with uid `anvilkit-<name>` (`anvilkit-overview`, `anvilkit-control`, `anvilkit-model-proxy`, `anvilkit-mcp`, `anvilkit-knowledge`, `anvilkit-background-worker`, `anvilkit-api`, `anvilkit-workflow`, `anvilkit-inference`).
- PromQL is run in Grafana's Explore view or with `$K -n anvilkit-observability get --raw "/api/v1/namespaces/anvilkit-observability/services/http:anvilkit-metrics-prometheus:9090/proxy/api/v1/query?query=<urlencoded>"`.
- Never turn an UNKNOWN outcome into success or failure without the evidence the procedure names; never resend under a new identity ([AGENTS.md §6](../../AGENTS.md)).
- Thresholds in the alerts are DEVELOPMENT_ONLY until ENV-05/ENV-09 supply them; routes are development receivers until ENV-10 names on-call routes.
- These files are published with the deployment files (`deploy/runbooks/`, the alerts' `runbookBase`). They hold procedures only: no credential, token, address of a real environment or record of a run. References such as "DD-02 (architecture `execution.md`)" name the project's design documents, which are kept outside this repository; a procedure never depends on reading them.
- `tools/check-docs.py` fails when an alert's runbook file is missing here, when the table below does not name an alert under its runbook, when a runbook links outside the repository's committed files, or when one carries a private path or address.

| Runbook | Alerts |
| --- | --- |
| [unresolved-effect-age.md](unresolved-effect-age.md) | AnvilKitUnresolvedObligationAgeHigh, AnvilKitModelProxyUnknownOutcomes, AnvilKitMCPUnknownCalls |
| [control-capacity.md](control-capacity.md) | AnvilKitControlCapacityExhausted, AnvilKitControlPoolSaturated, AnvilKitRelayBacklog, AnvilKitAPILatencyP95High, AnvilKitExecutionPoolSaturated |
| [recovery-epoch.md](recovery-epoch.md) | AnvilKitBackupStale; the failure-domain/PITR incident |
| [proxy-fleet-restart.md](proxy-fleet-restart.md) | AnvilKitModelProxySenderLost, AnvilKitModelProxyPendingObservations |
| [mcp-restriction.md](mcp-restriction.md) | AnvilKitMCPRecoveryRestricted |
| [knowledge-rebuild.md](knowledge-rebuild.md) | AnvilKitKnowledgeIndexFailures, AnvilKitKnowledgeProjectionLag, AnvilKitIndexStale |
| [async-backlog.md](async-backlog.md) | AnvilKitOutboxOldestAgeHigh, AnvilKitQueueOldestAgeHigh, AnvilKitDurableRequestDivergence |
| [secret-rotation.md](secret-rotation.md) | AnvilKitConfigGenerationRejected; planned rotation |
| [rollback.md](rollback.md) | AnvilKitAPIErrorRatioHigh, AnvilKitTargetDown, AnvilKitForcedStop, AnvilKitSyntheticProbeFailing, AnvilKitSyntheticProbeStale |
| [startup-failure.md](startup-failure.md) | AnvilKitStartupFailing, AnvilKitAbnormalRestart; also the Compose placement's evaluation of both |
| [control-pg.md](control-pg.md) | AnvilKitBusinessDatabaseDown, AnvilKitEventRetentionFailing; the Control/PG incident |
| [temporal.md](temporal.md) | AnvilKitTemporalDatabaseDown, AnvilKitTemporalRequestsFailing; the Temporal incident |
| [source-memory-revoked.md](source-memory-revoked.md) | AnvilKitMemoryRemovalUnreconciled; the source/memory revocation incident |
| [mcp-revocation-ssrf.md](mcp-revocation-ssrf.md) | AnvilKitMCPRevocationSlow, AnvilKitMCPEgressRefused; the MCP revocation/SSRF incident |
| [partial-publication.md](partial-publication.md) | AnvilKitReleasePartiallyPublished; the partial-publication incident |
| [isolation-failure.md](isolation-failure.md) | AnvilKitIsolationAdmissionRefused; the isolation-failure incident |
