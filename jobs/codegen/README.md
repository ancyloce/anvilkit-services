# Codegen Job (parent integration)

The codegen Job class (architecture DD-03, delivery.md P09/P12) is built from two repositories mounted here as Git submodules; this directory's own content is this integration note. Both run inside **one** codegen Job container: the trusted supervisor is the entrypoint, starts the trusted team coordinator as its child, and launches, stops and confirms the low-privilege Pi coder for every coding round. Nothing here is a separately deployed service.

| Path | Repository | Owns |
|---|---|---|
| `supervisor/` | [`anvilkit-job-codegen-supervisor`](https://github.com/ancyloce/anvilkit-job-codegen-supervisor) (Go module `github.com/ancyloce/anvilkit-job-codegen-supervisor`) | The trusted supervisor and its privilege-drop trampoline, the candidate stop and confirmation, the observer and finalizer of the fixed task, the fixed candidate, `config.yaml`, the base trusted resources (`agent/resources.json`), the fixed-task fixtures, the fixed-task image (`anvilkit-codegen`: `codegen-fixed-v1`, `harness-wiring-dev-v1`) |
| `team/` | [`anvilkit-job-codegen-team`](https://github.com/ancyloce/anvilkit-job-codegen-team) (npm package `@anvilkit/codegen-team`) | The trusted coordinator (LangGraph team, budgets, stage store, recovery), the Pi coder, `team.yaml`, the prompts and reviewed tools (`agent/team/`), the brief fixture, the final Codegen Job image (`anvilkit-codegen-team`: `codegen-team-dev-v1`) |

## Process protocol

Supervisor and coordinator speak newline-delimited JSON over the coordinator's stdio: `run-candidate` requests, `candidate-ended` and `refused` answers, and the coordinator's `team/result.json`. Its single source is the contracts repository (`contracts/jobs/codegen/protocol.schema.json`, `urn:anvilkit:codegen-protocol:v1`, with `fixtures.json`, checked by `contracts/tools/check.py`). Each repository holds a verbatim copy with its digests (`supervisor/internal/protocol/contract/`, `team/contract/`), verified against the source by its own `tools/sync-protocol.sh --check <contracts>` and held to the fixtures by its tests. Neither repository links a contracts package: the jobs module embeds the profiles that pin these images' digests.

## Image

The team repository's `Dockerfile` builds the final image from both repositories (multi-stage; the supervisor's sources as the named build context `supervisor`) on the independent validator's image named by digest:

```sh
docker build --build-context supervisor=jobs/codegen/supervisor \
  --build-arg VALIDATOR_REPOSITORY=localhost:5001/anvilkit-validator -t anvilkit-codegen-team jobs/codegen/team
sh jobs/codegen/team/tools/image-smoke.sh anvilkit-codegen-team
```

`deploy/dev/images.sh` builds both images into the development registry. The image digests are pinned by `contracts/jobs/profiles.json` and admitted by `deploy/policies/kyverno/`; a rebuilt image is a new digest and needs a profile revision there. The team image's inputs outside its repository are pinned in its CI (`SUPERVISOR_REF`, `CONTRACTS_REF`, the validator digest in the `Dockerfile`).

## Parent-owned integration

- `go.work` uses `./jobs/codegen/supervisor`; the team package is outside the Go closure.
- `tests/integration/{harness,team}_integration_test.go` run the supervisor, the coordinator, the coder, the access sidecar, Control, the Model Proxy and the validator chain with real processes on the development foundation (`TestTrustedHarnessProcessFlow`, `TestCodegenTeamFlow`); the team operation is funded with `team.yaml`'s aggregate exposure, which accounts every send at the route's reserved `max_exposure`.
- `tools/run-verification.py` (team step: both protocol copies, then the team package; go step: the supervisor module), `tools/check-source-export.py` (both repositories in the export set; the supervisor builds alone with `GOWORK=off`; both protocol copies against the exported contracts), `tests/evals/registry.json` (`codegen`, `team` suites), `deploy/handover/rehearse.py` (closure modules), `deploy/dev/images.sh`.

## Verification

```sh
(cd jobs/codegen/supervisor && GOWORK=off go vet ./... && GOWORK=off go test -count=1 ./...)   # as root: real UID 10001 processes
(cd jobs/codegen/team && pnpm install --frozen-lockfile && pnpm run check-types && pnpm run lint && pnpm run build && pnpm run tools:check && pnpm test)
sh jobs/codegen/supervisor/tools/sync-protocol.sh --check contracts && sh jobs/codegen/team/tools/sync-protocol.sh --check contracts
. .local/dev/env.sh && (cd tests/integration && go test -tags integration -run 'TestTrustedHarnessProcessFlow|TestCodegenTeamFlow' .)
```

These establish behavior on the host with real processes; they qualify no Kubernetes runtime, gVisor or candidate isolation (G-04/G-05 stay NOT_RUN; both codegen profiles stay DISABLED).
