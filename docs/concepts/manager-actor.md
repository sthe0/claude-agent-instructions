# Universal manager-actor

> The single executor — one disciplined agent, not a swarm.

The main Claude Code dialog **is** the manager; there is no separate manager bot. It takes any [task](task.md) and drives it to a verified result: resolving the task itself when it is small, or coordinating specialists (planner, developer, reviewer, and others) when it is large. It is one disciplined actor wearing different hats, not a swarm of disconnected agents.

"Universal" means the **root** of this repository defines the properties that hold for *every* task on the machine, while each **project** adds its own properties on top:

```text
effective agent = root (universal)  ⊕  project A specifics
                                    ⊕  project B specifics
                                    ⊕  …
```

Project-specific runbooks, memory, and skills live in each project's own `<project>/.claude/` tree (committed to that project's git), never in this root repo — the root never embeds project knowledge. How the manager-actor coordinates a task (under **Processes**) and the discipline it follows when many developers share one set of instructions (the consensus architecture, under **Architecture**) are both reachable from the [documentation index](../README.md).
