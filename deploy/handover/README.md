# Handover and retirement (P24, DEVELOPMENT_ONLY rehearsal)

The P24 tools of the replacement release: the P24a rehearsal that supplies G-13's clean-install, closure and transfer evidence to P23, and the P24b procedure that hands the legacy `anvilkit-local` scope over and retires it. Owning sections: [handover and recovery](../../docs/architecture/delivery.md#handover-and-recovery), [complete replacement](../../docs/architecture/delivery.md#definition-of-complete-replacement), [P24](../../docs/architecture/delivery.md#implementation-plan).

| Path | Purpose |
| --- | --- |
| `rehearse.py` | P24a. `inputs` freezes the release candidate, P22's inventory, P23's record, the legacy environment, the legacy contract and the Studio page store. `install` installs the release from zero on the qualification environment (`deploy/qualification`): its previous state moves under `.local/qualification-archive/` (never deleted), its cluster and DR store are recreated empty. `install-check` proves the start from zero (initdb clusters, migrations 00001..N inside the install window, a new Temporal, no legacy volume, mount or name, release images only, converged and probed). `closure` proves the new build closure (module closure test, no legacy source file in the new trees, every file, Go binary and SBOM of every release image against the legacy identities, the legacy images as positive controls). `dto` proves old-DTO separation and the route inventory against the installed API. `legacy` inventories the legacy records and obligations read-only on a disposable copy and decides, by stated rules, whether a transfer applies. `withdraw` rehearses the withdrawal before business writes and the admission close and reconciliation after them. `record` binds all of it into `handover-rehearsal.{json,md}`, which `deploy/qualification/record.py` reads for G-13. |
| `handover.py` | P24b. `plan` (read-only) checks the gates and every retirement target against the observed state. `rehearse` adds the final capture on copies (pg_dump of a disposable copy, restored into a second disposable server, every table's count and digest compared with the inventory), the final reconciliation, the transfer checks and the final checks. `execute` is the live handover and retirement; it refuses unless P23 is qualified, P24a is rehearsed, the scope is the agreed one and an explicit execution authorization is named. |
| `retirement.yaml` | The explicit retirement inventory: every legacy executable, worker, credential, image, container, volume, active source-build dependency and Git/runtime arrangement, with its consumers, decision (`retire`, `preserve`, `none`), what is preserved and how, the action and the authorization it needs. |

```sh
PY=.local/verification-venv/bin/python
REL=20261002T162353Z   # a release of tools/release-artifacts.py published by deploy/gitops/publish.py
RUN=$(date -u +%Y%m%dT%H%M%SZ)
$PY deploy/handover/rehearse.py inputs  --run $RUN --release $REL
$PY deploy/handover/rehearse.py legacy  --run $RUN
$PY deploy/handover/rehearse.py closure --run $RUN --release $REL
$PY deploy/handover/rehearse.py install --run $RUN --release $REL --reset-qualification-environment
$PY deploy/handover/rehearse.py install-check --run $RUN --release $REL
$PY deploy/handover/rehearse.py dto      --run $RUN
$PY deploy/handover/rehearse.py withdraw --run $RUN --release $REL
$PY deploy/handover/rehearse.py record   --run $RUN --release $REL
$PY deploy/handover/handover.py plan     --run $RUN
$PY deploy/handover/handover.py rehearse --run $RUN
```

`install` needs `fs.inotify.max_user_instances >= 512` (reset by every reboot) and the development foundation's registry; `dto`, `withdraw` and `handover.py rehearse` reach the installed API through their own NodePort Service `anvilkit-handover-api` (30911), removed afterwards. Evidence goes to `outputs/handover/<RUN>/` (ignored by Git): counts, digests and enumerations only; tokens are read from the environment's credentials file and never printed. Legacy volumes are read only through read-only mounts or disposable copies: nothing of the legacy scope is started, written or connected by `rehearse.py` or by `handover.py plan`/`rehearse`.

**What this is not.** The qualification environment is DEVELOPMENT_ONLY (one host, simulated zones): a rehearsal here is G-13 evidence for P23 to combine, never G-13 itself, and never the handover. The withdrawal rehearsal suspends the root Application's automated sync and deletes child Applications by hand; a production withdrawal makes that change through the GitOps source. `handover.py execute` is a release action: it destroys the legacy credentials, removes the legacy containers and image tags (after saving the images), and deletes the legacy Compose definition and tooling from the working tree (the user commits the removal); it never runs from a plan, a rehearsal or a filename search.
