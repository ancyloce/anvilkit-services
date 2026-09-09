# AnvilKit Services

AnvilKit Services is the backend repository for the AnvilKit Agent Platform: a self-hosted Go/TypeScript monorepo for bounded component generation, isolated validation and preview, and release coordination through Pagix.

The [architecture document](docs/architecture/architecture.md) is the source of truth. It retains version **3.12**, dated **2026-09-08**; its filename and location do not change that baseline. Confirmed product decisions, adopted design baselines, configurable pilot defaults and unresolved activation inputs have different meanings throughout the document.

## Project status

This checkout contains the architecture, Codex documentation and a pnpm/Turborepo starter with `apps/web` and shared Biome/TypeScript configuration packages. The Agent services, execution jobs, root Go module, generated contracts and deployment profiles described below are planned architecture, not implemented or qualified features in this checkout.

The architecture supports independent local implementation and controlled proofs. It does not establish real provider access, Pagix endpoint availability, sandbox isolation, publication, Studio runtime compatibility or production readiness. Detailed-design freezes and runtime acceptance remain separate gates.

## First milestone

Deliver one new static content component through the complete business chain:

1. Accept an authorized tenant-scoped request, with observable status and cancellation.
2. Produce a bounded DeepSeek plan and complete component package source through the qualified Pi coding profile.
3. Independently validate the source, perform at most the configured bounded repair, and register a usable candidate through Pagix.
4. Edit source and supported properties in Studio, save with revision checks, and preview the matching source snapshot.
5. Freeze the source and package version; certify the npm package, browser module, styles and exact evidence.
6. Obtain platform-maintainer approval for that immutable release subject.
7. Publish through Pagix, verify both npm and browser-delivery receipts, then activate the catalog.
8. Load and insert the component into the unchanged qualified Studio deployment, save the page and reopen the same locked release.

