# Development foundation (DEVELOPMENT_ONLY)

The reduced local topology of [delivery.md P03](../../docs/architecture/delivery.md#p03--provision-the-isolated-development-foundation):
PostgreSQL 17 and Temporal 1.31.2 in the `anvilkit-dev` Compose project, the three owned
schemas installed from `00001_init.sql` by the migration Job, and a kind cluster with the
least-privilege launcher identity. It is isolated from the legacy `anvilkit-local` profile
(separate project, bridge network, loopback-only ports 25432/27233, own volume) and never
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

Each service reads one reviewed, secret-free file, `services/<service>/config.yaml`, through its typed `internal/config` (koanf): precedence is defaults < that file < the service's allowlisted `ANVILKIT_<SERVICE>_*` environment overrides; unknown keys in the file, any non-allowlisted `ANVILKIT_<SERVICE>_*` variable, missing values, out-of-range values and contradictory cross-field settings stop the process before it starts. `env.sh` exports the file paths (`ANVILKIT_{API,CONTROL,WORKFLOW}_CONFIG`) so the binaries can run from any directory, the deployment placements, and the one secret (`ANVILKIT_CONTROL_DATABASE_URL`, refused inside the file and never logged).

| Service | File sections | Allowed environment overrides |
| --- | --- | --- |
| `anvilkit-agent-api` | `http` (listen, read header timeout, body limit, shutdown), `control.address`, `auth.mode`, `sse` (heartbeat, frame buffer, slow-consumer grace, write timeout) | `ANVILKIT_API_LISTEN`, `ANVILKIT_API_CONTROL_ADDRESS`, `ANVILKIT_API_AUTH_MODE`, `ANVILKIT_API_PRINCIPALS_FILE` |
| `anvilkit-agent-control` | `grpc` (listen, control/execution capacity, shutdown), `temporal` (address, namespace, task queue), `relay.interval`, `profiles.local_check_deadline`, `dispatch` (authority freshness; `development` DEVELOPMENT_ONLY prices — a model price binds its route's trusted `provider`/`model` — route authorizations and not-sent issuers, file-only, off by default) | `ANVILKIT_CONTROL_LISTEN`, `ANVILKIT_CONTROL_DATABASE_URL` (secret), `ANVILKIT_CONTROL_INVENTORY_DIR`, `ANVILKIT_CONTROL_TEMPORAL_ADDRESS` |
| `anvilkit-agent-workflow` | `temporal` (address, namespace, both task queues, identity, build id), `control.address`, `kubernetes.namespace`, `execution` (Control activity timeout/retries, launch window, observation bounds, `cleanup` timeout/attempts/settle window/reconciliation pacing and bound), `development` (DEVELOPMENT_ONLY fault injection, file-only), `shutdown_timeout` | `ANVILKIT_WORKFLOW_TEMPORAL_ADDRESS`, `ANVILKIT_WORKFLOW_CONTROL_ADDRESS`, `ANVILKIT_WORKFLOW_KUBECONFIG`, `ANVILKIT_WORKFLOW_LAUNCH_BACKEND`, `ANVILKIT_WORKFLOW_BUILD_ID` |

The Workflow's `execution` section is recorded into every run's history at its start (`workflow.SideEffect`), so a redeployed worker with different values never changes the replay of an existing run. Apollo generations, reload and secret rotation are P14 work on top of these files.

| Listener | Address | Identity |
| --- | --- | --- |
| PostgreSQL | `127.0.0.1:25432` | `anvilkit_{control,knowledge,mcp}_{app,migrator}`, `temporal`; app roles have no DDL |
| Temporal frontend | `127.0.0.1:27233` | namespace `anvilkit`, no TLS (mTLS is an ENV-03 deployment input) |
| Kubernetes | kind `anvilkit-dev`, `.local/dev/launcher.kubeconfig` | ServiceAccount `anvilkit-agent-workflow`: Jobs create/get/list/watch/delete and Pods read in `anvilkit-components` only |

## Running the LocalCheck chain by hand

```sh
. .local/dev/env.sh
(cd services/anvilkit-agent-control && go run ./cmd/anvilkit-agent-control) &
(cd services/agent/api && ANVILKIT_API_LISTEN=127.0.0.1:9100 go run ./cmd/anvilkit-agent-api) &
(cd services/anvilkit-agent-workflow && go run ./cmd/anvilkit-agent-workflow) &
curl -s -X POST http://127.0.0.1:9100/api/v1/operations -H "Authorization: Bearer $ANVILKIT_DEV_API_TOKEN_A" \
  -H 'Content-Type: application/json' \
  -d '{"commandId":"cmd-1","kind":"local_check","subject":{"profileId":"local-check-v1","subjectDigest":"sha256:0dc7fa9db7237a2b5c96f70f59bb00f73bb86a0ca5554e91c312f9ada26e18b3"}}'
curl -N http://127.0.0.1:9100/api/v1/operations/<operationId>/events -H "Authorization: Bearer $ANVILKIT_DEV_API_TOKEN_A"
```

The same chain is exercised automatically by `tests/integration` (`go test -tags integration ./...`
with the environment sourced), which builds and runs the three binaries itself (the API from its own
repository at `services/agent/api`), pointing them at the checked-in `config.yaml` files; `TestFaultScenarios` and `TestUnresolvedCreateRecovery` run the worker
with temporary copies of its file that enable `development.lose_receipt_once` /
`development.hold_until_canceled_once` and shorten or widen the cleanup/reconciliation bounds;
`TestDispatchAdmission` runs two Control processes with a temporary copy of Control's file that enables
the `dispatch.development` fixtures.

## The API in the kind cluster

`sh deploy/dev/api-chart.sh install` builds the API image from `services/agent/api` alone (the
API repository; the build resolves the published contracts module `go/v0.1.0` through the public
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

Pinned images: `postgres:17-alpine@sha256:18cfe3ef…`, `temporalio/server:1.31.2@sha256:6b02e517…`,
`temporalio/admin-tools:1.31.2@sha256:dbc5fcd6…`; kind 0.33.0 (node image resolved by kind).
