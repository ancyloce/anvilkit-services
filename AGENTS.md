# Codex repository guide

## Source of truth and scope

Read [AnvilKit Agent Platform Architecture v3.12](docs/architecture/architecture.md) before architecture-sensitive work. Its [active decisions](docs/architecture/architecture.md#s-3), [detail owners](docs/architecture/README.md#authoritative-sources) and [freeze gates](docs/architecture/plans/implementation-plan.md#s-14-3) govern this repository. [Historical decisions](docs/architecture/audits/architecture-history.md) are explicitly superseded where indicated; do not restore the former Go Model Proxy, Nx baseline, service aliases or pre-v3.11 intake order from those records.

The [Codex documentation index](docs/architecture/README.md) provides a focused reading route, design constraints, interface requirements and qualification checklist. These are draft supplements to the architecture's DD-01–DD-06 owners, not frozen contracts or implemented services. Distinguish confirmed decisions, design baselines, pilot defaults and activation inputs when documenting or implementing behavior.

Keep `docs/architecture/architecture.md` as the overview and reading entry point. Group detailed designs in `docs/design/`, implementation and readiness plans in `docs/architecture/plans/`, and audit/history records in `docs/architecture/audits/`; keep the hierarchy shallow and update affected links when moving files.

This file guides Codex working on the repository. It is not a system prompt for the platform's candidate jobs: [DD-03 trusted bootstrap](docs/design/dd-03-execution.md#s-8-1) prohibits automatically trusting candidate `AGENTS.md` files or other discovered resources.

## Repository state at initialization

The repository currently contains a pnpm/Turborepo starter, `apps/web`, and shared Biome/TypeScript configuration packages. The root manifest specifies `pnpm@12.3.4` and Node `>=22`. The architecture's proposed execution runtime is Node 24 LTS; a manifest engine range is not a qualified runtime lock.

No Agent service, job, root Go module, generated contract bundle or deployment is established by this documentation initialization. Paths below are planned locations. Inspect current files before subsequent work; this paragraph records the initialization state, not a permanent assertion about the repository.

## Architecture and placement

| Canonical unit | Planned path | Responsibility |
| --- | --- | --- |
| `anvilkit-agent-api` | `services/agent/api` | Go public commands, status and SSE; no cost-authority write role |
| `anvilkit-agent-control` | `services/agent/control` | Go execution authority, attempts, admission, actual costs, permits, durable intents and artifact authorization |
| `anvilkit-agent-workflow` | `services/agent/workflow` | Go Temporal Worker with `GenerationWorkflow`, `PreviewBuildWorkflow`, `ReleaseWorkflow`, Runner and Activities |
| Execution adapter | `services/agent/workflow/internal/execution` | Internal Go job launch/query/stop and recovery; no separate daemon |
| `anvilkit-agent-model-proxy` | `services/agent/model-proxy` | TypeScript service using `@earendil-works/pi-ai`; provider credential custody and admitted transport |
| `anvilkit-component-codegen` | `jobs/component/codegen` | Node/Pi source generation and bounded repair |
| `anvilkit-component-validator` | `jobs/component/validator` | Independent candidate validation and protected final certification |
| `anvilkit-component-preview` | `jobs/component/preview` | Isolated revision-bound preview; no model/publication permission |
| `anvilkit-job-access-proxy` | `jobs/shared/access-proxy` | Trusted Go sidecar per job; scoped authenticated model/artifact relay |

There are four long-running component-chain services. Temporal, Kubernetes, PostgreSQL and artifact storage are infrastructure; the Runner, adapters and Pi SDKs are code within their named owners. `anvilkit-export-worker` belongs at `services/export/worker` when that separate domain is implemented; export has no M1 scaffold requirement.

Use one root Go module, pnpm/Turborepo for TypeScript and the Go toolchain directly for Go. Internal services are not Git submodules. Studio, Pagix and `anvilkit-components` remain separate repositories; use their declared API/artifact contracts without cross-repository runtime source imports.

## Invariants to preserve

- Pagix alone owns business authorization, source revisions, commercial credits, review, publication and activation through authenticated APIs. Agent stores references and verified receipts, not Pagix tables or a replacement business ledger.
- Temporal alone advances business steps. Control's admission and start/change relays do not select successors. Workflow code is deterministic; I/O belongs in Activities/jobs.
- Codex is the retained coding-model role. The initial executor is one Pi loop in the Codegen job. The architecture specifies `pi-ai` provider `openai`, a platform API key and `gpt-5.3-codex`, subject to qualification. Native Codex execution, subscription/OAuth `openai-codex`, nested loops and automatic fallback are not the initial profile.
- Every physical paid request needs a new successful Control dispatch claim. Duplicate claims never grant another send, including same-owner retries. Unknown dispatch/usage retains exposure; timeouts and process loss do not prove zero cost or authorize redispatch.
- Only the Model Proxy holds provider keys. Only the trusted sidecar receives job scope credentials. Candidate jobs have no provider, production business or npm credentials. Validate current Attempt, physical instance, lease, execution/recovery generation and deadline at consequential boundaries.
- Durable intake precedes remote mutations. Initial queue waiting holds no source lease or commercial reservation. Workflow bootstrap obtains a permit, rechecks the original source/scope, confirms the fenced lease and funding, then permits planning/coding. The first-permit deadline never resets.
- Cancellation, holds and live definition changes fence new dispatch, preserve issued effects/costs and reconcile original identities. An acknowledgement does not recall an already issued grant. Control's seven-rank lock order in [DD-02](docs/design/dd-02-control.md#s-10-3-1) applies to all mutations; no network I/O occurs under database locks.
- Generation yields complete source; independent validation and matching Pagix candidate registration establish usability. One automatic repair is the pilot default. An author cannot weaken protected validation, dependency policy, budgets or coverage to obtain a pass.
- Source, finalized version, npm/browser/CSS bytes, host profile and evidence form the approval subject. Only a matching current platform-maintainer decision authorizes publication. Both delivery receipts precede activation. Partial or unknown effects retain their original operation IDs.
- Studio owns the runtime loader and existing page write path. Save Puck Data and the exact remote release lock atomically; preserve `root.props.componentLibrary`. New compatible components must load without another Studio deployment. Matching React/Puck version strings do not establish shared runtime identity.
- Commercial credits and actual platform costs are separate. A candidate may have settlement pending; billing uncertainty must not trigger regeneration.
- Durable SSE uses `(operationId, eventSeq)`, committed with the projection under the operation lock. Transient tokens do not advance that cursor. Replay/snapshot reads require current disclosure authorization.
- Telemetry follows the [unified logging contract](docs/architecture/plans/operations.md#logging): one completion or failure summary per executed call attempt, `operationId` on every record about an operation, trace context that never confers identity, candidate stdout counted and classified but never carried in any log field, and no tokens, prompts, source, reconnect cursors or provider bodies in any log. Diagnostic content lives in private artifacts behind a separate authorization, referenced by an opaque `diagnosticsRef` that carries no capability. Logs are diagnostic, never evidence.

## Contract and design ownership

Follow [DD-02 interfaces](docs/design/dd-02-control.md#s-7-4) and [staged freezes](docs/architecture/plans/implementation-plan.md#s-14-3) before dependent implementation:

- Audit dispositions and the capabilities that stay disabled: [remediation 2026-09-08](docs/architecture/audits/remediation-2026-09-08.md).
- DD-01: Workflow/Runner, protected bootstrap/finalizer, definition/descriptor/policy/binding records and live changes.
- DD-02: common values, public OpenAPI/private Protobuf, Control transactions, identities, costs, state and SSE.
- DD-03: execution adapter, sandbox, Pi profile, outer job/result envelopes and sidecar credentials.
- DD-04: component/build manifest closure, protected registration, preview protocol and certification.
- DD-05: real Studio production-build proof, host ABI and authoritative page-lock mapping.
- DD-06: actual Pagix API mapping and separate generation/publication/host-promotion acceptance.

`contracts/values/common-v1.schema.json` is the extracted canonical public/job JSON value specification; generated bindings and contract freeze remain pending. Private RPC comes from `contracts/proto`. Generate consumer bindings and compatibility fixtures. Do not hand-maintain divergent Go/TypeScript schemas or use generic ProtoJSON as public JSON validation. Money and 64-bit counters cross JavaScript as bounded decimal strings. Reject duplicate keys, unknown authority fields and unauthorized nulls. Only Control canonicalizes executable definitions using the pinned RFC 8785 implementation.

These contract paths are architecture targets, not permission to create contract/code files during a documentation-only task. Do not invent Pagix endpoint paths, named owners, dependency locks or passing evidence. A missing external capability blocks its own real integration; independent local design/proof work can continue.

## Working and verification rules

Inspect `git status --short` and applicable guidance before edits. Preserve existing and untracked user work, including the supplied architecture. Respect each request's file scope; a documentation-only initialization does not authorize source, package, lockfile, configuration, deployment, executable contract or empty service scaffold changes. If a requested new file already exists under a no-modification instruction, preserve it until the user explicitly permits that exception.

Use the scripts that actually exist. At initialization, root `build`, `dev`, `lint` and `check-types` delegate to Turbo. `apps/web` lint runs `biome check --write`, and its type check invokes `next typegen`; those commands can modify/create files. Do not run them for a create-only documentation task. Documentation verification should check relative links, Markdown structure and the changed-file allowlist without creating build artifacts; `python3 tools/check-docs.py` performs those checks plus JSON Schema and fixture validation.

For later Go implementation, use `go build ./...` and `go test ./...` after a Go module exists. For component qualification, use the frozen component repository's actual commands, including `build:packages` where specified, rather than assuming this starter's root scripts apply there. A successful starter build cannot qualify Agent services or component delivery.

Report what changed, checks actually completed, and the exact remaining gate. Keep draft, frozen and qualified statuses distinct. Never infer provider billing, sandbox secrecy, real Pagix delivery, Studio rendering, two-replica correctness or restore targets from documentation, fixtures or package imports. Do not commit, push, publish or deploy without task authorization.
