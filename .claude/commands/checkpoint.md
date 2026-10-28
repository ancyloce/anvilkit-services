---
description: Save current project state and prepare the next Claude Code session
---

Stop implementation and create a project checkpoint.

Follow `CLAUDE.md`.

Review the current session, repository state, and:

- `docs/ROADMAP.md`
- `docs/SESSION_SUMMARY.md`
- `docs/DECISIONS.md`
- `docs/BLOCKERS.md`
- `docs/HANDOFF.md`

Then:

1. Record verified completed and in-progress work.
2. Update roadmap status without expanding scope.
3. Record significant decisions without deleting history.
4. Update blockers using evidence-based statuses.
5. Regenerate `HANDOFF.md` with one next action and explicit `Do Not Do` items.
6. Calculate and record the project health score.
7. Validate that a new session can continue without chat history.
8. Return a concise closeout report and stop.

Do not modify application code.

Do not stage, commit, push, create a PR, create or switch branches, create worktrees, modify Git history, deploy, release, publish, or bypass protections.