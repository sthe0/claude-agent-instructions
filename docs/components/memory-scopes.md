# Memory scopes

> The three scopes the system writes durable experience into — personal, global engineering, and project — and the leaf-and-index shape they all share.

Memory is how the system gets better at removing difficulties over time; the *why* is in the [memory model](../concepts/memory-model.md) concept. This page is about the **where**: the three scopes, picked by purpose, always writing to the most specific one that fits.

| Scope | Where | Purpose |
|---|---|---|
| **Personal (auto-memory)** | `~/.claude-agent/projects/<cwd-hash>/memory/` | Facts about the user, conversational preferences, "what we agreed on" continuity. |
| **Global engineering** | [memory-global/](../../memory-global/) (imported into every session) | Cross-project engineering patterns, runbooks, retrospectives. |
| **Project** | `<project>/.claude/agent-memory/` (shared via the project's git) | Project-specific runbooks — pipelines, ticket detail, repo conventions. |

All three share one file shape: a short `MEMORY.md` **index** of pointer lines, with the detail in `leaves/` files. A leaf carries frontmatter (`name`, `description`, `type`) and, for the rigid kinds, a fixed section schema — see [leaf-schema.md](../../memory-global/leaves/leaf-schema.md) for the ordinary `leaf/v1` shape. When a scope's content grows or splits into a coherent sub-domain, it spins off a `<subdir>/MEMORY.md` sub-index rather than bloating the top index; that discipline is [memory-hierarchy.md](../../memory-global/leaves/memory-hierarchy.md).

The highest-value leaf is the **experience leaf** (`difficulty/v1` schema): one recurring difficulty, every context it arose in, and the plan that removed it each time. Recording one is the last step of a resolved task — searched-for-then-extended-or-created — so the next similar task starts from accumulated experience. The recording discipline is part of [resolution and experience](../processes/resolution-and-experience.md).
