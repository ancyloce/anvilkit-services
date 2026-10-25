# Compose placement (one server, DEVELOPMENT_ONLY)

The replacement system on one server with Docker Compose: one instance of each of
the eight services, the Knowledge forwarder and the two owner relays, the foundation
of `deploy/dev/compose.yaml` (PostgreSQL 17, Temporal 1.31.2, MinIO, NATS JetStream,
two Valkey instances, Qdrant 1.19.0, the development OIDC issuer), the four migration
one-shots, mTLS between the services with SPIFFE authorization, secrets as restricted
files. No Kubernetes cluster is involved. It is not HA, not the qualified platform
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
| `/srv/anvilkit/secrets/` | generated credentials and DSNs (`up.sh secrets`), the API's fixture principals, the Workflow's launcher kubeconfig, optionally `model-proxy-credentials.env` | directory 0700, files 0644 (read by the containers' own users through bind mounts) |
| `/srv/anvilkit/certs/` | `ca/` and one directory per principal (`tls.crt`, `tls.key`, `ca.crt`) from `pki.sh` | directory 0700, principal directories 0755 |
| Docker volumes `anvilkit_*` | `postgres-data`, `minio-data`, `nats-data`, `valkey-queue-data`, `qdrant-data`, `pagix-double` (owned by the Workflow's user 65532 through the `pagix-double-init` one-shot) | persistent; `down.sh` keeps them |

## Operations

```sh
export ANVILKIT_HOME=/srv/anvilkit
sh deploy/compose/up.sh                  # secrets, PKI, pull, foundation, setup one-shots, migrations, services, status
sh deploy/compose/up.sh status           # docker compose ps
sh deploy/compose/pki.sh verify          # every leaf chains to the CA; SANs printed
sh deploy/compose/down.sh                # stop and remove containers; volumes, secrets and certificates stay
docker compose --project-directory deploy/compose --env-file deploy/compose/images.env logs --since 10m anvilkit-agent-control
```

Published ports, all on `127.0.0.1` of the server (reach them through
`ssh -L 9100:127.0.0.1:9100 ubuntu@<server>`): API `9100`; operations: PostgreSQL
`25432`, Temporal `27233`, MinIO `29000`, OIDC `25556`. Every other port is private to
the Compose network. The API authenticates `auth.mode: fixture` bearer tokens from
`/srv/anvilkit/secrets/api-principals.json` (DEVELOPMENT_ONLY; ENV-07 supplies the
production issuer).

## Known blockers of this placement

- **B-1 Job execution.** The Workflow's launcher is the fixed-template Kubernetes port
  (`services/agent/workflow/internal/adapters/kubernetes`); no other execution backend
  exists and no cluster may run here. The Workflow is deployed with a launcher kubeconfig
  naming no reachable API server: it starts, serves the model-call Activities and fails
  every Job launch (LocalCheck, codegen, validator) at `CreateJob`. Generation,
  LocalCheck, preview builds and parsing are therefore unavailable; the Knowledge parser
  profile is empty for the same reason.
- **B-2 Provider credential.** The Model Proxy's route `controlled-openai-v1` is disabled
  in `config/anvilkit-agent-model-proxy.yaml` until the operator delivers
  `/srv/anvilkit/secrets/model-proxy-credentials.env` (`ANVILKIT_MODEL_PROXY_CREDENTIAL_DEEPSEEK=…`)
  and enables the route; every model call is refused (`PROFILE_UNQUALIFIED`) until then,
  and a paid call needs its own authorization.
- **B-3 ContextForge.** The MCP service runs without a ContextForge placement (none is
  deployed here): catalog discovery is unavailable; grants and the revocation barrier work.
- **B-4 Studio.** The Studio is published by its own repository
  (`anvilkit-studio`, `.github/workflows/docker-images.yml`, `ghcr.io/<owner>/studio`) and is
  not part of this stack until that image exists for the wanted commit.
