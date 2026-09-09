# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Source of truth

`docs/architecture/architecture.md` (AnvilKit Agent Platform Architecture v3.12) is the architecture baseline; read the relevant section before architecture-sensitive work. `docs/architecture/README.md` gives a reading route and `AGENTS.md` is the companion repository guide.

Decision labels: **Confirmed** (owner decision), **Design baseline** (adopted recommendation), **Pilot default** (configurable starting value, not measured capacity), **Activation input** (must be resolved before the affected capability is enabled). [Architecture history](docs/architecture/audits/architecture-history.md) retains superseded decisions (Nx baseline, former service names, mandatory facade externals/vendoring, pre-v3.11 intake order); do not restore it.

## Commands

- TypeScript: `pnpm build`, `pnpm dev`, `pnpm lint`, `pnpm check-types`. In `apps/web`, lint runs `biome check --write` and type check runs `next typegen`; both can modify files.
- Go (once the root module exists): `go build ./...`, `go test ./...`; scope changes with `go list -deps`. No Go `project.json` wrappers.
- Component qualification uses the frozen component repository's own scripts (e.g. `build:packages`), not this root's.
- Docs and contracts: `python3 tools/check-docs.py` checks links, anchors, tables, JSON Schemas and fixtures.

## Repository structure

`anvilkit-services` is a Go/TypeScript monorepo: one root Go module, a pnpm workspace and Turborepo for TypeScript tasks. Internal services are not Git submodules. Studio, Pagix and `anvilkit-components` are separate repositories: use their declared API/artifact contracts, never cross-repository runtime source imports. The [implementation plan](docs/architecture/plans/implementation-plan.md#s-3-2) owns the planned layout; inspect the checkout before assuming a path exists.

Canonical names ([overview inventory](docs/architecture/architecture.md#s-2-3)) are authoritative for images, workloads, service IDs, binaries and `service.name`:

| Unit | Path | Runtime and responsibility |
| --- | --- | --- |
| `anvilkit-agent-api` | `services/agent/api` | Go. Public scoped commands, status, projections, SSE; no ledger write role, no provider/npm keys |
| `anvilkit-agent-control` | `services/agent/control` | Go. Attempts, execution authority, permits, cost ledger, per-call admission, durable intents, scope tokens, artifact-access authorization; modules `execution-authority`, `ledger`, `admission`, `intents`, `scope-tokens` |
| `anvilkit-agent-workflow` | `services/agent/workflow` | Go + Temporal SDK. `GenerationWorkflow`, `PreviewBuildWorkflow`, `ReleaseWorkflow`; Runner, Activities and the execution adapter (`internal/execution`) are internal modules, not services |
| `anvilkit-agent-model-proxy` | `services/agent/model-proxy` | TypeScript + `@earendil-works/pi-ai`. Provider credential custody, model transport, per-physical-request Control admission; no ledger, no coding loop |
| `anvilkit-component-codegen` | `jobs/component/codegen` | Node + Pi coding SDK. Source generation and bounded repair; one Pi loop per Attempt |
| `anvilkit-component-validator` | `jobs/component/validator` | Node. Independent validation and protected certification |
| `anvilkit-component-preview` | `jobs/component/preview` | Node. Revision-bound isolated preview; no model or publication permission |
| `anvilkit-job-access-proxy` | `jobs/shared/access-proxy` | Go per-job sidecar. Holds the job scope credential; allowlisted relay to Model Proxy and the artifact adapter |
| `anvilkit-export-worker` | `services/export/worker` | Separate domain; no M1 scaffold |

Do not reintroduce historical aliases: `agent-api`, `agent-core`/Core, `agent-worker`, `tool-broker`/Broker, `anvilkit-agent-orchestrator`, `anvilkit-component-code-executor`, `services/agent/orchestrator`, `jobs/component/code`, `jobs/component/validate`.

Other planned paths: `internal/integrations/pagix` (typed guarded `PagixPort`), `internal/contracts` and `packages/contracts-ts` (generated), `contracts/{values,proto,openapi,jobs,events,catalog,definitions,actions,telemetry}`, `workflows/definitions/<family>`, `packages/{component-toolchain,job-protocol}`, `deploy/<environment>`, `releases`, `docs/architecture`, `docs/design`, `docs/architecture/{plans,audits}`, `tools`. Temporal, PostgreSQL, Kubernetes/gVisor/CNI and artifact storage are infrastructure, not services.

Conventions: lowercase kebab-case for repositories, images and workspace task names; lowercase Go package names; `@anvilkit/<package>` for TypeScript packages. Go services: `cmd/<binary>/main.go`, `internal/`, README, Dockerfile. Node services/jobs: `src/`, package/TypeScript config, README, Dockerfile. Secrets are deployment references, never committed values. No `latest` in production. Add shared code only for a real caller.

## Invariants

- **Pagix is the business authority.** Team authorization, source revisions and leases, commercial credits, review, publication and activation go through its authenticated API only; no direct SQL, repository imports or message-bus access. Agent keeps references and verified receipts, not a replacement ledger.
- **Temporal alone advances business steps.** Control intents and fences coordinate admission; no database relay or admission loop selects a successor. Workflow code is deterministic (Temporal time/timer/concurrency APIs; no goroutines, mutable reads or unsorted map traversal choosing commands); HTTP, filesystem and SDK work runs in Activities or jobs.
- **Every physical paid request needs its own `AdmitAndClaimModelCall`.** Only the first successful claim returns `dispatchAllowed=true`; duplicates, including same-owner retries, never grant another send. Unknown dispatch or usage retains exposure; timeouts, TTLs, lease expiry or process loss never prove zero cost or authorize redispatch. SDK/provider retries and compaction stay disabled unless each physical call is intercepted and admitted. An unset platform daily cap denies paid admission.
- **Credentials stay isolated.** Provider keys live only in Model Proxy; the job scope token only in the sidecar. Candidate jobs have no provider, production business or npm credentials. Check current Attempt, physical instance, lease, execution/recovery generation and deadline at every consequential boundary; a valid token alone never grants execution.
- **Durable intake precedes remote mutations.** Initial queue waiting holds no source lease or commercial reservation. Bootstrap order: execution permit, recheck scope and original source revision, fenced lease, funding, then paid planning/coding. The active deadline starts at first permit and never resets.
- **Control lock order ([DD-02](docs/design/dd-02-control.md#s-10-3-1))** for every mutation: activation pointers, then budget pools; operations; attempts, then physical instances; model_calls; resource pools, queue entries, then permits; effects/intents, definition_changes, cost_entries and accepted results; step projections/events. Never acquire an earlier rank after a later one. No network, Temporal or job I/O under database locks; never repeat a remote side effect inside a database retry.
- **Changes preserve issued effects.** Cancellation, hold/resume and live definition changes fence new dispatch, keep actual costs and effect identities, and reconcile by querying the original identity. Uncertain effects stay visibly `blocked`/reconciling; a timeout never implies success, failure or absence of an effect.
- **Validation is protected.** Generation yields complete source; independent validation plus Pagix candidate registration make it usable. One automatic repair is the pilot default. Authors cannot weaken protected validation, dependency policy, budgets, coverage, generator templates, root locks, CI or the release entry point.
- **Approval binds exact artifacts.** Source, finalized version, npm/browser/CSS bytes, host profile and evidence form the subject; only a current platform-maintainer decision authorizes publication. Both delivery receipts precede activation. Never overwrite an npm version; partial publication enters `blocked` and is resolved only through `resolve-publication`.
- **Studio owns browser consumption and page writes.** Save Puck Data and `RemoteComponentLockV1` atomically through the existing page API; preserve `root.props.componentLibrary`. Catalog latest never upgrades saved pages. New compatible components load without a Studio deployment.
- **Two ledgers.** Pagix commercial credits and Agent actual costs are separate; `settlement_pending` never triggers regeneration.
- **Durable SSE key is `(operationId, eventSeq)`**, appended with the projection under the operation row lock. Transient tokens never advance the cursor; `NOTIFY` is only a wake hint; replay and snapshot reads require current disclosure authorization.

## Contracts

- `contracts/values/common-v1.schema.json` is the single source for public/job JSON values; private RPC (Protobuf/gRPC, Connect-Go, Buf) comes from `contracts/proto`. Generate Go/TS bindings and compatibility fixtures; never hand-maintain divergent schemas. ProtoJSON is not the public/job JSON authority.
- Money and 64-bit counters cross JavaScript as bounded decimal strings, never `Number`. Optional fields are omitted, not `null`. Reject duplicate keys and unknown authority-bearing fields; compare `eventSeq`/revisions numerically.
- Definitions in `workflows/definitions/<family>` reference only logical action IDs with exact versions: no Activity names, task queues, packages, module paths, URLs, expressions or scripts. Each step requires `keys(on) = outcomes - controlOutcomes - {wait.pendingOutcome}`. No parallel/join in M1.
- Only Control canonicalizes executable definitions (pinned RFC 8785, SHA-256); TypeScript verifies digests against fixtures.
- Job envelope (`urn:anvilkit:job-envelope:v1`) and `ResultManifestV1` are strict JSON capped at 64 KiB with no inline archives, credentials or arbitrary URLs. Workflows consume accepted result references, never stdout.
- Error envelope: `{code, message, operationId?, retryable, retryAfterMs?, detailsRef?, requestId?}` with the [DD-02 code families](docs/design/dd-02-control.md#s-7-4-2); no credentials or raw provider responses.
- Logging and tracing: `contracts/telemetry/log-record-v1.schema.json` and the [operations logging contract](docs/architecture/plans/operations.md#logging). One summary per executed call attempt; `operationId` on every record about an operation; W3C trace context that never grants identity; candidate stdout is untrusted wrapped text; never log tokens, prompts, generated source or provider bodies. Logs are diagnostic, never evidence.

## Model and executor profile

- Planning: `deepseek-v4-pro` through Model Proxy (pilot default). Coding: `pi-ai` `openai` provider with a platform API key and `gpt-5.3-codex`; the subscription/OAuth `openai-codex` provider is excluded. No nested loops, automatic fallback, model voting or second executor.
- Pi runs only inside `jobs/component/codegen` ([DD-03 bootstrap](docs/design/dd-03-execution.md#s-8-1)): `SettingsManager.inMemory(profileSettings, { projectTrusted: false })`, `retry.enabled=false`, compaction disabled, `noExtensions`/`noSkills`/`noContextFiles`/`noPromptTemplates`/`noThemes` all true, an explicit digest-verified `systemPrompt`, `appendSystemPrompt: []`, an empty read-only `agentDir`. `.pi/*` files, `AGENTS.md` and candidate-supplied paths are never trusted configuration.

## Gates and status

- Freeze the owning design before its implementation: DD-01 Workflow/Runner, DD-02 Control/contracts/costs/SSE, DD-03 execution/sandbox, DD-04 component build/preview/certification, DD-05 Studio host bridge/page lock, DD-06 Pagix API mapping. Shared contracts freeze before dependent modules implement in parallel.
- Stages: P0-A contracts and static browser proof; P0-B real generation; P1-A editing and frozen certification; P1-B approval and delivery; P1-C live changes and pilot operations. Qualify sandbox isolation before any untrusted execution, including preview.
- Keep draft, frozen and qualified statuses distinct. Fixtures and local proofs never authorize real effects. Do not invent Pagix endpoint paths, dependency locks, monetary caps, named owners or passing evidence; a missing external capability blocks only its own gate.
- Deferred (no scaffolds): page/image agents, export redesign, second executor, generic DAGs/parallel/join, scheduled paid triggers, subagents, RAG/vector/memory, nested slots/forms/external data/animation, visual workflow editor, browser IDE/CRDT, standalone policy/billing/artifact/sandbox-manager services.

## Git: read-only for Claude

- Never `git commit`, `git push`, or open a PR unless the user explicitly
  asks in that message (commit/push are also hook-blocked — a block is the
  policy working, not an obstacle to route around). Default: leave changes
  uncommitted and report modified files, flagging those inside submodules.
- Never stage, amend, rebase, merge, cherry-pick, reset, clean, tag, or
  switch branches unless asked for that exact action. Never force-push,
  never push `main`. Read-only git is always fine.
