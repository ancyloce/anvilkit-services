# Recovery epoch procedure

**Alerts.** `AnvilKitBackupStale` (a store's newest successful backup is older than its bound, or it never succeeded) and the incident "failure-domain loss or database point-in-time recovery" of platform operations (architecture `platform.md`). This is the procedure the recovery gates (architecture `platform.md`) define; P3.7's end-to-end drill executes it.

**Dashboards.** `anvilkit-overview` (Newest backup age, Unresolved obligations, Alerts firing), `anvilkit-control`.

## Backups stale

```sh
$K get cronjobs -A -l app.kubernetes.io/part-of=anvilkit-backups
$K -n anvilkit-data get scheduledbackups,backups -l cnpg.io/cluster=anvilkit-business --sort-by=.metadata.creationTimestamp
$K -n <namespace> logs job/$($K -n <namespace> get jobs -l anvilkit.io/backup=<store> --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)
```

Run the store's backup once by hand (`$K -n <namespace> create job --from=cronjob/anvilkit-backup-<store> anvilkit-backup-<store>-manual`), fix what failed (DR target reachable, credentials current: [secret-rotation.md](secret-rotation.md)), and verify the alert resolves (`anvilkit:backup_age_seconds{cronjob="anvilkit-backup-<store>"}` below the bound). A restore drill of that store follows the next successful backup (`.local/verification-venv/bin/python deploy/qualification/restore-drills.py --run <RUN> <store>`).

## Failure-domain loss or PITR

Order matters; do not reopen anything before the last step.

1. **Fence.** Close new-system admission for the affected scope and stop the old exits: scale the senders that could still act on old authority to zero (`$K -n anvilkit-apps scale deploy/anvilkit-agent-model-proxy deploy/anvilkit-agent-mcp --replicas=0`), and keep Control **closed**: Control refuses new admission while a recovery run of the scope is open (step 4).
2. **Restore** the database to a named restore point (never "latest" when the failure is a bad write): CloudNativePG recovery from the off-cluster backup with `bootstrap.recovery.recoveryTarget.targetName: <restore point>` — the manifests `restore-drills.py` applies are the template (`.local/verification-venv/bin/python deploy/qualification/restore-drills.py --run <RUN> postgres-business` restores into a sibling cluster and records them in the run's evidence directory, which the tool prints); the object store from its replica; Temporal's database likewise. Verify revisions and integrity: `SELECT max(version_id) FROM goose_db_version WHERE is_applied` per domain is at least each binary's minimum (a lower one keeps the service not ready).
3. **Start Control** against the restored database. It serves, but admission stays closed for a scope with an open recovery run.
4. **Begin the recovery run** (a new epoch fences every old execution identity) through the public API (API-17) with an operator's token: Control decides the role from the token's verified roles (`operator` in the tenant; `platform_operator` and `"allTenants": true` for every tenant). The scope is the token's tenant, never a request field:
   ```sh
   curl -sS -X POST -H "Authorization: Bearer $OPERATOR_TOKEN" -H 'Content-Type: application/json' https://<api>/api/v1/recoveries \
     -d '{"commandId": "recover-<incident>", "windowStart": "<restore point - clock uncertainty>", "windowEnd": "<now>", "clockUncertaintySeconds": 5, "reason": "<incident reference>"}'
   ```
   A new run answers 201 in phase `fenced`; repeating the same command answers 200 with the same run; a second run while one holds the scope answers 409 `STALE_EXECUTION` (RECOVERY_IN_PROGRESS). The `RecoveryWorkflow` enumerates the five independent inventories (intake, Job, model, business write, tool) for the window, compares them with the restored identities and reconciles each finding under its original identity.
5. **Follow the run**: `GET /api/v1/recoveries/<runId>` until phase `reopened`, and `GET /api/v1/recoveries/<runId>/findings?status=unresolved` (follow `nextCursor` until `complete`) for what keeps it restricted; findings that no query resolves stay unsettled and keep the scope closed. Only an operator of the run's scope sees it (anyone else gets 404). A disposition needs evidence — `POST /api/v1/recoveries/<runId>/dispositions` with `commandId`, `class`, `obligationId`, `decision`, `evidenceRef`, `evidenceDigest` and the run's `recoveryEpoch`; evidence that does not verify is refused with `EFFECT_UNCERTAIN` and nothing is recorded — see [unresolved-effect-age.md](unresolved-effect-age.md).
6. **Rebuild** queues, indexes and projections (`knowledge-rebuild.md`, the background worker reconstructs lost queue entries from durable requests), then **probe** (the `anvilkit-probes` LocalCheck) and only then scale the senders back (`--replicas=<previous>`); admission reopens by scope when the run reaches `REOPENED`.

**Verify.** `GET /api/v1/recoveries/<runId>` reports `reopened` with `unsettledFindings` `0`; `anvilkit:unresolved_obligation_oldest_seconds` is 0 or explained by dispositions; the probe LocalCheck is green; no duplicate send or publication (upstream receipts per call identity); RTO/RPO recorded against ENV-09.

**Owner.** Platform (restore), Control (epoch, inventory), Security for credential revocation of the failed domain.
