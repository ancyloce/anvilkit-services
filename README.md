# AnvilKit Services

AnvilKit Services is the repository for the AnvilKit Agent platform: a self-hosted system that turns a marketing team's requirement into complete, editable static-component source, certifies and approves an exact release, publishes it to npm and browser/CSS targets, activates it, and keeps saved pages locked to the exact release they used. The platform also includes bounded multi-agent execution, knowledge retrieval (RAG, vectors, long-term memory) and a reviewed MCP tool marketplace.

The architecture documents under [`docs/architecture/`](docs/architecture/architecture.md) are the source of truth. This README summarizes them and records what the checkout actually contains; it does not restate the design. Repository working rules for people and development agents are in [`AGENTS.md`](AGENTS.md).

## Status at a glance

| Aspect | Current state |
| --- | --- |
| Architecture baseline | Architecture document edition **V4.0, revision 2** — document metadata only, never a service, database, directory or release suffix ([naming](docs/architecture/architecture.md#naming)) |
| Decision | Rebuild the owned application implementation completely with the fixed technology selections ([ADR-001](docs/architecture/architecture.md#adr-001)); existing code in this checkout is outside the new build closure |
| Design status | Detailed design: DD-01 through DD-10 and ADR-001 through ADR-004 are adopted design decisions, not implemented behavior |
| Implementation status | R0 contract baseline (P01–P02), a DEVELOPMENT_ONLY foundation (P03), the LocalCheck control chain across API, Control and Workflow (P04–P05) and shared budgets with single-use dispatch admission (P06, corrected 2026-09-15) exist and are locally verified; the contracts and the API live in their own repositories since 2026-09-15 and the contracts are published (`anvilkit-agent-contracts` `main` `f2cee94`, Go module tag `go/v0.1.0`, CI green); P07–P24 not started ([unit status records](docs/architecture/delivery.md#unit-status-records)) |
| Runtime qualification | All fourteen runtime gates G-01 through G-14 are `NOT_RUN`; the release qualification record is `NOT_VERIFIED` ([acceptance](docs/architecture/delivery.md#acceptance), [qualification lock](docs/architecture/platform.md#deployment-inputs)) |
| Pending inputs | Environment inputs ENV-01 through ENV-10 (machines, failure domains, prices/caps, IdP/Pagix/Studio contracts, RPO/RTO, on-call) are required before release and do not reopen technology selection ([deployment inputs](docs/architecture/platform.md#deployment-inputs)) |
| Initial versions | REST/SSE and Protobuf protocols begin at **v1**; the first stable application release target is **1.0.0** |

Adopted design decisions, observed implementation state, pending inputs and runtime qualification evidence are four different things throughout this repository. Historical approvals and proofs under `docs/archive/` (ignored by Git, present only in the original working copy) belong to the previous architecture and do not qualify the replacement.

## First milestone

The first milestone delivers one static component through the complete business chain. Each step is a separate lifecycle with its own identity, resources and clock ([lifecycles](docs/architecture/architecture.md#diagram-lifecycles)):

1. **Prepare** — a prompt becomes bounded structured requirements; at most two grouped clarification rounds of at most three questions each; selected sources, brand and assets freeze into a brief with content digests (M1-15 to M1-17).
2. **Generate** — a bounded agent team produces complete, editable component source under one shared operation budget; Pi is the only source writer (M1-02, M1-03, M1-11).
3. **Edit and preview** — conditional source save with expected revision; preview of exactly that revision in an isolated, separate-origin sandbox (M1-04).
4. **Certify** — an independent Validator with a trusted observer certifies npm, browser module, CSS and Puck/host compatibility; candidate reports never certify (M1-05).
5. **Approve** — a maintainer approves an exact `ReleaseSubject` binding source, version, artifact hashes, profiles and evidence; any changed field needs a new decision (M1-06).
6. **Publish and activate** — npm and browser/CSS publish through Pagix with both receipts verified before activation; a single `UNKNOWN` target keeps the release uncertain (M1-07, M1-08).
7. **Use in Studio** — new compatible components load without rebuilding the host; saved pages keep the exact release lock and never silently upgrade (M1-09, M1-10).

Operations are tenant-scoped, observable, cancellable and recoverable under original identities across replicas and failures (M1-01, M1-12 to M1-14). The seven capability requirements CAP-01 through CAP-07 (multi-agent, RAG/vectors, long-term memory, MCP marketplace, queue/events, Fx/configuration, Kubernetes) are part of the final scope; staging the work does not remove them. Details: [requirements](docs/architecture/requirements.md) and [user scenarios](docs/architecture/requirements.md#scenarios).

## Target architecture

The target is eight long-running application services on Kubernetes ([service catalog](docs/architecture/architecture.md#services)). All eight are **planned**; see [Repository state](#repository-state) for what exists.

| Service identity | Technology | Responsibility |
| --- | --- | --- |
| `anvilkit-agent-api` | Go, Gin | Authentication, OpenAPI validation, command mapping, authorized queries, SSE; no business database or provider key |
| `anvilkit-agent-control` | Go, gRPC, Fx | Admission, cost, execution and effect registries, artifacts, projections, Temporal relay; sole writer of the Control database |
| `anvilkit-agent-workflow` | Go, Temporal SDK | Fixed workflows (Preparation, Generation, PreviewBuild, Release, LocalCheck), bounded Runner, Activities, launch adapters |
| `anvilkit-agent-model-proxy` | TypeScript, pi-ai | Controlled provider adapter, stream normalization, usage capture; sole runtime holder of model provider keys |
| `anvilkit-agent-knowledge` | TypeScript, LangChain, native Qdrant client | Sources and ACL, ingestion, snapshots, retrieval, `MemoryFact`, projections, background tasks |
| `anvilkit-agent-mcp` | Go, official MCP SDK, Casbin | Reviewed catalog, grants, tool effects, protocol and SSRF policy |
| `anvilkit-agent-background-worker` | TypeScript, BullMQ | Claims durable Knowledge/MCP requests, runs bounded handlers, submits results; no domain database |
| `anvilkit-agent-inference` | Python, FastAPI, FlagEmbedding | Fixed embedding and reranking models with bounded batching; no business database or Qdrant access |

Codegen, Validator, Preview, Docling parser and migration are short-lived Job classes, not services. Temporal, PostgreSQL (CloudNativePG), Qdrant, Valkey, NATS JetStream, Apollo, OpenBao, RKE2/Cilium/Envoy Gateway and Rook-Ceph are infrastructure. Pagix keeps commercial, source, review, publication and activation authority; Studio keeps the page host and page writes; both are reached only through their authenticated APIs.

Governing decisions the entry points must not contradict:

- **Complete replacement.** New v1 contracts, empty target databases with `00001_init.sql` migrations, new Temporal histories and newly generated clients. Shared names never authorize reuse of old handlers, DTOs, SQL or executors. Business records or unresolved external obligations move only when an actual inventory shows they must, through separately verified import and handover work ([delivery](docs/architecture/delivery.md#implementation)).
- **Fixed technology, conditional alternatives.** The [60-decision matrix](docs/architecture/technology.md#choices) fixes each primary and its alternative. Primary-product replicas, quorum and failover provide high availability; an alternative is a planned replacement requiring migration and requalification, never an automatic heterogeneous standby.
- **Single authorities.** Temporal owns business progression; LangGraph coordinates bounded collaboration inside one execution attempt; Pi is the sole source-code writer; Control owns admission, costs, external effects and result acceptance ([ADR-002](docs/architecture/architecture.md#adr-002)). Knowledge owns source authorization and `MemoryFact`; MCP owns the reviewed catalog and explicit grants with the Control revocation barrier. Vector indexes, PostgresStore, caches and queues are rebuildable projections, never authorization or business-fact authorities.
- **Untrusted content and single-use sends.** Candidate code and retrieved or tool-returned content are untrusted; provider keys live only in Model Proxy and job scope credentials only in the trusted sidecar; validation is independent; every physical model or tool send needs its own single-use Control admission; `UNKNOWN` outcomes are reconciled under their original identity, never resent under a new one ([security](docs/architecture/security.md), [ADR-003](docs/architecture/architecture.md#adr-003)).

<a id="repository-state"></a>

## Repository state

Verified against the checkout on 2026-09-15. Inspect the tree before relying on this table; it records observed state, not the target. Repository ownership (parent, contracts repository, service repositories) is defined in [architecture](docs/architecture/architecture.md#repositories).

| Path | Observed content | Relation to the V4.0 target |
| --- | --- | --- |
| `docs/architecture/` | Eleven architecture documents plus [`docs/README.md`](docs/README.md); tracked (only `docs/archive/` stays ignored) | Current authority |
| `docs/archive/` | Previous architecture (v3.12), its contracts (`docs/archive/contracts/`) and tools (`docs/archive/tools/`) | Historical reference only; its approvals and test results do not carry over |
| `contracts/` | The `anvilkit-agent-contracts` repository as a submodule (registered by parent commit `92e1edd`; its content committed and pushed on `main` on 2026-09-15 — `0235295` is the tagged content, `f2cee94` adds the `verify.py` install-order fix and is the commit the gitlink records): `openapi/`, `proto/anvilkit/{control,knowledge,mcp}/v1` with `buf.yaml`, `jobs/`, `components/`, `events/` sources with fixtures; the generated consumers `go/` (module `github.com/ancyloce/anvilkit-agent-contracts/go`), `ts/` (`@anvilkit/generated-clients`), `python/`; `tests/go`; its own `tools/` and CI ([overview](contracts/README.md)) | Initial v1 sources and their generated consumers, one authority ([contract sources](docs/architecture/contracts.md#1-contract-sources-and-generated-bindings)); consumed as versioned packages: the Go module is published as tag `go/v0.1.0` (commit `c377f79`, byte-identical tree to `main`; module hash `h1:2vk5EHKnTRoCpW5EHeMGWzfCp//M5MQ+EDWxRlNqXKc=`, served by `proxy.golang.org` and recorded by the checksum database); the TypeScript/Python packages have no service consumer until P11/P15/P18/P20 |
| `services/agent/api` | The `anvilkit-agent-api` repository (submodule, history retained): the replacement API (Gin/oapi-codegen strict server + gin-contrib/sse, Fx, one typed `internal/config` and one secret-free `config.yaml`), its `Dockerfile`, CI and Helm chart `deploy/chart` ([overview](services/agent/api/README.md)); its `main` (`c4f5adb`, pushed 2026-09-15) holds the replacement including the Fx entry point `cmd/anvilkit-agent-api/main.go`, `README.md` and `deploy/chart/`, and requires `anvilkit-agent-contracts/go v0.1.0` with the published module hash; a clean clone of that commit builds, tests and reproduces the image `sha256:ca2ac6a9…` from the public proxy alone; the parent's gitlink records the commit it builds | Replacement implementation in its final location; builds from its repository alone against the published contracts module (public proxy, no replace directive, no workspace) |
| `services/anvilkit-agent-control`, `-workflow` | Go modules (Fx, grpc-go, pgx/sqlc, Temporal SDK, client-go) implementing the LocalCheck control chain and, in Control, budgets and single-use dispatch admission; each with one typed `internal/config` and one secret-free `config.yaml`; they require the contracts module and resolve it through the root `go.work` | Replacement implementation, still hosted here; final locations `services/agent/{control,workflow}` (their own repositories) |
| `jobs/migration` | goose/v3 migration Job with the `anvilkit_control` (at `00004`), `anvilkit_knowledge`, `anvilkit_mcp` schemas | Migration Job class and current migration-version authority; the SQL moves to its owning service with that service's extraction |
| `tests/contracts`, `tests/integration` | The build-closure test; end-to-end LocalCheck and dispatch proofs on the dev foundation (`ANVILKIT_INTEGRATION_API_URL` runs the LocalCheck flows against an API deployed elsewhere) | Executable cases of the plan |
| `deploy/dev/` | Compose (PostgreSQL 17, Temporal 1.31.2), kind cluster and launcher RBAC, `up.sh`/`down.sh`; `values/anvilkit-agent-api.yaml` (parent-owned development values) and `api-chart.sh` (installs the API's chart into the kind cluster) | DEVELOPMENT_ONLY foundation (P03); not HA, not RKE2 |
| `services/agent/control`, `services/agent/workflow` | Git submodules with the previous architecture's Go modules (Connect RPC) | Legacy, outside `go.work` and the build closure; replaced when Control and Workflow are extracted |
| `services/agent/model-proxy`, `jobs/shared/access-sidecar` | Git submodules containing README and LICENSE only | Planned; no implementation |
| `deploy/local/`, `compose.yaml`, `tools/prepare-local-compose.py` | Docker Compose profile of the legacy local-check flow (PostgreSQL 18.6, Temporal 1.31.2), mounts repointed to `docs/archive/contracts/`; the legacy API service pinned to its own image (`legacy-20260915`) and the preserved legacy worktree under `.local/legacy/` | Legacy environment; not handed over, not a startup procedure for the replacement |
| `packages/biome-config`, `packages/typescript-config`, `apps/` | pnpm/Turborepo starter | Starter only; TypeScript services arrive with P11/P15 |

Not present in any form: `anvilkit-agent-model-proxy`, `-knowledge`, `-mcp`, `-background-worker`, `-inference`, the `jobs/{codegen,validator,preview,parser}` classes, `packages/profile-schemas` and `deploy/{gitops,policies}`.

## Reading route

Start with [`docs/README.md`](docs/README.md), then read in this order:

| Document | Read it for |
| --- | --- |
| [Architecture](docs/architecture/architecture.md) | Scope, the eight services and dependency direction, lifecycles, naming and initial versions, ADR-001 to ADR-004, terminology |
| [Requirements](docs/architecture/requirements.md) | Product profile, M1-01 to M1-17, CAP-01 to CAP-07, UI constraints, scenarios |
| [Technology](docs/architecture/technology.md) | The 60 primary/alternative choices, inherited version targets, licensing boundaries |
| [Contracts](docs/architecture/contracts.md) | Contract sources, public endpoints, internal method groups, values and errors, events/SSE, data ownership, lock ranks |
| [Execution](docs/architecture/execution.md) | DD-01 Workflow, DD-02 Control and single-use dispatch, DD-03 agent harness and isolation |
| [Components](docs/architecture/components.md) | DD-04 build and certification, DD-05 Studio, DD-06 Pagix ports |
| [Knowledge](docs/architecture/knowledge.md) | DD-07 ingestion, vectors, retrieval, memory facts |
| [MCP](docs/architecture/mcp.md) | DD-08 catalog, grants, revocation barrier, tool calls, SSRF |
| [Platform](docs/architecture/platform.md) | DD-09 queues/events/Fx/configuration, DD-10 Kubernetes/HA/recovery, ENV inputs, operations |
| [Security](docs/architecture/security.md) | Trust regions, SEC-01 to SEC-12, logging and deletion, supply chain |
| [Delivery](docs/architecture/delivery.md) | Stages R0–R6, definition of complete replacement, gates G-01 to G-14, traceability, evaluation |

## Commands

Checked on 2026-09-14. None of them qualifies a G gate.

| Command | Effect | Prerequisites | Writes files |
| --- | --- | --- | --- |
| `sh tools/verification-env.sh [--static \| --only STEP]` | Creates or reuses `.local/verification-venv` (CPython 3.12, `tools/requirements.txt` fully pinned), installs the Python consumer package in place, then runs `tools/run-verification.py` with the given arguments | CPython 3.12 (`ANVILKIT_PYTHON` may name it), network for the first install | `.local/verification-venv/` only |
| `python3 tools/check-docs.py` | Current-baseline Markdown check (fences, tables, links, anchors); missing required inputs fail | Python 3 (standard library) | No |
| `python3 tools/check-contracts.py [--against REF]` | Delegates to `contracts/tools/check.py`: `buf lint`, OpenAPI validation, JSON Schema metaschema + fixtures, OpenAPI vectors of all three specs, RPC vector message names (`--against` names a ref of the contracts repository) | The pinned toolchain (`.local/verification-venv/bin/python`); any other interpreter exits 2 UNEXECUTED with the install command; buf 1.73.0 | No |
| `python3 tools/check-source-export.py [--keep DIR]` | Git export set (the parent's tracked + untracked-not-ignored files plus the contracts and API repositories' own listings) holds every declared input and no archive/secret/legacy-submodule content; a disposable copy passes the docs/contract checks and builds every `go.work` module on its own | The pinned toolchain, Go, buf | Temp dir only (or `--keep`) |
| `python3 tools/generate-contracts.py [--check] [--only contracts\|sqlc]` | Two separate generators: the contract bindings through `contracts/tools/generate.py` (Go, TypeScript, Python consumers of the contracts repository) and Control's sqlc output from its queries and the Control migrations; `--check` diffs each against a scratch tree | The pinned toolchain; buf, protoc-gen-go, protoc-gen-go-grpc, oapi-codegen, sqlc in `GOPATH/bin`; `pnpm install --frozen-lockfile` done in `contracts/` — versions in `contracts/README.md` | Without `--check`: yes |
| `sh tools/verification-env.sh` in `contracts/` | The contracts repository's own chain (check, generate `--check`, go with `GOWORK=off`, ts, python), reading nothing outside that directory | CPython 3.12, Go, buf and the generators, Node.js 24 + pnpm | `contracts/.local/` only |
| `python3 tools/run-verification.py [--static]` | docs → generate `--check` → contracts → export → ts (`pnpm install --frozen-lockfile` in `contracts/`, tsc, Biome, Vitest) → python (pytest in `contracts/python`) → `go build/vet/test` per `go.work` module → integration (needs the dev foundation) | The pinned toolchain, Go 1.26+, Node.js 24 + pnpm (`packageManager`), Docker for Testcontainers unless `--static` | No (scratch only) |
| `sh deploy/dev/api-chart.sh [install\|uninstall]` | Builds the API image from `services/agent/api` alone, loads it into the kind cluster, creates the principals Secret from `.local/dev/api-principals.json` and installs the API's chart with the parent's development values (`deploy/dev/values/anvilkit-agent-api.yaml`) | The dev foundation, Docker, kind, kubectl, helm; the image build resolves the published contracts module through the public proxy (`ANVILKIT_DEV_GOPROXY`/`ANVILKIT_DEV_GONOSUMDB` only for a private proxy); a rebuilt image rolls the Pods through the `anvilkit.io/image-id` Pod annotation | The kind cluster only |
| `sh deploy/dev/up.sh` / `sh deploy/dev/down.sh` | DEVELOPMENT_ONLY foundation: PostgreSQL 17, Temporal, migrations (forward, in place), kind cluster, launcher identity; `env.sh` exports the placement overrides and each service's `ANVILKIT_*_CONFIG` path | Docker, kind, kubectl, Go | `.local/dev/` only |
| `go test -tags integration ./...` in `tests/integration` | End-to-end LocalCheck on the dev foundation | `. .local/dev/env.sh` | Temp dirs only |
| `GOWORK=off GOFLAGS=-mod=readonly go build ./... && go vet ./... && go test ./...` in `services/agent/api` | Builds, vets and tests the API from its repository alone against the published contracts module (`go/v0.1.0` from `proxy.golang.org`, checksum-database verified); `docker build .` and `helm lint deploy/chart` likewise | Go 1.27, Docker, helm | Go build cache only |
| `GOWORK=off go build -mod=readonly ./...` in each legacy submodule (`services/agent/{control,workflow}`) | Builds the legacy service module | Go 1.27 | Go build cache only |

## Known limitations for the next phase

- **Development-only components.** The filesystem inventory adapter in Control, the bearer-token fixture verifier in the API, plaintext loopback gRPC and the kind/Compose topology exist only for development; ENV-02/03/07 inputs replace them in P07 and P22/P23.
- **Gates.** G-01 to G-14 remain `NOT_RUN`; the passing tests recorded in [delivery.md](docs/architecture/delivery.md#unit-status-records) are unit verification of P01–P05.
- **Not yet implemented.** External effects and the RGW inventory (P07), artifacts (P08), Job isolation (P09) and everything from P10 on; the API answers `DEPENDENCY_UNAVAILABLE` for those paths. Budgets and single-use dispatch (P06) exist with DEVELOPMENT_ONLY prices, route authorizations and not-sent evidence (ENV-06/ENV-07); no paid route is enabled. An operation whose Job cleanup could not be confirmed is closed `reconciling` with its cancel `pending` while its own LocalCheck run keeps reconciling the original launch key until the backend evidence settles it; only past the run's configured bound (`execution.cleanup.reconcile_max_duration`, default 24 h) does the run fail visibly and leave the record for the P07 recovery epoch and operator dispositions. Accepted results keep their original bytes (`stage_manifests.result_manifest_bytes`, verified on read); stages accepted before migration `00002` have no original bytes and are never backfilled. The generated TypeScript/Python consumers have no service consumer yet (P11/P15/P18/P20). Apollo generations, reload and secret rotation arrive with P14 on top of the per-service configuration files.
- **Git baseline.** The contracts are published: `anvilkit-agent-contracts` `main` `f2cee94` holds the sources, consumers, fixtures and tools (its CI is green), the tag `go/v0.1.0` (commit `c377f79`, the same tree as `0235295`) is the recorded `buf breaking` baseline (`contracts/tools/check.py --against go/v0.1.0`) and the Go module is served by the public proxy. The API repository's `main` and the parent's gitlinks and inputs are committed as recorded in the [cleanup record](docs/architecture/delivery.md#cleanup-record) (ninth pass); the earlier draft PR #1 / branch `publish/v1-contract-baseline` of the contracts repository duplicate `main` and can be closed. Retired with the publication: the local module proxy `.local/goproxy/` (a copy stays in the [pre-publication snapshot](docs/architecture/delivery.md#source-snapshot)). A source snapshot is never a substitute for a commit.
