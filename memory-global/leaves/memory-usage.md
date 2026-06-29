---
name: memory-usage
description: When to read / verify / write memory and what never to persist — the hygiene rules behind the CLAUDE.md three-scope table
type: reference
schema: leaf/v1
created: 2026-06-25
last_verified: 2026-06-25
---

## Difficulty

The three memory scopes (personal / global / project) say *where* a fact goes but not *when* to read it, *when* to trust it, or *what* must never be persisted. Without these rules memory drifts: stale mutable state gets presented as current, ephemeral task state bloats the index, and behavioral rules land in leaves where the always-loaded surface never sees them.

## Guidance

- **Read** the relevant scope index when the task touches a domain it knows, when the user references prior-conversation work, or before assuming repo/infra conventions.
- **Verify** specific paths / function names / flags from memory before recommending them — code moves. A leaf describing **mutable state** (PR/ticket status, working-tree contents, "pending"/"in progress" work, a session checkpoint) must be reconciled against the live source (`arc status` / `arc log`, PR API) **before** you present it as current; a checkpoint's own "next session" checklist counts only if you actually run it.
- **Write** when a fact is durable and non-obvious: corrections that should not recur, decisions and their reasons, user role and preferences, project state, prod / external-pipeline runbooks, **post-resolution task experiences** (CLAUDE.md § On task resolution).
- **Cite the source for OS / binary / version-dependent claims** — add a `> verified by: …` line (manpage, log line, command output, doc URL). Without it, future you treats a stale claim as ground truth and wastes diagnosis time.
- **Do not** write: ephemeral task state (use the task list), one-session plan drafts (use a plan file), secrets, content already covered by `CLAUDE.md`.
- **Behavioral rules** ("always X", "never Y") belong in `CLAUDE.md` or a skill / agent prompt — not in memory.

## See also

- [memory-hierarchy.md](memory-hierarchy.md) — when to spin off `<subdir>/MEMORY.md` sub-indexes.
- [leaf-schema.md](leaf-schema.md) — the `leaf/v1` section shape ordinary leaves opt into.
