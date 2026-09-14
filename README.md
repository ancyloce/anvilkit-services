# AnvilKit Services

AnvilKit Services is the repository for the AnvilKit Agent platform: a self-hosted system that turns a marketing team's requirement into complete, editable static-component source, certifies and approves an exact release, publishes it to npm and browser/CSS targets, activates it, and keeps saved pages locked to the exact release they used. The platform also includes bounded multi-agent execution, knowledge retrieval (RAG, vectors, long-term memory) and a reviewed MCP tool marketplace.

The architecture documents under [`docs/architecture/`](docs/architecture/architecture.md) are the source of truth. This README summarizes them and records what the checkout actually contains; it does not restate the design. Repository working rules for people and development agents are in [`AGENTS.md`](AGENTS.md).

## Status at a glance

| Aspect | Current state |
| --- | --- |
| Architecture baseline | Architecture document edition **V4.0, revision 2** — document metadata only, never a service, database, directory or release suffix ([naming](docs/architecture/architecture.md#naming)) |
| Decision | Rebuild the owned application implementation completely with the fixed technology selections ([ADR-001](docs/architecture/architecture.md#adr-001)); existing code in this checkout is outside the new build closure |
| Design status | Detailed design: DD-01 through DD-10 and ADR-001 through ADR-004 are adopted design decisions, not implemented behavior |
| Implementation status | First implementation pass (2026-09-14): R0 contract baseline (P01–P02), a DEVELOPMENT_ONLY foundation (P03) and the LocalCheck control chain across API, Control and Workflow (P04–P05) exist and are locally verified; P06–P24 not started ([unit status records](docs/architecture/delivery.md#unit-status-records)) |
| Runtime qualification | All fourteen runtime gates G-01 through G-14 are `NOT_RUN`; the release qualification record is `NOT_VERIFIED` ([acceptance](docs/architecture/delivery.md#acceptance), [qualification lock](docs/architecture/platform.md#deployment-inputs)) |
| Pending inputs | Environment inputs ENV-01 through ENV-10 (machines, failure domains, prices/caps, IdP/Pagix/Studio contracts, RPO/RTO, on-call) are required before release and do not reopen technology selection ([deployment inputs](docs/architecture/platform.md#deployment-inputs)) |
| Initial versions | REST/SSE and Protobuf protocols begin at **v1**; the first stable application release target is **1.0.0** |

Adopted design decisions, observed implementation state, pending inputs and runtime qualification evidence are four different things throughout this repository. Historical approvals and proofs under [`docs/archive/`](docs/archive/) belong to the previous architecture and do not qualify the replacement.

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

Verified against the checkout on 2026-09-14. Inspect the tree before relying on this table; it records observed state, not the target.

| Path | Observed content | Relation to the V4.0 target |
| --- | --- | --- |
| `docs/architecture/` | Eleven architecture documents plus [`docs/README.md`](docs/README.md); tracked (only `docs/archive/` stays ignored) | Current authority |
| `docs/archive/` | Previous architecture (v3.12), its contracts (`docs/archive/contracts/`) and tools (`docs/archive/tools/`) | Historical reference only; its approvals and test results do not carry over |
| `contracts/` | `openapi/{agent,inference,model-proxy}.yaml`, `proto/anvilkit/{control,knowledge,mcp}/v1/*.proto` with `buf.yaml`, `jobs/`, `components/`, `events/` schemas with fixtures | Initial v1 sources of the replacement ([contract sources](docs/architecture/contracts.md#1-contract-sources-and-generated-bindings)) |
| `packages/generated-clients/go` | Checked-in Go bindings (grpc-go, oapi-codegen, `jobschema`) regenerated by `tools/generate-contracts.py` | Generated, never hand-edited; drift fails `--check` |
| `jobs/migration` | goose/v3 migration Job with `00001_init.sql` for `anvilkit_control`, `anvilkit_knowledge`, `anvilkit_mcp` | Migration Job class; the single migration-version authority |
| `services/anvilkit-agent-control`, `-api`, `-workflow` | New Go modules (Fx, grpc-go, pgx/sqlc, Gin/oapi-codegen, Temporal SDK, client-go) implementing the LocalCheck control chain | Replacement implementation; other endpoints answer `DEPENDENCY_UNAVAILABLE` until their units |
| `tests/contracts`, `tests/integration` | Go consumer tests of the fixtures; end-to-end LocalCheck proof on the dev foundation | Executable cases of the plan |
| `deploy/dev/` | Compose (PostgreSQL 17, Temporal 1.31.2), kind cluster and launcher RBAC, `up.sh`/`down.sh` | DEVELOPMENT_ONLY foundation (P03); not HA, not RKE2 |
| `services/agent/api`, `services/agent/control`, `services/agent/workflow` | Git submodules with the previous architecture's Go modules (Connect RPC, `ReadDSN` API) | Legacy, outside `go.work` and the build closure; retired only through P24 |
| `services/agent/model-proxy`, `jobs/shared/access-sidecar` | Git submodules containing README and LICENSE only | Planned; no implementation |
| `deploy/local/`, `compose.yaml`, `tools/prepare-local-compose.py` | Docker Compose profile of the legacy local-check flow (PostgreSQL 18.6, Temporal 1.31.2), mounts repointed to `docs/archive/contracts/` | Legacy environment; not handed over, not a startup procedure for the replacement |
| `packages/biome-config`, `packages/typescript-config`, `apps/` | pnpm/Turborepo starter | Starter only; TypeScript services arrive with P11/P15 |

Not present in any form: `anvilkit-agent-model-proxy`, `-knowledge`, `-mcp`, `-background-worker`, `-inference`, the `jobs/{codegen,validator,preview,parser}` classes, `packages/profile-schemas` and `deploy/{charts,gitops,policies}`.

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
| `python3 tools/check-docs.py` | Current-baseline Markdown check (fences, tables, links, anchors); missing required inputs fail | Python 3 | No |
| `python3 tools/check-contracts.py [--against REF]` | `buf lint`, OpenAPI validation, JSON Schema metaschema + fixtures, public API vectors | Python 3 with `jsonschema`, `referencing`, `pyyaml`, `openapi-spec-validator`; buf 1.73.0 | No |
| `python3 tools/generate-contracts.py [--check]` | Regenerates the Go bindings and sqlc code with pinned tools; `--check` diffs against a scratch tree | buf, protoc-gen-go, protoc-gen-go-grpc, oapi-codegen, sqlc at the pinned versions (`packages/generated-clients/go/README.md`) | Without `--check`: yes |
| `python3 tools/run-verification.py [--static]` | docs → generate `--check` → contracts → `go build/vet/test` per `go.work` module → integration (needs the dev foundation) | Go 1.26+, Docker for Testcontainers unless `--static` | No (scratch only) |
| `sh deploy/dev/up.sh` / `sh deploy/dev/down.sh` | DEVELOPMENT_ONLY foundation: PostgreSQL 17, Temporal, migrations, kind cluster, launcher identity | Docker, kind, kubectl, Go | `.local/dev/` only |
| `go test -tags integration ./...` in `tests/integration` | End-to-end LocalCheck on the dev foundation | `. .local/dev/env.sh` | Temp dirs only |
| `GOWORK=off go build -mod=readonly ./...` in each legacy submodule | Builds the legacy service module | Go 1.27 | Go build cache only |

## Known limitations for the next phase

- **Development-only components.** The filesystem inventory adapter in Control, the bearer-token fixture verifier in the API, plaintext loopback gRPC and the kind/Compose topology exist only for development; ENV-02/03/07 inputs replace them in P07 and P22/P23.
- **Gates.** G-01 to G-14 remain `NOT_RUN`; the passing tests recorded in [delivery.md](docs/architecture/delivery.md#unit-status-records) are unit verification of P01–P05.
- **Not yet implemented.** Budgets and single-use dispatch (P06), external effects and the RGW inventory (P07), artifacts (P08), Job isolation (P09) and everything from P10 on; the API answers `DEPENDENCY_UNAVAILABLE` for those paths.
- **Git baseline.** The new inputs are untracked until the user stages them (see the [cleanup record](docs/architecture/delivery.md#cleanup-record)); `buf breaking` therefore has no recorded baseline yet.
