# CLAUDE.md

Claude-specific entry point for `anvilkit-services`. The shared working rules for every development agent live in [`AGENTS.md`](AGENTS.md); this file does not repeat them.

## Before starting any task

1. Read `AGENTS.md` in full with the Read tool. A Markdown link does not load its target into context, and this file is not a substitute for it.
2. Then read [`docs/README.md`](docs/README.md), the relevant parts of [`docs/architecture/architecture.md`](docs/architecture/architecture.md), and the sections that the navigation table in `AGENTS.md` names for the task. Read `docs/architecture/security.md` when the task touches credentials, candidate code, retrieved content, model or tool sends, or external effects.

## Claude Code specifics

- Only `docs/archive/`, `outputs/`, `logs/`, `.local/` and build caches are gitignored. Inputs added by a task are untracked until the user stages them, and `.local/` (dev foundation credentials, snapshots, the preserved legacy worktree) exists only in the primary checkout; a Git worktree therefore lacks them. Run tasks in the primary checkout and edit files there. `contracts/` and `services/agent/{api,control,workflow}` are their own repositories (see [architecture](docs/architecture/architecture.md#repositories)): changes inside them are changes of those repositories.
- Git is read-only for Claude (`AGENTS.md`, section 4): no commit, push, staging, tagging, submodule registration, branch switching or history rewriting in any of the repositories unless the user asks for that exact action in the current message. Commit and push may also be hook-blocked; a block is the policy working.
- Read-only checks: `sh tools/verification-env.sh --static` (pinned CPython 3.12 venv under `.local/`, then the chain), or with `.local/verification-venv/bin/python`: `tools/check-docs.py`, `tools/check-contracts.py`, `tools/check-source-export.py`, `tools/generate-contracts.py --check`, `tools/run-verification.py --static`; the contracts repository alone with `sh tools/verification-env.sh` inside `contracts/`; each service alone with `GOWORK=off` in `services/agent/{api,control,workflow}` (Control's sqlc drift check is `sh tools/sqlc.sh --check` there). `generate-contracts.py` without `--check` rewrites the checked-in contract consumers and Control's sqlc output; `deploy/dev/up.sh` starts containers and a kind cluster and migrates the dev database forward; `deploy/dev/{api,control,workflow}-chart.sh install` deploy the services into that cluster.
- Recalled memories and older session notes may describe the previous architecture (v3.12, CD-01 to CD-05, W1–W3, P0/P1 stages, `docs/design/`, `docs/architecture/plans/`). The current baseline is architecture V4.0 revision 2 under `docs/architecture/`; verify any remembered path, command or gate against the checkout before using it.
- Report as `AGENTS.md`, section 8 requires: which checks ran, their PASS/FAIL/UNEXECUTED results, and what remains unverified. Never present `NOT_RUN` or `NOT_VERIFIED` items as passing.

## Git is read-only

- Never `git commit`, `git push`, or open a PR unless the user explicitly asks in that message (commit/push may also be hook-blocked — a block is the policy working, not an obstacle to route around). Default: leave changes uncommitted and report modified files, flagging those inside submodules and nested repositories (`contracts/`, `services/agent/{api,control,workflow}`).
- Never stage, amend, rebase, merge, cherry-pick, reset, clean, tag, switch branches, register or change submodules (`git submodule add/update/sync`, `.gitmodules`) or publish a package unless asked for that exact action. Never force-push, never push `main`. Read-only git is always fine. Prepare such operations locally and present their concrete targets for authorization.
- Never publish, deploy, or send anything to an external service without task authorization.
