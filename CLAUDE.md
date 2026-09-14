# CLAUDE.md

Claude-specific entry point for `anvilkit-services`. The shared working rules for every development agent live in [`AGENTS.md`](AGENTS.md); this file does not repeat them.

## Before starting any task

1. Read `AGENTS.md` in full with the Read tool. A Markdown link does not load its target into context, and this file is not a substitute for it.
2. Then read [`docs/README.md`](docs/README.md), the relevant parts of [`docs/architecture/architecture.md`](docs/architecture/architecture.md), and the sections that the navigation table in `AGENTS.md` names for the task. Read `docs/architecture/security.md` when the task touches credentials, candidate code, retrieved content, model or tool sends, or external effects.

## Claude Code specifics

- `docs/`, `contracts/`, `outputs/` and `logs/` are gitignored. A Git worktree or fresh clone therefore lacks the architecture documents and the legacy contracts; run documentation and contract tasks in the primary checkout and edit files there.
- Git is read-only for Claude (`AGENTS.md`, section 4): no commit, push, staging, branch switching or history rewriting unless the user asks for that exact action in the current message. Commit and push may also be hook-blocked; a block is the policy working.
- `python3 tools/check-docs.py` is the read-only documentation check. `python3 tools/run-verification.py` regenerates derived artifacts and runs container/browser proofs in every mode, including `--static`; never run it as a read-only check.
- Recalled memories and older session notes may describe the previous architecture (v3.12, CD-01 to CD-05, W1–W3, P0/P1 stages, `docs/design/`, `docs/architecture/plans/`). The current baseline is architecture V4.0 revision 2 under `docs/architecture/`; verify any remembered path, command or gate against the checkout before using it.
- Report as `AGENTS.md`, section 8 requires: which checks ran, their PASS/FAIL/UNEXECUTED results, and what remains unverified. Never present `NOT_RUN` or `NOT_VERIFIED` items as passing.
