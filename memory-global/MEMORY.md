# Global memory

Index of memories applicable across all projects on this machine. Entries are pointer lines to leaf files in `leaves/`.

Loaded into every session via `@~/.claude/memory-global/MEMORY.md` import in `CLAUDE.md`. Keep this index under ~200 lines — anything past the first 200 lines is truncated by the harness.

## How to use

- **Read:** open this file, then the relevant leaf. Do not load every leaf at session start.
- **Write:** when you learn a fact that applies across projects (user role, machine-wide tools, cross-project workflow), add a leaf in `leaves/` with the auto-memory frontmatter (`name`, `description`, `type` — `user` / `feedback` / `project` / `reference`) and add a one-line pointer here.
- **Project-only facts** belong in `<project_cwd>/.claude/agent-memory/` instead.

## Reasoning and coordination practices

- [Reasoning and task solving](leaves/reasoning-and-task-solving.md) — understand before acting, plan and approval, when stuck, memory vs prompts, self-check before first production edit.
- [Typical coordinator pitfalls](leaves/coordinator-pitfalls.md) — anti-patterns to avoid as the root coordinator; signals that point to specific corrective actions.

## Tooling and mechanics

- [Subagent resume and transcripts](leaves/subagent-resume-and-transcripts.md) — `SendMessage` resume mechanism (needs `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`), transcript layout under `~/.claude/projects/.../subagents/`, subagent auto-compaction, cleanup.
- [settings.json env precedence](leaves/claude-code-settings-env-precedence.md) — env in settings.json overrides shell env (`env -u` does not help); auth precedence ladder; what to do when an apiKeyHelper isn't enough.

Workflow-level permissions (separate from memory): `~/claude-agent-instructions/permissions/` + `scripts/permissions-cli.py`. Not a memory leaf — operational config.

## System knowledge

Durable facts about systems, processes, organizational structure, and codebase architecture. Leaves live in `leaves/system-knowledge/`. Recording criteria in `~/.claude/CLAUDE.md` § Memory § `system-knowledge/` leaves.

<!-- Add one-line pointers to leaf files as system knowledge accumulates. -->

## Recent retrospectives

- [Session retrospective 2026-05](leaves/session-retrospective-2026-05.md) — period summary, top mistakes, ticket startup checklist, self-check gates.
- [Coordination machinery refactor 2026-05-24](leaves/experience/2026-05-24-coordination-refactor.md) — added task-weight triage / CLARIFY / PLAN-READY / depth cap / two-turn self-improvement / `config.md` for constants; two rounds of silent-architectural-decision corrections; lessons on consequence-of-change being a change, "config" meaning a separate file, `rg`-sweep before commit.
- [Code-driven enforcement arc 2026-05-25](leaves/experience/2026-05-25-code-driven-enforcement-arc.md) — nine-iteration build-out of `verify-*` scripts, hooks, structured permissions, spawn wrapper, cost log + three rule additions; lessons on process-as-code pacing, verify-script ROI, JSON-over-YAML for stdlib portability, missed-leaf-at-resolution as a recurring failure mode.
- [Soft-control hooks arc 2026-05-26](leaves/experience/2026-05-26-soft-control-hooks-arc.md) — frontmatter sentinel + CLAUDE.md token-trim + 3 soft-control hooks (self-critique / tracker / push reminders) + 1 rejected proposal (hard cap on memory); lessons on warn-vs-block trade-off and the instruction-surfaces-vs-content-stores distinction.
