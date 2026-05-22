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

## Recent retrospectives

- [Session retrospective 2026-05](leaves/session-retrospective-2026-05.md) — period summary, top mistakes, ticket startup checklist, self-check gates.
