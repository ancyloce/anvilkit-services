---
description: Compress the current session, synchronize project memory, generate a handoff, and stop
---

# Close Current Session

The current session is long enough to require closure.

Do not continue implementation, debugging, investigation, deployment, or scope expansion.

Follow `CLAUDE.md` and perform only the closeout workflow below.

## 1. Inspect Current State

Review:

- the complete current session
- `docs/ROADMAP.md`
- `docs/SESSION_SUMMARY.md`
- `docs/DECISIONS.md`
- `docs/BLOCKERS.md`
- `docs/HANDOFF.md`
- relevant working-tree changes
- available test, build, validation, and deployment evidence

Use repository evidence over assumptions.

Do not run commands solely to create new work.

## 2. Extract Durable Information

Identify:

- current objective
- current stage and milestone
- completed work
- partially completed work
- verified results
- unverified results
- changed files and components
- architecture, infrastructure, configuration, and deployment changes
- decisions made during this session
- active and resolved blockers
- open risks
- remaining tasks
- one concrete next action
- actions the next session must not perform

Do not preserve repetitive conversation, transient reasoning, failed paths with no continuing value, or raw command history.

## 3. Synchronize Project Memory

Update only the documents affected by verified session facts.

### `docs/ROADMAP.md`

- synchronize milestone status
- preserve approved scope
- do not invent milestones
- do not add unsupported future work

### `docs/SESSION_SUMMARY.md`

- replace stale state with the actual current state
- summarize completed and in-progress work
- reference supporting evidence
- list active risks, decisions, and blockers
- specify exactly one next action
- remove obsolete or duplicated information

### `docs/DECISIONS.md`

- create records for significant decisions made during this session
- include rationale, alternatives, impact, owner, and reversal conditions when known
- mark replaced decisions as `SUPERSEDED`
- never delete decision history
- do not convert assumptions into decisions

### `docs/BLOCKERS.md`

- create newly confirmed blockers
- update existing blocker status
- mark verified resolutions as `RESOLVED` or `CLOSED`
- include owner, impact, required action, resolution criteria, and evidence
- do not mark a blocker resolved without evidence

### `docs/HANDOFF.md`

Regenerate it so a new Claude Code session can continue without reading this conversation.

It must include:

- current goal
- current stage
- concise current state
- completed work
- unverified or partial work
- active decisions
- active blockers
- known risks
- changed files or relevant resources
- verification evidence
- exactly one next action
- explicit `Do Not Do` items
- startup instructions for the next session

## 4. Assess Project Health

Score each category from `0` to `5`:

- Documentation
- Scope Control
- Decision Traceability
- Blocker Management
- Code Quality
- Verification Coverage
- Deployment Readiness
- Operational Risk
- Recoverability
- Project Momentum

Use this scale:

- `0`: Critical
- `1`: Poor
- `2`: Weak
- `3`: Acceptable
- `4`: Good
- `5`: Excellent

Calculate the total out of `50`.

Use these ratings:

- `45-50`: GREEN
- `35-44`: HEALTHY
- `25-34`: YELLOW
- `15-24`: AT_RISK
- `0-14`: CRITICAL

Record the assessment in:

- `docs/SESSION_SUMMARY.md`
- `docs/HANDOFF.md`

For every category below `4`, include a brief evidence-based explanation and one improvement action.

Do not award points without repository evidence.

## 5. Validate Closeout

Before finishing, confirm:

- the roadmap matches the repository state
- summary references resolve to existing decisions and blockers
- blocker statuses are evidence-based
- handoff contains one executable next action
- no important session knowledge exists only in chat
- a new session can recover state from repository documents
- Markdown headings, links, code blocks, and tables remain valid
- no escaped pipes are used inside Markdown table cells

If recovery is incomplete, update the documents before finishing.

## 6. Git and External-Action Restrictions

Do not:

- stage changes
- commit
- amend
- push
- open or merge a pull request
- create or switch branches
- create worktrees
- rebase, merge, cherry-pick, reset, clean, or tag
- change submodules
- deploy or release
- publish artifacts
- send data to an external service
- bypass hooks or repository protections

A hook or policy block is expected behavior and must not be bypassed.

Inspecting `git status` and `git diff` is allowed.

## 7. Final Response

Return only a concise closeout report containing:

1. session status
2. completed work
3. documents updated
4. decisions added or changed
5. blockers added, resolved, or still active
6. verification status
7. project health score and rating
8. exactly one next action
9. confirmation that the session is ready to end

If documentation cannot be updated, report the exact blocker and stop.

After the report, do not continue work.