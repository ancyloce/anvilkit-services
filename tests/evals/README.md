# Evaluation registry and evaluator (P22)

`registry.json` is the versioned requirement traceability of
[delivery.md P22](../../docs/architecture/delivery.md): every M1, CAP and SEC
requirement maps to executable cases (a suite, a selector, the reviewed
profiles it exercises, a time budget and the gates it contributes to) and to
explicit NOT_RUN entries that name the missing environment inputs (ENV-01 to
ENV-10). Contributing evidence never closes a gate on its own.

| Path | Content |
|---|---|
| `registry.json` | Suites, cases and NOT_RUN entries (schema version 1, registry revision) |
| `rag/`, `memory/` | The DEVELOPMENT_ONLY labeled sets of the P16/P17 evaluators, whose reports keep every trial |

```sh
.local/verification-venv/bin/python tools/run-evaluation.py --check       # coverage and selectors
. .local/dev/env.sh && .local/verification-venv/bin/python tools/run-evaluation.py --run [--case ID] [--suite NAME]
.local/verification-venv/bin/python tools/run-evaluation.py --inventory   # the release-candidate inventory
```

`--run` writes `outputs/evaluations/<run>/` (ignored by Git): `raw/<case>.log`
with every byte the suite printed, `report.json` and `report.md` (per case
PASS/FAIL/NOT_RUN with duration and raw digest; per requirement the local and
the qualification status; per gate the contributing cases and what blocks it;
pass@k, pass^k, failure classes and latency percentiles over every kept trial of
the RAG and memory evaluators) and `inventory.json` (repository revisions and
working-tree digests, contract, migration, profile and lockfile digests,
toolchains, and why the release candidate is or is not qualifiable). A failing
case is recorded, never dropped; the owning unit fixes it or its profile is
quarantined (P22 recovery). Monetary cost is reported only where a priced route
exists; none does (ENV-06).

## Named assertions (registry revision 13, P3.4)

Since revision 13 a requirement is judged by **named assertions**, not by
whole cases. `assertions` maps each M1, CAP and SEC requirement to tests and
subtests. Each entry names:

- its case and full name: a Go test plus its subtest path as `go test -json`
  reports it, a Vitest or Playwright full name, or a checker's case;
- the `file:line` that defines it;
- for Vitest, Playwright and checker entries, the `title` written on that line;
- what it checks.

`scenarios` carries each requirement's scenario text. `gaps` records the parts
of a scenario that no test exercises yet, and `notRun` the parts blocked by a
missing environment input.

A requirement's status in a run is that of its assertions. An assertion is
PASS only when exactly that test or subtest reported a pass. A subtest that
skipped, or a case that did not run, leaves the requirement NOT_RUN. A failed
assertion fails it.

The Studio Playwright tests run inside `TestStudioWorkbench`, whose Go result
cannot be separated per spec test. They stay NOT_RUN until the Studio run
reports per test.

```sh
.local/verification-venv/bin/python tools/run-evaluation.py --trace   # the reviewer's listing
```

`--check` refuses an assertion in any of these situations:

- it names an unknown case;
- it sits in a case that does not map the requirement;
- it lies outside a Go case's test selection;
- it cites a `file:line` that does not define it.

`${ANVILKIT_STUDIO_DIR}` paths are checked when the Studio checkout is present.

Revision 13 removed these mappings, whose assertions contradict or do not
exercise the scenario:

| Requirement | Case | Why |
|---|---|---|
| M1-13, CAP-07 | IT-DEPLOYED | `TestDeployedRepairChain` requires one replica, `Recreate` and no HPA: the opposite of the two-instance HA target |
| M1-17 | IT-LIFECYCLE | brand references resolve through the DEVELOPMENT_ONLY Pagix/Knowledge double, whose digests are synthetic and whose ACL is not checked |
| M1-14, M1-16 | IT-LIFECYCLE | one hold refusal and one round of one question; no definition change, no second round |
| SEC-04 | IT-HARNESS, U-CODEGEN | the AF_INET/AF_INET6 probes are logged, not asserted (WF-CLUSTER asserts them) |
| M1-03 | IT-NETWORK, U-NETWORK, U-POLICIES, U-SIDECAR, WF-CLUSTER, IT-HARNESS, IT-CERTIFICATION | isolation tests, not rejection of candidate-modified configuration |
| M1-12 | IT-LOCALCHECK, IT-FAULTS, IT-UNRESOLVED-CREATE, IT-ARTIFACTS | no database rollback |
| M1-13 | IT-WORKFLOW-BOUNDARY | redaction and worker versioning on one Temporal |
| M1-07 | IT-RELEASE | scenario A is inline in `TestRelease` on the Pagix double and cannot be named |
| M1-01, SEC-01 | U-CONTRACTS | contract vectors; no duplicate submission, tenant, SAN or audience case |
| SEC-01, SEC-03 | U-SECRETS | a scan of rendered manifests; no impersonation or key theft |
| SEC-02 | U-WORKFLOW-BOUNDS | its "lost grant response" is a funding refusal, not a dispatch grant |
| SEC-07 | IT-MCP-CALLS | no redirect, rebinding or metadata case at integration level |
| SEC-10 | IT-KNOWLEDGE-PUBLIC, IT-CERTIFICATION | no malicious document, archive, link or dependency |
| SEC-12 | U-CONTROL | no page lock, type collision, props or CSP |

Cases that no longer map a requirement (IT-FAULTS, IT-UNRESOLVED-CREATE,
IT-DEPLOYED, U-SECRETS) stay registered as supporting evidence. Each states
why in `unmapped`.

The PITR tests are registered under G-14:

- IT-PITR (`TestPointInTimeRecovery`): M1-12, SEC-08, SEC-11, CAP-07.
- IT-MEMORY-PITR (`TestMemoryRemovalRecovery`): CAP-03, SEC-06, SEC-11.

The P3.5 cases are:

- STRESS-MODEL-PROXY-SETTLEMENT;
- LOAD-CAPACITY, LOAD-SOAK and LOAD-CHAOS, which run only under an
  operator-authorized `ANVILKIT_QUALIFICATION_RUN`.

The SEC-09 cluster negatives are registered as:

- SEC09-POLICIES-CLUSTER (`deploy/policies/check.sh --cluster`, development
  cluster);
- SEC09-ADMISSION (`check-admission.py`, qualification cluster).

### Sign-off (P3.4 AC1)

| Revision | Prepared | Reviewer sign-off |
|---|---|---|
| 13 | 2026-10-13: the mapping was drafted from a read of every cited test and verified by `--check`, which confirms each name exists at its `file:line` | **PENDING**: a human reviewer confirms, per requirement in `--trace`, that each named assertion exercises the scenario text and that no mapping contradicts it, then records name, date and any changes here |

Until that sign-off is recorded, revision 13's mapping is a proposal. It is
not reviewed traceability.
