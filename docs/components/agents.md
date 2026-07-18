# Agents and the spawn model

> The specialization roles the manager-actor delegates to — planner, developer, code-reviewer, and the rest — and the spawn mechanism that runs each in its own process.

The [universal manager-actor](../concepts/manager-actor.md) resolves small tasks itself and **delegates** larger or specialized steps to a **specialization**: a role packaged as a skill under [skills/specializations/](../../skills/specializations/). A specialization can run two ways — **inline** (via the Skill tool, sharing the current context) or **spawned** (a fresh `claude -p` process with its own context and budget). Spawning is the default for large or multi-step work, and the only way to get a genuinely fresh context — which is the whole point of a reasoning check.

The specializations and what each is for:

- [planner](../../skills/specializations/planner/SKILL.md) — decomposition into stages with dependencies, risks, and done criteria.
- [developer](../../skills/specializations/developer/SKILL.md) — writing, refactoring, debugging, and reviewing production code.
- [code-reviewer](../../skills/specializations/code-reviewer/SKILL.md) — a maintainability / readability / reusability pass over a diff, as the developer's self-review or an independent review.
- [thinker](../../skills/specializations/thinker/SKILL.md) — an independent reasoning check on a non-trivial chain, where fresh context is load-bearing.
- [tech-writer](../../skills/specializations/tech-writer/SKILL.md) — authoring or polishing technical prose in the language of the dialogue.
- [yandex-cloud-expert](../../skills/specializations/yandex-cloud-expert/SKILL.md) — Yandex Cloud and `yc` operations.

The full inventory with triggers lives in [skills.md](skills.md). The spawn template, budget tiers, the recursion cap, and the return markers a spawned specialist reports back (`COMPLETED` / `PLAN-READY` / `INCOMPLETE` / `CLARIFY` / `REPLAN` / `PERMISSION-REQUEST` / `ESCALATE`) are documented in [spawning-specialists.md](../../memory-global/leaves/spawning-specialists.md); how the manager handles each marker is in [handling-escalations.md](../../memory-global/leaves/handling-escalations.md).

This repo ships **no** standalone subagents in [agents/](../../agents/) — that directory is reserved and documented in its own [README](../../agents/README.md); the specializations above are the roles in active use. Machine-local and project-local subagents live outside this repo (`~/.claude-agent/agents/` and `<project>/.claude/agents/`).
