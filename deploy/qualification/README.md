# Qualification environment (P23, DEVELOPMENT_ONLY)

A disposable environment for P23's drills, separate from the development foundation (`deploy/dev`) and from the legacy `anvilkit-local` environment. It never touches the `anvilkit-dev` cluster or the foundation's databases, queues or Temporal; it shares only the development registry `anvilkit-dev-registry` and the `kind` Docker network.

**What it is not.** Every node is a container on one host: the zone labels `qa-a/b/c` are simulated failure domains, the DR store shares the host, the object store is a MinIO stand-in (no Ceph/RGW), the registry is CNCF Distribution (no Harbor), there is no gVisor, Cilium, workload mTLS, IdP, Apollo/MySQL or ContextForge. Measurements taken here are observations of this placement, never production SLO, RPO or RTO values (ENV-01..10 stay required; `deploy/gitops/lock.yaml`).

| Path | Purpose |
| --- | --- |
| `cluster.sh` | `up`: pull-through caches of Docker Hub, quay.io, ghcr.io, registry.k8s.io and reg.kyverno.io, MinIO vendored from the host's image cache into `anvilkit-dev-registry:5000/vendor` (Docker Hub and quay.io refuse it to anonymous pulls), the DR object store `anvilkit-qualification-dr` and the kind cluster `anvilkit-qualification` (three control-plane nodes behind kind's load balancer, three workers in zones `qa-a/b/c`), registry hosts on every node. `stop`/`start` stop the six nodes and start them again on the addresses their etcd peer certificates name (Docker would assign new ones and the control plane would not re-form); `down` deletes only that cluster; `down-dr` also removes the mirror and DR containers (the DR data directory under `.local/qualification/dr` stays). Needs `fs.inotify.max_user_instances >= 512`. |
| `kind.yaml` | The cluster definition, node image pinned by digest. |
| `values/` | The eight services' environment values: two replicas, HPA 2–3, PDB `minAvailable: 1`, hostname and zone spread, in-cluster addresses. Checked against the deployment lock. |
| `platform/` | Parent-owned charts of the environment: `admission` (signed-image policy), `data` (CloudNativePG clusters, roles, databases, Barman Cloud backups to the DR store), `objects` (in-cluster S3 stand-in), `streams` (JetStream streams, replicas 3), `migrations` (knowledge/mcp schema and Store migrations), `probes` (a LocalCheck after every environment revision). Every input of a hook Job also sits in a tracked ConfigMap: Argo CD leaves hooks out of its comparison, so a change that touched only a hook would never sync. |
| `bootstrap.py` | Credentials (`.local/qualification/credentials.json`, 0600, never printed), namespaces and Secrets through kubectl's standard input, DR store buckets and users, Argo CD, the root Application; waits for convergence, initializing OpenBao once (Shamir 3-of-5 in `.local/qualification/openbao` and the DR store) and unsealing each member as it starts, and terminating a running sync of an earlier source so a fix-forward revision can sync. |
| `check-waves.py` | P23-04: publishes a revision that breaks wave -20 and marks wave 10, shows that no later wave changes while the root waits, then fixes forward and requires convergence. |
| `check-admission.py` | P23-03: server-side dry-run admission of signed, unsigned, foreign-signed, unattested, tagged and foreign-registry images in `anvilkit-apps`. |
| `drills.py` | P23-05/07: failure and rotation drills under a business load, with evidence per drill. |
| `restore-drills.py` | P23-08: restores of the business and Temporal databases, Qdrant, etcd, OpenBao and objects from the DR store. |
| `measure-residency.py` | P23-10: two replicas of the release's Inference image on Docker (the cluster cannot hold them beside the platform): cold start, resident memory, latency, a restart while the other serves. |
| `record.py` | P23-11: the qualification record binding every piece of evidence with its digest; gates and ENV inputs as they stand. |
| `check-telemetry.py` | P23-06: every raw anvilkit-* log and the Collector's exported logs, metrics and traces searched for every secret of the environment and for secret shapes. |

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
.local/verification-venv/bin/python deploy/qualification/restore-drills.py --run RUN postgres-business ...
```

Operating notes. A StatefulSet whose Pod never became Ready (an unpullable image) keeps that Pod after its template is repaired: delete the Pod once the fixed revision is applied (Kubernetes' forced rollback). A restarted OpenBao member stays sealed until `bootstrap.py` (or an operator with three shares) unseals it; production unseals through ENV-10's KMS. Rotating the queue Valkey password restarts every member at once (a member started with the new password asks the old Sentinels for the primary and fails); production rotation needs ACL users with two passwords. `platform/valkey` is the upstream CloudPirates chart patched so the Sentinel no longer prints its password-bearing configuration (`ANVILKIT-PATCH.md`).

Evidence goes to `outputs/qualification/<RUN>/` (ignored by Git). Cleanup deletes only resources this environment owns, by exact name.
