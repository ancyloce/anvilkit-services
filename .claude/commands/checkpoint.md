---
description: Save a project-memory checkpoint under AGENTS.md, report, and stop
---

# Save a Checkpoint

When the user explicitly invokes `/checkpoint`:

1. Follow [AGENTS.md](../../AGENTS.md), including its [Project memory policy](../../AGENTS.md#project-memory).
2. Execute the [shared checkpoint workflow](../../AGENTS.md#memory-checkpoint) once in checkpoint mode. Include the recovery step only if the request also asks to close or hand off the session.
3. Finish with that workflow's report-and-stop step.

Use the shared workflow's scope and draft-only handling; this command adds no permissions or separate memory rules.
