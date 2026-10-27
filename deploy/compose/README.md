# Compose placement (one server, DEVELOPMENT_ONLY)

The replacement system on one server with Docker Compose: one instance of each of
the eight services, the Knowledge forwarder and the two owner relays, the foundation
of `deploy/dev/compose.yaml` (PostgreSQL 17, Temporal 1.31.2, MinIO, NATS JetStream,
two Valkey instances, Qdrant 1.19.0, the development OIDC issuer), the four migration
one-shots, mTLS between the services with SPIFFE authorization, secrets as restricted
files. The services never run in Kubernetes; Jobs run on a Jobs-only RKE2 node on
the same host (below, plan 0006 option B). It is not HA, not the qualified platform
(`deploy/qualification`), not production; every G gate stays `NOT_RUN`.

## Images

Only CI-published images are deployed. `.github/workflows/release.yml` builds the ten
owned images from a `v*` tag of this repository (every submodule at its recorded
gitlink), scans, signs and pushes them by digest to the release registry
(`ANVILKIT_RELEASE_REGISTRY`, a repository variable, for example `ghcr.io/<owner>`),
and attaches `release-manifest.json` to the tag's GitHub release. `images.sh` turns
that manifest into `images.env`, which `compose.yaml` reads:

```sh
gh release download <tag> --pattern release-manifest.json --dir /tmp/release-<tag>
sh deploy/compose/images.sh /tmp/release-<tag>/release-manifest.json > deploy/compose/images.env
```

`images.sh` refuses a `BLOCKED` manifest, findings, a manifest pushed to the
development registry and any reference that is not a digest under the manifest's
registry. The foundation images are the digests of `deploy/dev/compose.yaml`.

## Server layout