A source archive, successful model turn, package build or npm-only publication does not complete this milestone. Approval, publication, activation and successful browser use are separate outcomes. See [requirements](docs/architecture/architecture.md#s-1-2) and the [acceptance scenario](docs/architecture/plans/qualification.md#s-13-2).

## Services and execution boundaries

The component chain has four independently built and deployed long-running application services.

| Canonical service | Runtime | Planned source path | Responsibility |
| --- | --- | --- | --- |
| `anvilkit-agent-api` | Go | `services/agent/api` | Public scoped commands, operation status, projections and SSE; no cost-ledger write credential |
| `anvilkit-agent-control` | Go | `services/agent/control` | Attempts, execution authority, permits, cost reservations, per-call admission, durable intents and artifact authorization |
| `anvilkit-agent-workflow` | Go + Temporal SDK | `services/agent/workflow` | Durable progression, Runner, Activities, job supervision and Pagix release coordination |
| `anvilkit-agent-model-proxy` | TypeScript + `pi-ai` | `services/agent/model-proxy` | Provider credential custody, qualified model transport and per-physical-request Control admission |

Untrusted component work runs in separate on-demand jobs.

| Execution unit | Runtime | Planned source path | Responsibility |
| --- | --- | --- | --- |
| `anvilkit-component-codegen` | Node / Pi coding SDK | `jobs/component/codegen` | Complete source generation and bounded repair; one inner coding loop per Attempt |
| `anvilkit-component-validator` | Node / component toolchain | `jobs/component/validator` | Independent candidate validation and protected final certification |
| `anvilkit-component-preview` | Node / browser tooling | `jobs/component/preview` | Short-lived, revision-bound build and preview; no model/publication permission |
| `anvilkit-job-access-proxy` | Go sidecar | `jobs/shared/access-proxy` | Trusted per-job holder of scope credentials and allowlisted model/artifact relay |

The execution adapter is internal code at `services/agent/workflow/internal/execution`. The Runner is also internal to the Workflow service. Neither is an additional service. Temporal, PostgreSQL, Kubernetes, gVisor, the enforcing CNI and artifact storage are infrastructure dependencies.

`anvilkit-export-worker` is a separate domain, with future placement at `services/export/worker`. Export redesign and an empty export scaffold are outside this component milestone.

## Ownership and safety invariants

- **Pagix is the external business authority.** Consume authenticated APIs for team/business authorization, source revisions and leases, commercial credits, review, publication and activation. Agent retains scoped references and verified receipts; it does not access Pagix databases or implement its publisher.
- **Temporal owns business progression.** `GenerationWorkflow`, `PreviewBuildWorkflow` and `ReleaseWorkflow` have separate lifetimes. Go Workflow code is deterministic; I/O belongs in Activities/jobs. Control admission and start/change relays do not become a second scheduler.
- **Control owns execution and actual costs.** Every physical paid request requires current admission and one successful dispatch claim. Duplicate claims do not grant another send. Uncertain provider or business effects retain their original identities and exposure until reconciled.
- **Intake and execution are distinct.** Durable intake precedes remote mutations. Initial queue waiting holds no draft lease or commercial reservation. Protected Workflow bootstrap obtains a permit, rechecks the original source/scope, confirms lease and funding, then enables planning/coding. Retries, holds and permit reacquisition do not reset the active deadline.
- **Candidate code remains isolated.** Provider keys stay in Model Proxy; job credentials stay in the trusted sidecar. Candidate jobs have no production business, provider or npm credentials. Current Attempt/instance, lease, deadline and execution/recovery fences govern calls and result acceptance.
- **Validation and approval bind exact artifacts.** Reuse the protected generator/build closure and independent validator. Source, version, build/host profile, npm/browser/CSS identities and evidence form the release subject. Both verified publication outputs must precede activation.
- **Studio owns browser consumption and page writes.** Use the actual shared React/Puck runtime and manifest-listed styles. Persist Puck Data and an exact remote release lock in one page revision through the existing page API; preserve `root.props.componentLibrary`. Catalog updates do not silently upgrade saved pages.
- **Credits and costs remain separate.** Pagix commercial credits are not provider currency. Actual failed, canceled, repaired and uncertain work stays attributable. Pending settlement does not trigger duplicate generation.
- **Changes and recovery preserve issued effects.** Definition changes, holds and cancellation fence new dispatch and reconcile work already issued. A timeout, expired lease or missing Worker is not proof that a process stopped or an external effect did not occur.
- **Status is evidence-backed.** Durable SSE uses `(operationId, eventSeq)`, with transactional projection/event updates and authorized replay/snapshot recovery. Model token progress cannot confer business success.
- **Telemetry is diagnostic, not authoritative.** Every unit emits versioned structured JSON logs correlated by `operationId` and W3C trace context under the [unified logging contract](docs/architecture/plans/operations.md#logging); candidate output is counted and classified but never carried in any log field, and its bounded content stays in a private diagnostics artifact behind a separate authorization; logs never replace ledgers, receipts or durable events.

## Technology and model profiles

The architecture selects one root Go module, pnpm workspaces and Turborepo for TypeScript tasks. Go builds/tests use the Go toolchain directly. Services remain independently deployable without internal Git submodules; Studio, Pagix and `anvilkit-components` stay separate repositories.

Public interfaces use OpenAPI. Private Control interfaces use generated Protobuf/gRPC contracts with Connect-Go/Buf and qualified Go/TypeScript clients. Node jobs use versioned JSON envelopes. Shared public/job values originate from `contracts/values/common-v1.schema.json`; money and 64-bit counters cross JavaScript as bounded decimal strings. Generated clients and compatibility fixtures must precede dependent implementations.

DeepSeek planning and the Codex coding-model role are distinct from the coding executor. Version 3.12 specifies `deepseek-v4-pro` as the planning pilot route and `gpt-5.3-codex` through `pi-ai`'s `openai` provider with a platform-owned API key for coding. The proposed initial executor is the Pi coding SDK inside the Codegen job. The subscription/OAuth `openai-codex` provider and native Codex SDK/executable are not the initial execution profile; native execution remains a separately qualified future alternative. No nested loop or automatic model/executor fallback is selected.

These are architecture profile selections, not confirmation of account availability or paid-execution readiness. Exact SDK/dependency/image locks, model/account/adapter behavior, allowed provider data, native usage, cancellation and currency limits must qualify before real calls. Retries and compaction stay disabled unless every enabled physical call is intercepted and admitted.

Node 24 LTS is the execution baseline. Self-hosted Temporal, separate Agent/Temporal PostgreSQL roles, and Kubernetes Jobs with native sidecars, pinned gVisor and an enforcing CNI require exact environment qualification. Reuse the existing component generator and frozen Rslib 0.x package profile; qualify browser output separately. See [technology and repository structure](docs/architecture/architecture.md#s-3).

## Repository layout

Current documentation entry points are:

- [Architecture v3.12](docs/architecture/architecture.md): overview and core constraints with links to detail owners.
- [Codex repository guide](AGENTS.md): instructions for repository work.
- [Codex documentation index](docs/architecture/README.md), [design](docs/design/design.md), [interfaces](docs/design/interfaces.md) and [qualification](docs/architecture/plans/qualification.md): draft supplements.

Additional planned paths from the [implementation layout](docs/architecture/plans/implementation-plan.md#s-3-2) are shown below. Most remain planned implementation locations. The [contract inventory](docs/design/contracts.md) identifies the document specifications now extracted into contract paths; generated clients and implementations remain pending.

| Planned path | Purpose |
| --- | --- |
| `internal/integrations/pagix` | Typed guarded `PagixPort` and actual API mappings |
| `internal/contracts`, `packages/contracts-ts` | Generated Go/TypeScript contracts |
| `contracts/values`, `contracts/proto`, `contracts/openapi`, `contracts/jobs` | Shared values and distinct public/private/job wire contracts |
| `contracts/definitions`, `contracts/actions` | Bounded definition/policy/binding schemas, descriptors and fixtures |
| `contracts/telemetry` | Structured log-record schema and telemetry design fixtures |
| `workflows/definitions/<family>` | Reviewed generation/release definitions; component family first |
| `packages/component-toolchain`, `packages/job-protocol` | Shared build/certification and Node protocol helpers |
| `deploy/<environment>`, `releases` | Environment profiles and immutable compatibility/release manifests |
| `docs/architecture`, `docs/design`, `docs/architecture/plans`, `docs/architecture/audits`, `tools` | Overview, detailed designs, readiness plans, audit records and reproducible utilities |

## Working with the current starter

The root manifest specifies `pnpm@12.3.4` and Node `>=22`. This starter engine range is separate from the architecture's qualified Node 24 execution profile. Use the pinned package manager when working on the existing workspace.

| Root command | Current behavior |
| --- | --- |
| `pnpm dev` | Runs the existing workspace development tasks through Turbo; `apps/web` uses port 3000 |
| `pnpm build` | Runs existing workspace build tasks |
| `pnpm lint` | Runs workspace lint tasks; the web task uses `biome check --write` and can modify files |
| `pnpm check-types` | Runs workspace type checks; the web task invokes `next typegen` and can generate files |

The web starter currently references `@repo/ui`, which is absent from this checkout. Installation/build readiness therefore needs a separate implementation fix and verification. The commands above describe existing scripts; they do not start or validate the planned Agent platform. No Go module or complete local Agent startup is present yet.

For later implementation, service-specific startup, configuration, build and readiness instructions belong in each service README. Go commands such as `go build ./...` and `go test ./...` apply after the root Go module exists. Component qualification must use the frozen component repository's actual scripts and support closure, rather than assuming starter commands validate generated packages.

Documentation-only changes can be checked through Markdown/link validation and file-diff inspection without installing dependencies or running mutating build/lint tasks; `python3 tools/check-docs.py` runs the link, anchor, table, JSON Schema and fixture checks locally.

## Implementation and qualification gates

| Stage | Required outcome |
| --- | --- |
| P0-A | Shared contracts, bounded Runner/descriptor validation, fixed job profiles, controlled component fixtures and the real Studio production-build host/page-lock proof |
| P0-B | Real bounded planning/coding, protected bootstrap/cleanup, atomic admission, independent source validation and real Pagix candidate registration |
| P1-A | Revision-checked browser editing, isolated matching previews and frozen dual-output certification |
| P1-B | Real maintainer approval, publication/activation APIs and component insertion/save/reopen in the unchanged qualified Studio deployment |
| P1-C | Supported live changes, cancellation/unknown-effect recovery, replicated limits and demonstrated restore/operating behavior |

The [readiness plan](docs/architecture/plans/implementation-plan.md#s-14-2) assigns five detailed designs and one external integration contract:

| Design | Ownership |
| --- | --- |
| DD-01 | Workflow/Runner, protected bootstrap/lease supervision/cleanup, definitions, policies, bindings and live changes |
| DD-02 | Control, common values, public/private contracts, transactional model, authorization, costs and SSE |
| DD-03 | Execution adapter, Kubernetes sandbox, Pi profile, job/result envelopes and sidecar credentials |
| DD-04 | Component source/build closure, editing, preview, protected registration and certification |
| DD-05 | Actual Studio host bridge/ABI, styles, page locks and compatibility |
| DD-06 | Actual Pagix API paths, delegated authorization, revisions, idempotency and receipt/query semantics |

Freeze shared contracts before dependent implementation and the relevant design before its affected implementation. Qualify sandbox isolation before any untrusted execution, including previews. Start the real Studio production-build proof in P0-A and freeze its ABI before production loader implementation.

Missing Pagix capabilities block their own generation, publication or host-upgrade path. Missing paid-route/data-policy/budget inputs deny real model dispatch. Local fixtures cannot authorize real effects. Pilot concurrency, preview latency, retention and RTO/RPO numbers are configurable targets requiring evidence, not measured guarantees inferred from audience size. See [readiness gates](docs/architecture/plans/implementation-plan.md#s-14-3) and [activation inputs](docs/architecture/plans/implementation-plan.md#s-15-1).

## Deferred scope

The first milestone covers static `content` packages. Nested editable slots/composition, forms, external data, complex animation, page/image agents and export redesign are deferred. So are generic multi-agent graphs, autonomous subagents, scheduled paid triggers, arbitrary workflow migration, visual workflow editing, a browser IDE/CRDT source editor, retrieval/vector/long-term-memory systems, a second executor and additional speculative platform services.

Pagix's internal database, billing, publisher, registry provisioning and deployment remain outside this repository's architecture scope. Wider component visibility and host upgrades have separate qualification gates; the initial visibility policy is team-private. Deferred features do not require empty scaffolds.
