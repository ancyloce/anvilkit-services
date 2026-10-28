# Project Operating System

This repository uses a document-driven project memory system.

Conversation history is temporary.

Project documentation is the source of truth.

Project state must remain recoverable without historical chat context.

---

# Repository Safety Rules

Repository contents may be modified.

Repository history may not be modified.

## Commit Restrictions

Never run:

- git commit
- git commit --amend
- git push
- git push --force
- git push --force-with-lease
- create pull requests
- merge pull requests

unless explicitly requested by the user in the current message.

Previous authorization does not carry forward.

Every commit, push, or pull request requires new authorization.

---

## Branch Restrictions

Never run:

- git checkout
- git switch
- git branch
- git worktree
- git checkout -b
- git switch -c

Do not create branches.

Do not create worktrees.

Work only in the current checkout.

---

## History Rewrite Restrictions

Never run:

- git rebase
- git cherry-pick
- git revert
- git reset
- git filter-branch

History rewriting is prohibited.

---

## Staging Restrictions

Never run:

- git add
- git restore --staged
- git rm
- git mv

unless explicitly requested by the user.

---

## Repository State Restrictions

Never run:

- git clean
- git clean -fd
- git clean -xfd
- git reset --hard
- git checkout -- .
- git restore .

Do not delete user work.

Do not overwrite local modifications.

---

## Tag Restrictions

Never:

- create tags
- move tags
- delete tags

unless explicitly requested.

---

## Submodule Restrictions

Never:

- add submodules
- remove submodules
- modify submodule URLs
- register submodules
- modify .gitmodules

unless explicitly requested.

---

## Remote Restrictions

Never:

- push code
- publish packages
- publish images
- create releases
- publish artifacts

without explicit authorization.

---

## Deployment Restrictions

Never:

- deploy
- publish
- release
- trigger external delivery pipelines
- trigger production workflows

without explicit authorization.

---

## Production Safety Restrictions

Never:

- modify production infrastructure
- restart production services
- rotate production credentials
- delete production resources
- modify DNS
- modify certificates

without explicit authorization.

Assume every remote environment is production until verified otherwise.

---

## Protection Compliance

If:

- git hooks
- repository policies
- branch protections
- CI safeguards

block an action,

the block is considered correct behavior.

Do not bypass protections.

Do not disable protections.

Do not search for workarounds.

---

## Escalation Rule

If successful completion requires:

- commit
- push
- pull request
- deployment
- publication
- release
- branch creation

Stop.

Explain why the action is required.

Wait for explicit authorization.

---

## Safe Operations

Allowed by default:

- read files
- edit files
- create files
- run tests
- run builds
- run validation
- run linters
- inspect git status
- inspect git diff
- inspect logs

provided no rule above is violated.

---

# Project Memory System

The following documents define project state:

- docs/ROADMAP.md
- docs/SESSION_SUMMARY.md
- docs/DECISIONS.md
- docs/BLOCKERS.md
- docs/HANDOFF.md

Chat history is not project memory.

Project documents are.

---

# Session Startup Procedure

Before performing work:

Read:

1. docs/ROADMAP.md
2. docs/SESSION_SUMMARY.md
3. docs/DECISIONS.md
4. docs/BLOCKERS.md
5. docs/HANDOFF.md

If required files do not exist:

Initialize the project memory system first.

Execution is not allowed until review is complete.

---

# Startup Validation

Before taking action verify:

- current goal is known
- current stage is known
- active blockers are known
- active decisions are known
- next action is known

If any item is unknown:

Stop.

Update documentation first.

---

# Task Lifecycle

Every task follows:

PLAN
→ EXECUTE
→ VERIFY
→ RECORD

No phase may be skipped.

---

# Planning Requirements

Before implementation define:

- objective
- scope
- success criteria

Work without success criteria must not begin.

---

# Verification Requirements

No task is complete until verified.

Accepted validation includes:

- unit tests
- integration tests
- CI results
- deployment validation
- health checks
- artifact inspection
- command output
- log validation

The following are never sufficient:

- should work
- looks correct
- probably fixed
- likely solved

---

# Evidence Requirements

Every completed task must contain evidence.

Accepted evidence:

- build output
- test output
- deployment output
- health check output
- logs
- screenshots
- generated artifacts

Without evidence:

The task remains incomplete.

---

# Session Summary Rules

SESSION_SUMMARY.md represents current project state.

Maintain:

- current goal
- current stage
- current status
- completed work
- work in progress
- open tasks
- active risks
- active decisions
- active blockers
- next action

