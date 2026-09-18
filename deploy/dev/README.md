# Development foundation (DEVELOPMENT_ONLY)

The reduced local topology of [delivery.md P03](../../docs/architecture/delivery.md#p03--provision-the-isolated-development-foundation):
PostgreSQL 17, Temporal 1.31.2 and a MinIO with two object stores (the versioned artifact
bucket `anvilkit-artifacts` of P08 and the shared inventory bucket `anvilkit-inventory` of the
in-cluster Control replicas, each with its own credentials) in the `anvilkit-dev` Compose
project, the three owned schemas installed and migrated forward (`anvilkit_control` by Control's
own migration Job from `services/agent/control`, knowledge/mcp by `jobs/migration`), and a kind
cluster with the least-privilege launcher identity for host-side workers, rendered from the
Workflow chart that owns it. It is isolated from the legacy `anvilkit-local` profile
(separate project, bridge network, loopback-only ports 25432/27233/29000, own volumes) and never
attaches to its databases, histories or credentials.

It is not HA evidence, not RKE2/Cilium/gVisor, and qualifies nothing (ENV-01/02/03 remain
open). It exists so the LocalCheck chain can be exercised against a real PostgreSQL, a real
Temporal namespace and a real Kubernetes API.

```sh
sh deploy/dev/up.sh          # idempotent; writes credentials only under .local/dev/; migrates the databases forward
. .local/dev/env.sh          # ANVILKIT_DEV_CONTROL_DSN, ANVILKIT_DEV_TEMPORAL_ADDRESS, KUBECONFIG, service overrides
sh deploy/dev/down.sh        # removes anvilkit-dev containers and the kind cluster (--volumes drops data)
```

## Service configuration

Each service reads one reviewed, secret-free file, `services/agent/<service>/config.yaml`, through its typed `internal/config` (koanf): precedence is defaults < that file < the service's allowlisted `ANVILKIT_<SERVICE>_*` environment overrides; unknown keys in the file, any non-allowlisted `ANVILKIT_<SERVICE>_*` variable, missing values, out-of-range values and contradictory cross-field settings stop the process before it starts. `env.sh` exports the file paths (`ANVILKIT_{API,CONTROL,WORKFLOW}_CONFIG`) so the binaries can run from any directory, the deployment placements, and the one secret (`ANVILKIT_CONTROL_DATABASE_URL`, refused inside the file and never logged).

| Service | File sections | Allowed environment overrides |
| --- | --- | --- |
| `anvilkit-agent-api` | `http` (listen, read header timeout, body limit, shutdown), `control.address`, `auth.mode`, `sse` (heartbeat, frame buffer, slow-consumer grace, write timeout), `artifacts.transfer_window` (the deadline the API sets on a transfer it begins) | `ANVILKIT_API_LISTEN`, `ANVILKIT_API_CONTROL_ADDRESS`, `ANVILKIT_API_AUTH_MODE`, `ANVILKIT_API_PRINCIPALS_FILE` |
| `anvilkit-agent-control` | `grpc` (listen, control/execution capacity, shutdown), `inventory` (backend filesystem or s3), `artifacts` (backend s3 or disabled, object/window/capability bounds, `s3` region/prefix/path style/qualification), `temporal` (address, namespace, task queue), `relay.interval`, `profiles` (`local_check_deadline`; P13: `preparation` deadline, `max_rounds`, `max_questions`, `wait` and analysis `funding`; `generation` `queue_deadline`, `active_window`, `capacity` of the execution pool, `funding`, `definitions`, `max_repairs`, `codegen_profile`, `validator_profile`), `recovery.enumeration_page`, `dispatch` (authority freshness; `development` DEVELOPMENT_ONLY prices — a model price binds its route's trusted `provider`/`model` — route authorizations, not-sent issuers and operators, file-only, off by default), `model_proxy` (timeout, owner, identity mode of the original-identity query of model dispatches, P11; without an address the DEVELOPMENT_ONLY attestation double answers) | `ANVILKIT_CONTROL_LISTEN`, `ANVILKIT_CONTROL_DATABASE_URL` (secret), `ANVILKIT_CONTROL_INVENTORY_DIR`, `ANVILKIT_CONTROL_INVENTORY_S3_{ENDPOINT,BUCKET,ACCESS_KEY_ID,SECRET_ACCESS_KEY}`, `ANVILKIT_CONTROL_ARTIFACTS_S3_{ENDPOINT,BUCKET,ACCESS_KEY_ID,SECRET_ACCESS_KEY}` (the two key pairs are secrets and must differ: separate permission boundaries), `ANVILKIT_CONTROL_TEMPORAL_ADDRESS`, `ANVILKIT_CONTROL_MODEL_PROXY_ADDRESS`, `ANVILKIT_CONTROL_MODEL_PROXY_TOKEN` (secret) |
| `anvilkit-agent-workflow` | `temporal` (address, namespace, both task queues, identity, build id), `control.address`, `model_proxy` (timeout, identity mode; the placement and the DEVELOPMENT_ONLY token come from the environment; without an address the model call Activities answer `DEPENDENCY_UNAVAILABLE`), `kubernetes` (namespace, enabled profiles, sidecar placement, candidate seccomp profile), `execution` (Control activity timeout/retries, launch window, observation bounds, `cleanup` timeout/attempts/settle window/reconciliation pacing and bound), `lifecycle` (P13: the `preparation` analysis route and call bounds, the `generation` permit poll interval, lease TTL/renewal lead/call bound and the definition activations this worker registers, the `artifacts` transfer window, and `pagix_double_dir` — the DEVELOPMENT_ONLY lease/source/content-digest doubles, environment-only; without it every generation fails at bootstrap `DEPENDENCY_UNAVAILABLE` until ENV-07), `development` (DEVELOPMENT_ONLY fault injection, file-only), `health.listen` (`/healthz`, `/readyz`), `shutdown_timeout` | `ANVILKIT_WORKFLOW_TEMPORAL_ADDRESS`, `ANVILKIT_WORKFLOW_CONTROL_ADDRESS`, `ANVILKIT_WORKFLOW_MODEL_PROXY_ADDRESS`, `ANVILKIT_WORKFLOW_MODEL_PROXY_TOKEN` (secret), `ANVILKIT_WORKFLOW_KUBECONFIG` (a worker outside the cluster; unset, the launcher uses the Pod's ServiceAccount token), `ANVILKIT_WORKFLOW_LAUNCH_BACKEND`, `ANVILKIT_WORKFLOW_BUILD_ID`, `ANVILKIT_WORKFLOW_IMAGE_REGISTRY`, `ANVILKIT_WORKFLOW_SIDECAR_CONTROL_ADDRESS`, `ANVILKIT_WORKFLOW_HEALTH_LISTEN`, `ANVILKIT_WORKFLOW_PAGIX_DOUBLE_DIR` |
| `anvilkit-agent-model-proxy` (P11; `services/agent/model-proxy/config.yaml`, `src/config.ts`) | `http` (listen, body bound, header timeout, SSE heartbeat, slow-consumer grace, shutdown), `identity` (`disabled`, `development` bearer principals, `mtls`; the owner recorded on Control's dispatches), `control` (address, timeout, identity mode, admission reentry bounds), `store` (`filesystem` DEVELOPMENT_ONLY or `s3`), `observation` (resubmission backoff, sweep interval, reclaim grace), `routes` (each with `api`, the trusted `provider`/`model`, `base_url`, `credential_env`, limits and reviewed tool schemas; `enabled: false` in the checked-in file) | `ANVILKIT_MODEL_PROXY_LISTEN`, `ANVILKIT_MODEL_PROXY_CONTROL_ADDRESS`, `ANVILKIT_MODEL_PROXY_PRINCIPALS_FILE`, `ANVILKIT_MODEL_PROXY_STORE_DIR`, `ANVILKIT_MODEL_PROXY_STORE_S3_{ENDPOINT,BUCKET,ACCESS_KEY_ID,SECRET_ACCESS_KEY}` (the key pair is a secret), `ANVILKIT_MODEL_PROXY_CONTRACTS_DIR`, the declared route credentials `ANVILKIT_MODEL_PROXY_CREDENTIAL_<NAME>` (secrets) |
| `anvilkit-agent-knowledge` (P14; `services/agent/knowledge/config.yaml`, `src/config.ts`) | `grpc` (listen, capacity, shutdown), `health.listen`, `database.max_conn`, `tasks` (input bound, max lease, retry delay and bound, sweep), `outbox.consumer_group`, `control.timeout`, `apollo` (mode, app id), `reload` (interval, drain limit) | `ANVILKIT_KNOWLEDGE_{LISTEN,HEALTH_LISTEN,DATABASE_URL,DATABASE_URL_FILE,CONTROL_ADDRESS,APOLLO_SNAPSHOT_FILE,CONFIG}`; a rotated `DATABASE_URL_FILE` publishes a new generation |
| `anvilkit-agent-mcp` (P14; `services/agent/mcp/config.yaml`, `internal/config`) | `grpc`, `health`, `database.max_conn`, `tasks`, `outbox` (forwarder toggle, consumer group, poll/ack/resend/batch, `nats.publish_timeout`/`name`), `control.timeout`, `apollo`, `reload` | `ANVILKIT_MCP_{LISTEN,HEALTH_LISTEN,DATABASE_URL,DATABASE_URL_FILE,NATS_URL,CONTROL_ADDRESS,APOLLO_SNAPSHOT_FILE,CONFIG}` |
| `anvilkit-agent-background-worker` (P14; `services/agent/background-worker/config.yaml`, `src/config.ts`) | `health.listen`, `queue.prefix`, `worker` (concurrency, lease, heartbeat, input bound, handler timeout, queue attempts/backoff/lock/stall, submit retries, owner timeout, shutdown), `bull_board` (enabled, listen, read-only), `relay` (batch, ack wait, max deliver, reconcile interval/age/limit, shutdown) | `ANVILKIT_BACKGROUND_WORKER_{HEALTH_LISTEN,QUEUE_URL,QUEUE_URL_FILE,NATS_URL,KNOWLEDGE_ADDRESS,MCP_ADDRESS,CONTRACTS_DIR,BULL_BOARD_LISTEN,RELAY_OWNER,RELAY_DATABASE_URL,RELAY_DATABASE_URL_FILE,CONFIG}` |
| Knowledge forwarder sidecar (P14; `services/agent/knowledge/forwarder`) | none (no reviewed business parameter) | `ANVILKIT_FORWARDER_{DATABASE_URL,DATABASE_URL_FILE,NATS_URL,CONSUMER_GROUP,HEALTH_LISTEN,POLL_INTERVAL,ACK_DEADLINE,RESEND_INTERVAL,BATCH_SIZE,PUBLISH_TIMEOUT,CLOSE_TIMEOUT,RELOAD_INTERVAL}` |

The Workflow's `execution` section is recorded into every run's history at its start (`workflow.SideEffect`), so a redeployed worker with different values never changes the replay of an existing run. Apollo generations, reload and secret rotation are P14 work on top of these files.

| Listener | Address | Identity |
| --- | --- | --- |
| PostgreSQL | `127.0.0.1:25432` | `anvilkit_{control,knowledge,mcp}_{app,migrator}`, `temporal`; app roles have no DDL |
| Temporal frontend | `127.0.0.1:27233` | namespace `anvilkit`, no TLS (mTLS is an ENV-03 deployment input) |
| Kubernetes | kind `anvilkit-dev`, `.local/dev/launcher.kubeconfig` | ServiceAccount `anvilkit-agent-workflow-host` in `anvilkit-components` with the Role/RoleBinding `anvilkit-agent-workflow-host-launcher`: Jobs create/get/list/watch/delete and Pods read in `anvilkit-components` only. The definition is the Workflow chart's (`services/agent/workflow/deploy/chart`, templates `serviceaccount.yaml` and `rbac.yaml`, the single owner of the launcher RBAC), rendered by `up.sh` under the release name `anvilkit-agent-workflow-host` for the workers that run on the host; the in-cluster release of `workflow-chart.sh` creates its own identity from the same templates. The namespaces (`deploy/dev/k8s/namespaces.yaml`) stay platform-owned |
| MinIO (object stores) | `127.0.0.1:29000` | The artifact store (P08): root credentials of `.local/dev/minio.env` (generated once, DEVELOPMENT_ONLY), the versioned bucket `anvilkit-artifacts`; Control's `Qualify` probe (presigned PUT, read by version, versioning) runs at startup. The shared inventory of the in-cluster Control replicas: the bucket `anvilkit-inventory` with its own user and bucket-limited policy from `.local/dev/minio-inventory.env` (Control refuses one access key for both stores); Control's inventory `Qualify` probe (conditional create, same-body idempotency, read-after-write, resumable listing) runs at startup. The Model Proxy's record and evidence store (P11): the bucket `anvilkit-model-proxy` with its own user and bucket-limited policy from `.local/dev/minio-model-proxy.env` (conditional creates and ETag-conditional replaces; the Proxy's `qualify` probe runs at startup). All three are created by `deploy/dev/minio-setup.sh`. Host-side runs keep the filesystem inventory (`ANVILKIT_CONTROL_INVENTORY_DIR`) and the Proxy's filesystem store (`ANVILKIT_MODEL_PROXY_STORE_DIR`). It is the S3 API only: not the production object backend of C09, and no placement, retention or failure-domain claim (ENV-02/ENV-09) |
| NATS JetStream (P14) | `127.0.0.1:24222` (monitoring on the container's 8222) | No authentication (ENV-03); the three domain streams `ANVILKIT_KNOWLEDGE`, `ANVILKIT_MCP`, `ANVILKIT_CONTROL` (`anvilkit.<domain>.>`) are created by `nats-setup` from the reviewed DEVELOPMENT_ONLY definitions under `deploy/dev/nats/` (file storage, one replica, 7-day retention, 1 MiB messages, 2-minute duplicate window) |
| Valkey, queue instance (P14) | `127.0.0.1:26379` | The BullMQ instance (`ANVILKIT_DEV_QUEUE_URL`), append-only persistence; a single node — Sentinel failover is not qualified here |
| Valkey, cache instance (P14) | `127.0.0.1:26380` | A separate instance (`ANVILKIT_DEV_CACHE_URL`), never the queue (C03) |

## Running the LocalCheck chain by hand

```sh
. .local/dev/env.sh
(cd services/agent/control && go run ./cmd/anvilkit-agent-control) &
(cd services/agent/api && ANVILKIT_API_LISTEN=127.0.0.1:9100 go run ./cmd/anvilkit-agent-api) &
(cd services/agent/workflow && go run ./cmd/anvilkit-agent-workflow) &
curl -s -X POST http://127.0.0.1:9100/api/v1/operations -H "Authorization: Bearer $ANVILKIT_DEV_API_TOKEN_A" \
  -H 'Content-Type: application/json' \
  -d '{"commandId":"cmd-1","kind":"local_check","subject":{"profileId":"local-check-v1","subjectDigest":"sha256:0dc7fa9db7237a2b5c96f70f59bb00f73bb86a0ca5554e91c312f9ada26e18b3"}}'
curl -N http://127.0.0.1:9100/api/v1/operations/<operationId>/events -H "Authorization: Bearer $ANVILKIT_DEV_API_TOKEN_A"
```

The same chain is exercised automatically by `tests/integration` (`go test -tags integration ./...`
with the environment sourced), which builds and runs the three binaries itself, each from its own
repository at `services/agent/{control,api,workflow}`, using temporary copies of the checked-in `config.yaml` files with a unique pair of test queues
(the worker killed mid-observation in `TestLocalCheckChain` is killed only once Temporal shows its `ObserveJob`
Activity started); `TestFaultScenarios` and `TestUnresolvedCreateRecovery` run the worker
with temporary copies of its file that enable `development.lose_receipt_once` /
`development.hold_until_canceled_once` and shorten or widen the cleanup/reconciliation bounds;
`TestDispatchAdmission` runs two Control processes with a temporary copy of Control's file that enables
the `dispatch.development` fixtures; `TestRecoveryReconciliation` adds a fixture operator and a shortened
settle window; `TestModelProxyControlledCalls` adds the Proxy's controlled route to those fixtures and runs
two Proxy processes (see below); `TestArtifactTransfer` and `TestArtifactAcceptance` run API-12 and the acceptance path
against the foundation's MinIO (the artifact environment of `env.sh`).

## The API in the kind cluster

`sh deploy/dev/api-chart.sh install` builds the API image from `services/agent/api` alone (the
API repository; the build resolves the published contracts module `go/v0.1.1` through the public
proxy, `ANVILKIT_DEV_GOPROXY`/`ANVILKIT_DEV_GONOSUMDB` only redirect it to a private proxy), loads
it into the `anvilkit-dev` node (a rebuilt image rolls the Pods: its ID is set as the
`anvilkit.io/image-id` Pod annotation, so the Deployment's template changes exactly when the image did),
creates the principals Secret from `.local/dev/api-principals.json` (never in Git) and installs the
API repository's chart (`services/agent/api/deploy/chart`) into `anvilkit-apps` with the parent-owned
development values `deploy/dev/values/anvilkit-agent-api.yaml`; `control.address` is set to the
Docker network gateway of kind, so the host-side Control must listen on a non-loopback address for
this topology (`ANVILKIT_CONTROL_LISTEN=0.0.0.0:9101`). `kubectl -n anvilkit-apps port-forward
svc/anvilkit-agent-api 9100:80` exposes it; `sh deploy/dev/api-chart.sh uninstall` removes it.

The LocalCheck flows run against that API with the harness's own Control and Workflow:

```sh
. .local/dev/env.sh
cd tests/integration && ANVILKIT_INTEGRATION_API_URL=http://127.0.0.1:9100 ANVILKIT_INTEGRATION_CONTROL_LISTEN=0.0.0.0:9101 \
  ANVILKIT_INTEGRATION_TOKEN_A=<tenant_a token of .local/dev/api-principals.json> ANVILKIT_INTEGRATION_TOKEN_B=<tenant_b token> \
  go test -tags integration -run TestLocalCheckChain ./...
```

This is development-foundation verification of the packaged API, not production qualification.

## Control and the Workflow worker in the kind cluster

`sh deploy/dev/control-chart.sh install` builds the Control image from `services/agent/control` alone (the
Control repository; the build resolves the contracts module through the public proxy), loads it into the
node, creates the four Secrets from `.local/dev/` (the application and migrator database URLs as the
cluster reaches PostgreSQL, the inventory user of `minio-inventory.env`, the artifact store credentials of
`minio.env`; never in Git) and installs the Control repository's chart (`services/agent/control/deploy/chart`)
into `anvilkit-apps` with the parent-owned development values `deploy/dev/values/anvilkit-agent-control.yaml`:
two replicas sharing the `anvilkit-inventory` bucket (the S3 inventory backend; the filesystem adapter is
never rendered by the chart), the artifact store, the foundation's Temporal by its kind-network address, and
the chart's migration Job (`anvilkit-migration` of the same image, pre-install/pre-upgrade hook) applied
with the migrator role before the Pods roll. The gRPC health service answers the Pods' probes.

`sh deploy/dev/workflow-chart.sh install` builds the Workflow image from `services/agent/workflow` alone,
loads it and installs the Workflow repository's chart (`services/agent/workflow/deploy/chart`) with
`deploy/dev/values/anvilkit-agent-workflow.yaml`: two replicas that reach the Control release inside the
cluster (`anvilkit-agent-control.anvilkit-apps.svc.cluster.local:9101`, also as the sidecar address of
the Jobs they launch), the foundation's Temporal by its kind-network address, and the launcher identity the
chart creates itself (ServiceAccount in `anvilkit-apps`, Role/RoleBinding `anvilkit-agent-workflow-launcher`
in `anvilkit-components`; the Pod's mounted token, no kubeconfig). The health listener answers the probes.
Install Control first; `uninstall` removes either release. The in-cluster workers poll the same Temporal
namespace as the host-side workers of the integration scenarios: uninstall the Workflow release before
running those. `up.sh` attaches the foundation's PostgreSQL, Temporal and MinIO to the kind network and
exports the cluster-side addresses (`ANVILKIT_DEV_*_CLUSTER`) and the inventory credentials in `env.sh`.

This is development-foundation verification of the packaged services and charts (two replicas each on
one kind node), not HA evidence and not production qualification.

## The Model Proxy (P11) on the foundation

`env.sh` exports the Proxy's inputs for host-side runs: its reviewed file (`ANVILKIT_MODEL_PROXY_CONFIG`), the contracts checkout it validates against, the Control placement, its DEVELOPMENT_ONLY bearer principals (`.local/dev/model-proxy-principals.json`: one token each for the Workflow's Activities, the access sidecar and Control's query, exported as `ANVILKIT_DEV_MODEL_PROXY_TOKEN_{WORKFLOW,SIDECAR,CONTROL}`), a filesystem store directory and the `anvilkit-model-proxy` bucket's credentials (`ANVILKIT_DEV_MODEL_PROXY_*`). No route credential is exported: the checked-in `controlled-openai-v1` route stays unserved (`pnpm start` in `services/agent/model-proxy` after `. .local/dev/env.sh` answers `/readyz` on its probe listener — `health.listen`, `127.0.0.1:9104` by default, `ANVILKIT_MODEL_PROXY_HEALTH_LISTEN` — and refuses every call with `PROFILE_UNQUALIFIED`), and `TestModelProxyControlledCalls` in `tests/integration` enables it in its own configuration copies against its countable upstream double: two Proxy processes (`node dist/main.js`, built by the scenario from the Proxy repository) sharing one filesystem store against two Control processes with the route's fixture price, the Workflow's `CallModel` Activity (executed by the Workflow repository's `TestCallModelActivityChain` against those processes), the access sidecar as UID 10002 relaying under its confirmed scope, and Control's recovery query of a lost send through the RecoveryService. The scenario reopens the recovery run it begins; the shared `tenant_a` scope must not stay closed behind it.

`sh deploy/dev/model-proxy-chart.sh install` builds the Proxy image from `services/agent/model-proxy` alone (with `contracts/` as the named build context the image build declares), loads it into the node, creates the two Secrets from `.local/dev/` (the store user of `minio-model-proxy.env`, the principals file) and installs the Proxy repository's chart with `deploy/dev/values/anvilkit-agent-model-proxy.yaml`: two replicas sharing the `anvilkit-model-proxy` bucket (the chart renders the S3 store only), reaching the Control release of the namespace, with no route credential referenced (nothing is served; the routes are declared). `uninstall` removes the release. This is development-foundation verification of the packaged service and chart, not HA evidence and not production or provider qualification.

## The background lane (P14) on the foundation

`up.sh` migrates `anvilkit_knowledge` and `anvilkit_mcp` to `00002` (the outbox physical schema of the pinned watermill-sql adapter, the request columns, the `local-check` fixture kind) and creates the relay identities `anvilkit_{knowledge,mcp}_relay` and the forwarder identity `anvilkit_knowledge_forwarder` (`postgres-init.sh` on a fresh data directory, an idempotent step of `up.sh` on an existing one). `env.sh` exports the placements of the six host-side processes; each reads its reviewed file and the allowlisted environment:

| Process | Command (after `. .local/dev/env.sh`) | Placements from env.sh |
| --- | --- | --- |
| MCP owner | `go run ./cmd/anvilkit-agent-mcp` in `services/agent/mcp` (listens on `127.0.0.1:9106`, health `9116`) | `ANVILKIT_MCP_{CONFIG,DATABASE_URL,NATS_URL,CONTROL_ADDRESS}` |
| Knowledge owner | `node dist/main.js` in `services/agent/knowledge` after `pnpm run build` (listens on `127.0.0.1:9105`, health `9115`) | `ANVILKIT_KNOWLEDGE_{CONFIG,DATABASE_URL,CONTROL_ADDRESS}` |
| Knowledge forwarder sidecar | `go run ./cmd/anvilkit-knowledge-forwarder` in `services/agent/knowledge/forwarder` (health `9125`) | `ANVILKIT_FORWARDER_{DATABASE_URL,NATS_URL}` |
| Owner queue relays | `node dist/main.js relay` in `services/agent/background-worker` with `ANVILKIT_BACKGROUND_WORKER_RELAY_OWNER=knowledge ANVILKIT_BACKGROUND_WORKER_RELAY_DATABASE_URL=$ANVILKIT_DEV_KNOWLEDGE_RELAY_DSN` (and `mcp` / `$ANVILKIT_DEV_MCP_RELAY_DSN`, each with its own `ANVILKIT_BACKGROUND_WORKER_HEALTH_LISTEN`) | `ANVILKIT_BACKGROUND_WORKER_{CONFIG,QUEUE_URL,NATS_URL,KNOWLEDGE_ADDRESS,MCP_ADDRESS,CONTRACTS_DIR}` |
| Background Worker | `node dist/main.js worker` in `services/agent/background-worker` (health `9127`) | the same |

Fixture requests: `node dist/localcheck.js request --source <source_id>` (Knowledge; the source row must exist) and `go run ./cmd/anvilkit-agent-mcp local-check request -grant <grant_id>` (MCP; the grant row must be `active`), both printing the task identity and the expected result digest; `cancel` and `get` follow. `tests/integration` (`TestBackgroundLane`) starts all six processes itself, seeds the `src_int` source and `g_int` grant through the migrator identities, empties the queue Valkey (`docker exec anvilkit-dev-valkey-queue-1 valkey-cli FLUSHALL`) and runs the failure scenarios.

## The trusted Job boundary (P09) on the foundation

`deploy/dev/up.sh` adds the DEVELOPMENT_ONLY runtime inputs of the trusted Job boundary to the kind cluster; none of them qualifies gVisor, the RKE2/Cilium combination or a production registry:

| Input | What up.sh does | Why |
| --- | --- | --- |
| Local registry | `anvilkit-dev-registry` (`registry:3`, pinned by digest) on the kind Docker network, published on `127.0.0.1:5001`; the node's containerd gets `/etc/containerd/certs.d/localhost:5001/hosts.toml` and `config_path` (containerd restarted once) | Profiles pin images by digest and the kubelet pulls `localhost:5001/<image>@sha256:…` by digest |
| Node pool | Label `anvilkit.io/pool=components` on the node | The harness template's node selector (DD-10 §2) |
| Candidate syscall profile | `deploy/policies/seccomp/anvilkit-candidate.json` copied to `/var/lib/kubelet/seccomp/anvilkit/candidate.json` on the node | The supervisor container's `Localhost` seccomp profile: AF_UNIX sockets only |
| Kyverno 1.19.1 | `install.yaml` and the CLI downloaded once into `.local/dev/kyverno/` (checksums pinned in up.sh), installed with server-side apply; the P09 policies of `deploy/policies` applied | Actual admission of the fixed templates (`deploy/policies/check.sh --cluster` for the dry-run evidence) |
| The foundation on the kind network | `anvilkit-dev-minio-1`, `anvilkit-dev-postgres-1` and `anvilkit-dev-temporal-1` connected to the `kind` network; `ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER`, `ANVILKIT_DEV_CONTROL_DSN_CLUSTER`, `ANVILKIT_DEV_TEMPORAL_ADDRESS_CLUSTER` in `env.sh` | A Job's sidecar uploads to the presigned URL Control issued (Control must sign for the address the Pod reaches); the Control and Workflow releases in the cluster reach the database and Temporal |
| Gateway | `ANVILKIT_DEV_KIND_GATEWAY` in `env.sh` | A host-side Control listening on `0.0.0.0` is reached from a Pod at `<gateway>:<port>`; the verification chain starts such a Control from `services/agent/control` for the Workflow repository's cluster scenario and names it through `ANVILKIT_INTEGRATION_CONTROL_ADDRESS` / `ANVILKIT_INTEGRATION_SIDECAR_CONTROL_ADDRESS` |

`sh deploy/dev/images.sh --harness` builds `anvilkit-codegen` from `jobs/codegen` and
`anvilkit-job-access-sidecar` from its own repository Dockerfile against the published
contracts `go/v0.1.2`, then pushes both to the local Docker registry. Without `--harness`
it also builds the P10 validator, preserving its named contracts build context, and the P12
team image `anvilkit-codegen-team` (`jobs/codegen/Dockerfile.team`: the validator image at the
digest that Dockerfile names as its base, plus the supervisor and the team package). Pin the
printed digests in `contracts/jobs/profiles.json` and the admission policy, increment every
affected profile revision, regenerate with `contracts/tools/generate.py`, and render/check
the templates with `sh deploy/policies/check.sh --cluster`. The [P09 acceptance record](../../docs/architecture/delivery.md#verification-p09-repair)
records the repaired images actually run; local registry delivery is not remote publication.

The Workflow's checked-in `config.yaml` enables `local-check-v1` only; `kubernetes.image_registry` comes from `ANVILKIT_WORKFLOW_IMAGE_REGISTRY` (`localhost:5001` in `env.sh`). The P09 scenarios (`TestTrustedHarnessProcessFlow` in `tests/integration`, real processes on the host; `TestHarnessOnTheDevelopmentCluster` in the Workflow repository's `internal/adapters/kubernetes`, the wiring profile on the kind cluster, against the Control the parent's chain prepares) construct their launchers with `harness-wiring-dev-v1` enabled; `codegen-fixed-v1` (gVisor) is enabled nowhere, and so is the P12 team profile `codegen-team-dev-v1` (it runs model-written code, needs gVisor, and the admission policy names no team Pod). The kind cluster has no gVisor RuntimeClass: the candidate profiles cannot be admitted here, which is what the policy check records as UNEXECUTED. The P12 scenario (`TestCodegenTeamFlow` in `tests/integration`) runs the team with real processes on the host: the supervisor in team mode, the coordinator, the Pi coder as UID 10001, the validator's chain, the sidecar, one Model Proxy against the countable upstream, two Controls.

Pinned images: `postgres:17-alpine@sha256:18cfe3ef…`, `temporalio/server:1.31.2@sha256:6b02e517…`,
`temporalio/admin-tools:1.31.2@sha256:dbc5fcd6…`; kind 0.33.0 (node image resolved by kind); `registry:3@sha256:1be55279…`; Kyverno `v1.19.1` (`install.yaml` sha256 `d3322cb3…`, CLI tarball sha256 `b38228f3…`).

For concurrent verification, use an isolated Control database as well as the private queues:
other Control relays must not claim this suite's operations. The P09 run used the existing
PostgreSQL container, database `anvilkit_control_p09` owned by `anvilkit_control_migrator`,
and Control's own migrations through version 7. Its local environment wrapper
`.local/p09-repair/env.sh` sources `.local/dev/env.sh` and changes only the test database URLs.
No concurrent workers or legacy resources are stopped. The full chain remains
`sh tools/verification-env.sh` with that environment sourced.
