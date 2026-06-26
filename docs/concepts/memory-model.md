# Memory model

> The means of accumulating experience in overcoming difficulties — not just a fact store.

Memory closes the learning loop: *[difficulty](difficulty.md) → overcame it → recorded how → reused it next time.* It is what makes the system better at removing difficulties over time, rather than re-deriving the same solution every session.

There are **three scopes**, picked by purpose (write to the most specific one that fits):

- **Personal (auto-memory)** — facts about the user, conversational preferences, and continuity of "what we agreed on", in the user's language. Claude Code's native auto-memory mechanism.
- **Global engineering** — cross-project engineering patterns, reasoning practices, runbooks, and retrospectives, in structured English. Loaded into every session.
- **Project (local)** — project-specific runbooks (pipelines, ticket detail, repo conventions), shared via the project's own git.

All three share the same shape: a short `MEMORY.md` index pointing at leaf files, each leaf carrying frontmatter. A fact that qualifies for two scopes goes in the most specific one, never duplicated. The detailed scope table, the leaf schema, and the read/verify/write hygiene rules live in [CLAUDE.md](../../CLAUDE.md) § Memory; the memory-scopes component doc (under **Components** in the [documentation index](../README.md)) covers the mechanics.
