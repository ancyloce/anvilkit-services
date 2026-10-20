# Qualification environment (P23, DEVELOPMENT_ONLY)

A disposable environment for P23's drills, separate from the development foundation (`deploy/dev`) and from the legacy `anvilkit-local` environment. It never touches the `anvilkit-dev` cluster or the foundation's databases, queues or Temporal; it shares only the development registry `anvilkit-dev-registry` and the `kind` Docker network.

**What it is not.** Every node is a container on one host: the zone labels `qa-a/b/c` are simulated failure domains, the DR store shares the host, the object store is a MinIO stand-in (no Ceph/RGW), the registry is CNCF Distribution (no Harbor), there is no gVisor, Cilium, workload mTLS, IdP, Apollo/MySQL or ContextForge. Measurements taken here are observations of this placement, never production SLO, RPO or RTO values (ENV-01..10 stay required; `deploy/gitops/lock.yaml`).

| Path | Purpose |
| --- | --- |
| `cluster.sh` | `up`: pull-through caches of Docker Hub, quay.io, ghcr.io, registry.k8s.io and reg.kyverno.io, MinIO vendored from the host's image cache into `anvilkit-dev-registry:5000/vendor` (Docker Hub and quay.io refuse it to anonymous pulls), the DR object store `anvilkit-qualification-dr` and the kind cluster `anvilkit-qualification` (three control-plane nodes behind kind's load balancer, three workers in zones `qa-a/b/c`), registry hosts on every node. `stop`/`start` stop the six nodes and start them again on the addresses their etcd peer certificates name (Docker would assign new ones and the control plane would not re-form); `down` deletes only that cluster; `down-dr` also removes the mirror and DR containers (the DR data directory under `.local/qualification/dr` stays). Needs `fs.inotify.max_user_instances >= 512`. |
| `kind.yaml` | The cluster definition, node image pinned by digest. |
| `values/` | The eight services' environment values: two replicas, HPA 2–3, PDB `minAvailable: 1`, hostname and zone spread, in-cluster addresses. Checked against the deployment lock. |
| `platform/` | Parent-owned charts of the environment: `admission` (signed-image policy), `data` (CloudNativePG clusters, roles, databases, Barman Cloud backups to the DR store), `objects` (in-cluster S3 stand-in), `streams` (JetStream streams, replicas 3), `migrations` (knowledge/mcp schema and Store migrations), `probes` (a LocalCheck after every environment revision), `backups` (P3.7: scheduled snapshots of Qdrant and OpenBao and a mirror of the object store to the DR store, one CronJob `anvilkit-backup-<store>` each, credentials from OpenBao at run time). Every input of a hook Job also sits in a tracked ConfigMap: Argo CD leaves hooks out of its comparison, so a change that touched only a hook would never sync. |
| `bootstrap.py` | Credentials (`.local/qualification/credentials.json`, 0600, never printed), namespaces and Secrets through kubectl's standard input, DR store buckets and users, Argo CD, the root Application; waits for convergence, initializing OpenBao once (Shamir 3-of-5 in `.local/qualification/openbao` and the DR store) and unsealing each member as it starts, and terminating a running sync of an earlier source so a fix-forward revision can sync. |
| `check-waves.py` | P23-04: publishes a revision that breaks wave -20 and marks wave 10, shows that no later wave changes while the root waits, then fixes forward and requires convergence. |
| `check-admission.py` | P23-03: server-side dry-run admission of signed, unsigned, foreign-signed, unattested, tagged and foreign-registry images in `anvilkit-apps`. |
| `drills.py` | P23-05/07: failure and rotation drills under a business load, with evidence per drill. |
| `restore-drills.py` | P23-08: restores of the business and Temporal databases, Qdrant, etcd, OpenBao and objects from the DR store. |
| `measure-residency.py` | P23-10: two replicas of the release's Inference image on Docker (the cluster cannot hold them beside the platform): cold start, resident memory, latency, a restart while the other serves. |
| `record.py` | P23-11: the qualification record binding every piece of evidence with its digest; gates and ENV inputs as they stand. |
| `check-telemetry.py` | P23-06: every raw anvilkit-* log and the Collector's exported logs, metrics and traces searched for every secret of the environment and for secret shapes. |
| `check-dataplane.py` | P0.6 AC3: through one fresh port-forward per case, NATS, the Valkey master proxy, servers and Sentinel, and the business PostgreSQL primary: plaintext, credential-less, unlisted and wrong credentials refused, one deployed credential accepted as the positive control, an out-of-scope NATS publication refused; nothing written to a stream or queue key (`tests/integration` `TestQualificationDataPlane`). |
| `rollback-drill.py` | P3.2 AC4: promotes release N+1 over N and rolls back, measuring convergence and the rollback window; the probe Application must sync green on the rolled-back version. |
| `alert-drill.py` | P3.6 AC2: injects every AnvilKit alert's condition through a drill metrics Pod and proves each alert fires and reaches the receiver of its severity. |
| `loadrun.py` | P3.5: shared pieces of the soak and chaos runs (the load tool binary, the Prometheus readout through `kubectl proxy`, Control's ledger checks: duplicate `actual` cost entries, operations stuck `reconciling`, dispatches past their deadline without observation or disposition). `--self-test`. |
| `soak.py` | P3.5 AC2: >= 4 h of the load tool (`tests/load`) with holds, cancels and stream resumes while each application Deployment is rolled out in turn; zero duplicate sends/charges, zero stuck `reconciling`, zero `SENDER_LOST` from rollouts, no restart from load. `--self-test`. |
| `chaos.py` | P3.5 AC3: Control/Proxy/MCP Pod faults and the PostgreSQL, Valkey, NATS and object-store drills of `drills.py`, each under its own load; the designed outcome per fault and no duplicate charge. `--self-test`. |
| `check-dr.py` | P3.7 AC3: the DR store's least-privilege users and object lock (`bootstrap.py` `DR_USERS`, `DR_BUCKETS`): create-only inventory and removal users, a snapshot writer without delete, a prune identity that can neither read nor write, locked versions that no one deletes within their retention. `--local` applies the bootstrap's DR script to a disposable MinIO and runs every probe; `--run` probes the qualification DR store; `--self-test`. |
| `workflow-rollout.py` | P1.7: the Workflow worker's Worker Deployment Versioning rollout (any cluster and release of the Workflow chart with `config.temporal.versioning.enabled`): installs build B's Deployment beside A's, promotes B once it is ready and has registered its task queues, and removes A only after Temporal reports it DRAINED with no open pinned run; `retire` resumes the drain and removal later. `--self-test`. Procedure below. |
| `workflow-rollout-drill.sh` | P1.7 AC2 on the development foundation (DEVELOPMENT_ONLY): `workflow-rollout.py` with the Workflow image and real LocalCheck runs in the namespace `anvilkit-rollout-drill` on task queues and a worker deployment of its own (no RBAC or certificate elsewhere; Control unreachable by design, so each run stays open on its build while its first Control call retries, about 4 minutes, then closes failed): first versioned install, DRAINING refusal while runs pinned to the old build are open, retire after DRAINED, an aborted build that never becomes ready, its retirement; run histories and worker logs searched for non-determinism. Record under `outputs/workflow-rollout-drill-<UTC>/`. |

```sh
sh deploy/qualification/cluster.sh up
.local/verification-venv/bin/python tools/release-artifacts.py --run RUN
.local/verification-venv/bin/python deploy/gitops/publish.py --environment qualification --run RUN
.local/verification-venv/bin/python deploy/qualification/bootstrap.py --run RUN
.local/verification-venv/bin/python deploy/qualification/check-admission.py --run RUN
.local/verification-venv/bin/python deploy/qualification/drills.py --run RUN node-loss db-primary ...
```

```sh
.local/verification-venv/bin/python deploy/qualification/check-waves.py --run RUN --revision N
.local/verification-venv/bin/python deploy/qualification/check-telemetry.py --run RUN
.local/verification-venv/bin/python deploy/qualification/check-dataplane.py --run RUN
.local/verification-venv/bin/python deploy/qualification/restore-drills.py --run RUN postgres-business ...
```


### Workflow worker rollout under Worker Deployment Versioning (P1.7)

With `config.temporal.versioning.enabled` every Workflow run is pinned to the worker build it started on, so a plain rolling update would leave the pinned runs of the old build without a worker. The Workflow chart then renders one worker version per release: the Deployment, ConfigMap, HPA and PDB are named `<fullname>-<first 10 hex of sha256(buildId)>`, selected by `anvilkit.io/worker-version`, annotated with the Temporal deployment and Build ID, and carry `helm.sh/resource-policy: keep` and Argo CD `Prune=false`, so the next release starts its version beside the previous one instead of replacing it. The rollout moves a release from build A to build B:

1. **Install** B beside A: `helm upgrade --install` with B's values (the image digest is the Build ID); the release that first enables versioning has an unversioned Deployment, which the tool annotates with the keep policies before the upgrade. A's Deployment must still exist, unchanged.
2. **Ready**: B's rollout completes and B's version has registered both task queues and every task queue type the current version serves. Otherwise the rollout is ABORTED: nothing is promoted, A stays current and running, B's objects stay for diagnosis (`retire` removes a never-promoted build later).
3. **Promote**: `temporal worker deployment set-current-version` B. New runs land on B; A's pinned runs continue on A.
4. **Drain**: A's version is polled (`describe-version` drainage status and a visibility count of the runs pinned to it, both reported) until Temporal reports DRAINED, checked after A's last routing change, and no open run is pinned to A. Temporal reports DRAINED only a visibility grace period (3 minutes by default) after the last pinned run closed.
5. **Remove** A's Deployment, ConfigMap, PDB and HPA by exact name, waiting for its Pods. A DRAINING, current or ramping build is never removed: at `--drain-timeout` the result is DRAINING (exit 3) and `retire` resumes steps 4-5 later. An unversioned predecessor holds no pinned runs: it is removed once B is current, its open runs move to B on their next task, as in a rolling update (counted in the record).

```sh
# Temporal CLI: `temporal` on PATH (TLS flags may be part of --temporal-cli as --flag=value), or on the
# development foundation the pinned admin-tools image:
T="docker run --rm --network host --entrypoint temporal temporalio/admin-tools:1.31.2@sha256:dbc5fcd6ee8f0f4d808bf765af9a87dea9d8a283abfdcfbd2fc148496ba66107"
.local/verification-venv/bin/python deploy/qualification/workflow-rollout.py rollout --namespace NS [--context CTX] \
    --temporal-address HOST:PORT --temporal-cli "$T" -f VALUES --set image.digest=sha256:... --set config.temporal.versioning.enabled=true
.local/verification-venv/bin/python deploy/qualification/workflow-rollout.py retire --namespace NS --temporal-address HOST:PORT --temporal-cli "$T"
.local/verification-venv/bin/python deploy/qualification/workflow-rollout.py --self-test
```

After an ABORTED rollout the release's latest revision renders the aborted build while the running build stays through its keep policy: roll forward with a fixed build (its rollout treats both as previous builds) or `helm rollback` to the previous revision, and `retire` removes the aborted build, which never registered and holds no runs. `--no-install` skips step 1 where the environment's GitOps applied B's release (Argo CD keeps A through `Prune=false`). The record goes to `outputs/workflow-rollout/<UTC>/` (or `--out`). Exit 0 COMPLETE, 1 ABORTED or FAILED, 2 REFUSED (nothing changed: an unversioned or self-promoting render, an unreadable routing, a current version without a Deployment in the release), 3 DRAINING. The tool changes workloads and the deployment's routing: run it only with the environment operator's authorization. Versioning stays disabled in the chart default and in the development and qualification values; enabling it in an environment is that environment's decision.

Operating notes. A StatefulSet whose Pod never became Ready (an unpullable image) keeps that Pod after its template is repaired: delete the Pod once the fixed revision is applied (Kubernetes' forced rollback). A restarted OpenBao member stays sealed until `bootstrap.py` (or an operator with three shares) unseals it; production unseals through ENV-10's KMS. Rotating the queue Valkey password restarts every member at once (a member started with the new password asks the old Sentinels for the primary and fails); production rotation needs ACL users with two passwords. A host reboot restarts the nodes through Docker's restart policy, not `cluster.sh start`, and has these effects:
- `fs.inotify.max_user_instances` is back at 128, and a kubelet fails on `inotify_init`.
- A control plane can come back on a new address. Free its etcd peer certificate's address if another container took it, then reconnect the node on it.
- OpenBao is sealed: `.local/verification-venv/bin/python deploy/qualification/bootstrap.py --unseal-only` unseals the running members with the custody shares and changes nothing else (exit 1 while a member stays sealed).
- With more than 500 inotify instances in use on the host (three kind clusters), 512 is not enough either: a restarted node's systemd fails with "Failed to create control group inotify object: Too many open files" and exits 255; raise the limit (1024 served on 2026-10-18).
- The Valkey master proxies can keep a resolution taken before cluster DNS was ready, with every server DOWN, until their `haproxy` containers restart.
- A host clock that steps by more than 2 s keeps Valkey's Sentinels in TILT; failovers they abort meanwhile can leave every queue server a replica of another (`role:slave`, `master_link_status:down` on all three, `-failover-abort-no-good-slave` in the Sentinel logs), and nothing is primary. With the clock steady, promote the server the Sentinels name as master (`+try-failover master mymaster <server>`), after checking that the servers hold the same data (`DBSIZE`): `$K -n anvilkit-data exec anvilkit-valkey-queue-<i> -c valkey -- sh -c 'valkey-cli --tls --cacert /etc/valkey/tls/ca.crt --cert /etc/valkey/tls/tls.crt --key /etc/valkey/tls/tls.key -h 127.0.0.1 -p 6380 REPLICAOF NO ONE'` (the container's `REDISCLI_AUTH` authenticates). The others resynchronize from it and the Sentinels clear `odown`; the Background Worker and the relays reconnect without a restart. `platform/valkey` is the upstream CloudPirates chart patched so the Sentinel no longer prints its password-bearing configuration (`ANVILKIT-PATCH.md`).

Evidence goes to `outputs/qualification/<RUN>/` (ignored by Git). Cleanup deletes only resources this environment owns, by exact name.
