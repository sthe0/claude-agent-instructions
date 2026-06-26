# Task

> The form every action takes — removing a [difficulty](difficulty.md) is always framed as a task.

All work the system does is **tasks**. A task is classified by **weight**, and routing follows from the class:

- **Chat** — a bare acknowledgement, a clarification, an opinion, or a short factual answer with no file changes. Answered in-thread; no plan, no specialist, no memory recording.
- **Small change** — a tightly bounded edit (a single file, few lines, no architectural decision, no irreversible or external action). Done directly in-thread after a brief self-check.
- **Substantive** — anything else: multi-file, architectural, externally-effecting, ambiguous, or long-running. Routed through the full coordination cycle (plan → user approval → specialists → verification).

The concrete thresholds that separate the classes live in [config.md](../../config.md); the routing they drive is the task lifecycle, under **Processes** in the [documentation index](../README.md). When in doubt between two classes, pick the heavier one once and downgrade only if the work visibly fits the lighter class.