| Path | Content | Mode |
|---|---|---|
| `~/anvilkit/deploy/compose/` | this directory (compose.yaml, config/, images.env, scripts) | checkout of the deployed commit, configuration only |
| `~/anvilkit/deploy/dev/` | the setup scripts and reviewed files the stack mounts (`postgres-init.sh`, `minio-setup.sh`, `nats-setup.sh`, `nats/`, `temporal-setup.sh`, `temporal-dynamicconfig.yaml`, `oidc/config.json`) | configuration only |
| `~/anvilkit/deploy/policies/`, `~/anvilkit/services/agent/{workflow,knowledge}/deploy/chart/` | the Job admission chart and default-deny policies; the two service charts whose launcher ServiceAccount and Role `jobs-node.sh identities` renders | configuration only |
| `/srv/anvilkit/secrets/` | generated credentials and DSNs (`up.sh secrets`), the API's fixture principals, the Workflow's and Knowledge's launcher kubeconfigs (`jobs-node.sh identities`), optionally `model-proxy-credentials.env` | directory 0700, files 0644 (read by the containers' own users through bind mounts) |
| `/etc/rancher/rke2/`, `/etc/systemd/network/50-anvilkit-jobs0.*`, `/etc/anvilkit/guard.nft` | the Jobs node's RKE2 configuration (admin kubeconfig 0600), the host-only interface, the inbound guard | root |
| `/srv/anvilkit/certs/` | `ca/` and one directory per principal (`tls.crt`, `tls.key`, `ca.crt`) from `pki.sh` | directory 0700, principal directories 0755 |
| Docker volumes `anvilkit_*` | `postgres-data`, `minio-data`, `nats-data`, `valkey-queue-data`, `qdrant-data`, `pagix-double` (owned by the Workflow's user 65532 through the `pagix-double-init` one-shot) | persistent; `down.sh` keeps them |

## Operations

```sh
export ANVILKIT_HOME=/srv/anvilkit
sh deploy/compose/up.sh                  # secrets, PKI, pull, foundation, setup one-shots, migrations, services, status
sh deploy/compose/up.sh budget           # current + next month budget pools of tenant_a/user_a (once a month)
sh deploy/compose/up.sh status           # docker compose ps
sh deploy/compose/pki.sh verify          # every leaf chains to the CA; SANs printed
sh deploy/compose/down.sh                # stop and remove containers; volumes, secrets and certificates stay
docker compose --project-directory deploy/compose --env-file deploy/compose/images.env logs --since 10m anvilkit-agent-control
```

Every service container carries `stop_grace_period` equal to its chart's
`terminationGracePeriodSeconds` (Model Proxy 360 s, MCP 130 s, Knowledge and its
forwarder 90 s, background worker and relays 60 s, Workflow 45 s, Inference 40 s,
Control and API 30 s), so a stop or recreate lets the drain finish; Docker's default
of 10 s would cut admitted model sends.

Published ports, all on `127.0.0.1` of the server (reach them through
`ssh -L 9100:127.0.0.1:9100 ubuntu@<server>`): API `9100`; operations: PostgreSQL
`25432`, Temporal `27233`, MinIO `29000`, OIDC `25556`. Every other port is private to
the Compose network. The API authenticates `auth.mode: fixture` bearer tokens from
`/srv/anvilkit/secrets/api-principals.json` (DEVELOPMENT_ONLY; ENV-07 supplies the
production issuer).

## Local Studio

The Studio is tested from a local checkout (default `../anvilkit-studio` next to this
repository, dependencies already installed), never from this server:

```sh
sh deploy/compose/studio-local.sh up       # tunnel, minio alias, token copy, Studio dev server
sh deploy/compose/studio-local.sh status
sh deploy/compose/studio-local.sh down     # stops all of it, removes the alias line and the token copy
```

`up` opens an SSH tunnel (local `9100` to the API, local `9000` to MinIO, because the
signed upload URLs Control hands out name `minio:9000`), adds `127.0.0.1 minio` to
`/etc/hosts` when it can, copies the `tenant_a` fixture token to a `0600` file under
`~/.local/state/anvilkit-studio-local/`, and starts `pnpm dev` with
`ANVILKIT_AGENT_API_URL` and `ANVILKIT_AGENT_API_TOKEN` only. The browser calls the Studio's
own relay; it never sees the token or the API address. Open `http://localhost:3000/agent/…`
(`localhost`: Next.js blocks its development resources for other hosts). The Studio
relay has no user authentication of its own, so keep it on the local machine.

## Budget pools

Control funds every Preparation (2 USD reserved) and Generation (3 USD reserved,
`profiles.*.funding` in `config/anvilkit-agent-control.yaml`) from a current platform
pool, a tenant pool and an actor pool; with any of them missing it refuses the
operation with `BUDGET_EXHAUSTED` before a model is called. Unspent reservations
return at settlement. `up.sh budget` (the `control-budget-setup` one-shot,
`control-budget-pools.sql`) creates those pools for the fixture principal
(`tenant_a`, `user_a`) for the current and the next calendar month (UTC), keeps
existing pools unchanged, and must run once a month. Caps are scale-6 USD
amounts (`1000000` = 1 USD) from the shell that runs it:

| Variable | Default | Pool |
|---|---|---|
| `ANVILKIT_BUDGET_PLATFORM_CAP` | `20000000` (20 USD) | platform |
| `ANVILKIT_BUDGET_TENANT_CAP` | `10000000` (10 USD) | tenant `tenant_a` |
| `ANVILKIT_BUDGET_ACTOR_CAP` | `10000000` (10 USD) | actor `user_a` |

**Open decision (owner):** the product has no pool administration path. Control's
`BudgetPoolService.CreatePool` is allowed only for the API's identity, and the API
exposes no route for it, so this placement writes the rows `CreatePool` would write,
as Control's application role. An operator route (or a Control administration tool)
replaces this step once it exists.

## Jobs node

Owner decisions (plan 0006): option B with the D-2 exception (one RKE2 node on this
host, Job namespaces only, never the services), D-4 development profiles first (no
gVisor yet: `local-check-v1`, `validator-source-dev-v1`, `codegen-team-dev-v1`,
`parser-docling-dev-v1`), D-5 one generation at a time (a `pods: 1` quota per Job
namespace). `jobs/jobs-node.sh` installs and configures it, in this order, before the
Compose services that publish on its address start:

```sh
sh deploy/compose/jobs/jobs-node.sh host            # anvilkit-jobs0 (10.255.255.1) and the inbound guard
sh deploy/compose/jobs/jobs-node.sh guard-confirm   # from a new ssh connection, within 3 minutes
sh deploy/compose/jobs/jobs-node.sh install         # RKE2 v1.36.4+rke2r1 (Cilium) and Helm, pinned
sh deploy/compose/jobs/jobs-node.sh cluster         # Kyverno 1.19.1, admission chart, namespaces, relay, policies
sh deploy/compose/jobs/jobs-node.sh identities      # launcher identities and kubeconfigs, sidecar identity
sh deploy/compose/jobs/jobs-node.sh probe           # DNS, relay and refusals from a pod with the Job policies
```

How the Job pods reach the services: Control (9101), the Model Proxy (9103) and MinIO
(9000) publish on the host-only address 10.255.255.1 (a dummy interface; nothing
outside the host routes to it). In the cluster, a TCP relay (`anvilkit-gateway`,
HAProxy) forwards the unchanged mTLS streams there, and ExternalName Services give it
the Compose names the sidecar and the presigned URLs use. The Job namespaces keep
default deny plus egress to CoreDNS and to the relay on the flows the qualified topology
names (`deploy/qualification/platform/network`). The launchers in the Workflow and
Knowledge containers reach the API server as `anvilkit-jobs-api` (10.255.255.1:6443)
with one-year ServiceAccount tokens limited to their namespace and to the Roles their
charts own (the Workflow: Jobs and reading Pods; Knowledge: Jobs and the Secrets of its
parser Jobs; neither lists Secrets, creates Pods or execs). The access sidecar's identity is the `pki.sh`
leaf, copied into the `anvilkit-job-access-sidecar-identity` Secret. The inbound guard
drops new connections on `eth0` except SSH, ICMP and DHCP, so the node's listeners
(6443, 9345, 10250, etcd) stay private.

After `pki.sh` renews the sidecar leaf, or before the launcher tokens expire, run
`jobs-node.sh identities` again and recreate `anvilkit-agent-workflow` and
`anvilkit-agent-knowledge`. The MCP and Knowledge relays of the sidecar stay off (their
allowlists do not admit the sidecar here).

## Known blockers of this placement

- **B-1 Job execution.** Addressed by the Jobs node (above) for the development profiles;
  the gVisor production profiles (`codegen-*-v1`, `validator-source-v1`,
  `preview-build-v1`, `parser-docling-v1`) stay unavailable until gVisor is installed and
  qualified on the node (plan 0006 D-4, next step). Without the node (`verify.sh` reports
  it), every Job launch fails at `CreateJob`.
- **B-2 Provider credential.** The Model Proxy's route `controlled-openai-v1` (DeepSeek
  `deepseek-flash`) is enabled in `config/anvilkit-agent-model-proxy.yaml` with the unpaid
  `health_probe: models`, and is served only where the operator wrote
  `/srv/anvilkit/secrets/model-proxy-credentials.env` (`ANVILKIT_MODEL_PROXY_CREDENTIAL_DEEPSEEK=…`,
  mode 0600, written by hand, never by `up.sh`, never through Git or an agent session).
  Without that file the proxy logs `route not served` and refuses every model call
  (`PROFILE_UNQUALIFIED`). `verify.sh` reports the route state and the probe from the
  proxy's log; a paid call (a Preparation) needs the owner's explicit authorization.
  Changing the credential: replace the file, then
  `docker compose … up -d --force-recreate anvilkit-agent-model-proxy`.
- **B-3 ContextForge.** The MCP service runs without a ContextForge placement (none is
  deployed here): catalog discovery is unavailable; grants and the revocation barrier work.
- **B-4 Studio.** Deferred by owner decision (2026-10-25): the Studio is not deployed as
  a Compose service; it runs from a local checkout against this placement
  (`studio-local.sh`, below). Its image is published by its own repository
  (`anvilkit-studio`, `.github/workflows/docker-images.yml`, `ghcr.io/<owner>/studio`).