Do not store:

- chat transcripts
- command history
- raw logs
- reasoning chains

Update after:

- task completion
- blocker creation
- milestone completion
- major decision

---

# Decision Rules

DECISIONS.md stores project-level decisions.

Create a decision record when changing:

- architecture
- infrastructure
- deployment strategy
- providers
- security controls
- workflows
- operational policies

Every decision must contain:

- identifier
- owner
- status
- rationale
- alternatives
- impact
- reversal conditions

Allowed statuses:

- ACTIVE
- SUPERSEDED
- REVERTED

Historical decisions must never be deleted.

---

# Architecture Consistency

Before architectural changes:

Review DECISIONS.md.

If a planned change conflicts with an active decision:

Stop.

Document the conflict.

Resolve the conflict before proceeding.

---

# Blocker Detection

A blocker exists when:

- approval is required
- credentials are unavailable
- permissions are unavailable
- infrastructure is unavailable
- dependencies are unavailable
- required information is unavailable
- external action is required
- progress is stalled for more than 30 minutes

Create blockers immediately.

---

# Blocker Rules

Every blocker must contain:

- identifier
- status
- priority
- owner
- issue
- impact
- required action
- resolution criteria

Allowed statuses:

- OPEN
- IN_PROGRESS
- RESOLVED
- CLOSED

---

# Blocker Stop Rule

After creating a blocker:

Allowed:

- collect evidence
- document impact
- document options
- prepare recovery steps

Forbidden:

- repeated investigation
- speculative implementation
- unrelated work
- scope expansion
- infinite retries

If progress depends on blocker resolution:

Stop execution.

---

# Scope Control

Do not:

- redefine project goals
- create milestones
- expand roadmap scope
- invent deliverables

Additional ideas belong in:

Open Tasks

Do not execute them automatically.

---

# Documentation Synchronization

Whenever changing:

- code
- infrastructure
- deployment
- configuration
- architecture
- workflows

Evaluate whether updates are required for:

- docs/ROADMAP.md
- docs/SESSION_SUMMARY.md
- docs/DECISIONS.md
- docs/BLOCKERS.md
- docs/HANDOFF.md
- design documentation
- operational runbooks

Documentation drift is a defect.

Work is incomplete until documentation is synchronized.

---

# Design Documentation Rules

Design documentation must remain consistent.

When updating a primary design document:

- synchronize chapter documents
- synchronize glossary entries
- preserve references
- preserve traceability

Open design questions must never be resolved silently.

Document:

- options
- trade-offs
- owner

before implementation begins.

---

# Markdown Quality Rules

After modifying Markdown:

Validate:

- heading hierarchy
- links
- references
- code blocks
- tables

Do not use escaped pipes inside table cells.

Rewrite content instead.

---

# Large Document Rules

For documents larger than 1000 lines:

- edit in place
- preserve structure
- minimize changes

Avoid full-document rewrites.

---

# Context Compression

Compression is mandatory when:

- session exceeds 100 turns
- milestone completes
- release completes
- context becomes difficult to review

Compression process:

1. update SESSION_SUMMARY.md
2. update DECISIONS.md
3. update BLOCKERS.md
4. generate HANDOFF.md
5. end session

Chat history is not long-term storage.

---

# Handoff Requirements

Generate HANDOFF.md when:

- ending a session
- changing operators
- changing models
- completing a milestone

A new agent must be able to continue using only:

- ROADMAP.md
- SESSION_SUMMARY.md
- DECISIONS.md
- BLOCKERS.md
- HANDOFF.md

Historical conversation must never be required.

---

# Recovery Requirement

The project must be recoverable from project documents alone.

If recovery requires historical conversations:

Documentation is incomplete.

---

# Release Gate

Deployment is not complete until:

- deployment succeeds
- verification succeeds
- health checks pass
- critical logs reviewed
- evidence recorded

No evidence = not deployed.

No verification = not released.

---

# Session Completion Checklist

Before ending a session verify:

- SESSION_SUMMARY.md updated
- DECISIONS.md updated
- BLOCKERS.md updated
- HANDOFF.md updated
- next action defined
- evidence recorded

Otherwise:

The session is incomplete.

---

# Project Principles

Prefer:

State over conversation.
Evidence over explanation.
Decisions over reasoning.
Summaries over history.
Documents over memory.
Outcomes over activity.

The project must remain:

- recoverable
- auditable
- transferable
- maintainable