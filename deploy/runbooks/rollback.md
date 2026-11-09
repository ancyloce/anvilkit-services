# Rollback

**Alerts.** `AnvilKitAPIErrorRatioHigh`, `AnvilKitTargetDown`, `AnvilKitForcedStop`, `AnvilKitSyntheticProbeFailing` and `AnvilKitSyntheticProbeStale` (the recurring end-to-end probes no longer succeed) — when they start with a promotion (a new environment chart version). A container that does not start or restarts has its own runbook ([startup-failure.md](startup-failure.md)).

**Forced stops.** A replica whose drain outlives its bound closes what is still open and completes the stop normally; the API exits 0 after it. Each service records it in its stop log line (`"forced":true`; the API also logs `api drain bound reached: open connections closed`) and sets its `anvilkit_<service>_shutdown_forced` gauge. The gauge exists only until the stopping process exits, so `AnvilKitForcedStop` can miss a forced stop between two scrapes: the log line is the durable record. Read it with `$K -n anvilkit-apps logs <pod> --previous | grep '"forced":true'` or in the log store; the Compose placement evaluates the same line (`deploy/compose/alerts.py`). Forced stops during a promotion mean requests or sends were cut at the drain bound: check the bound against the longest request before promoting further.

**Dashboards.** `anvilkit-overview` (Public API 5xx ratio, Scrape targets up, Alerts firing), the dashboard of the failing service.

**What it means.** Promotion is a reviewed change of the environment combination's `release` field; rollback is the revert of that change, which publishes the previous release again as a new environment chart version ([deploy/gitops/README.md](../gitops/README.md#release-promotion-and-rollback-p32)). Database migrations are expand/contract: the previous images run against the newer schema (each service checks only its minimum version), so a rollback never runs a down migration.

**Decide.**

```sh
$K -n anvilkit-platform get applications.argoproj.io -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,REV:.spec.source.targetRevision
$K -n anvilkit-apps get pods -o wide
$K -n anvilkit-apps logs deploy/<service> -c <container> --since=15m | grep -E '"level":"ERROR"|request failed|schema'
```

Roll back when the failure follows the promotion and the previous release was healthy; fix forward when the fault is in the environment (a dependency, a secret).

**Roll back.**

1. Revert the promotion's change of `deploy/gitops/environments/<env>.yaml` (`git revert <commit>`; the user's git operation, reviewed like the promotion) and merge it: `.github/workflows/promote.yml` publishes the previous release's manifest as a new environment chart version.
2. Point the root Application at that version: `$K apply -f <root-application.yaml from the promote run's artifact>`.
3. With a declared rollback window Argo CD syncs automatically; otherwise sync on command (`argocd app sync anvilkit-<env>` or the UI). A sync that waits on a resource the new source repairs is terminated as `deploy/qualification/bootstrap.py`'s `fix_forward` does.
4. For the qualification environment, `deploy/qualification/rollback-drill.py --from <N> --to <N+1>` performs and measures both directions.

**Verify.** Every Application `Synced`/`Healthy`; the `anvilkit-apps` Deployments run exactly the previous release's digests (`$K -n anvilkit-apps get deploy -o jsonpath='{range .items[*]}{.metadata.name} {.spec.template.spec.containers[*].image}{"\n"}{end}'`); the probe LocalCheck is green; `AnvilKitAPIErrorRatioHigh` resolves.

**Owner.** Release (promotion and rollback), the failing service's owner for the cause.
