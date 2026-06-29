---
name: leaf-schema
description: The rigid section schema for ordinary memory leaves (schema:leaf/v1) — required ## Difficulty / ## Guidance / ## See also, the grandfather rule for un-migrated leaves, and what verify-leaf-structure.py enforces. Experience leaves use difficulty/v1 instead (see experience-leaf-schema.md).
type: reference
schema: leaf/v1
---

# Memory leaf schema (`leaf/v1`)

## Difficulty

A memory leaf with no rigid structure drifts into a free-floating fact whose **functional ground** — the difficulty it removes — is implicit or absent. Without that ground you cannot apply the fact correctly, judge whether it is still worth keeping, or tell signal from noise (the universal principle in `CLAUDE.md`: every leaf exists to remove a named difficulty, stated in "to achieve X, do Y" form). `leaf/v1` makes the ground a required, machine-checked section so a leaf cannot be written without naming why it exists.

## Guidance

This is the single source of truth for the **ordinary-leaf** schema. It covers reference / feedback leaves and the `system-knowledge/` leaves. **Experience leaves use a different schema** (`schema: difficulty/v1`, see [experience-leaf-schema.md](experience-leaf-schema.md)) and are out of scope here.

### Frontmatter

Standard auto-memory frontmatter plus the schema tag and temporal fields:

```
---
name: <human title>
description: <one line — used for recall; for system-knowledge, name the difficulty>
type: reference | feedback
schema: leaf/v1
created: YYYY-MM-DD
last_verified: YYYY-MM-DD
last_accessed: YYYY-MM-DD     # optional; hook-managed, never hand-edited
---
```

### Temporal frontmatter

Three date fields (ISO `YYYY-MM-DD`) apply across all leaf shapes:

- **`created`** — date the fact was first recorded; set by recording tooling, do not hand-edit.
- **`last_verified`** — date the content was last confirmed still true; equals `created` at birth; required; invariant: `last_verified >= created`.
- **`last_accessed`** — date the leaf was last explicitly opened via the `Read` tool; optional; managed entirely by the `PostToolUse(Read)` stamping hook — **never hand-edit**; day-granular (same-day re-reads produce no diff).

> **Why "accessed" means explicit Read only:** the auto-memory mechanism surfaces `MEMORY.md` automatically at session load — this does not pass through the `Read` tool and fires no hook. No "memory-recall hook" exists in the Claude Code hook system. The only hookable access event is an explicit `Read` call.

### Required sections

A leaf carrying `schema: leaf/v1` MUST contain these three H2 headers (enforced by regex on `^## `):

1. **`## Difficulty`** — the divergence between desired and actual state that this leaf removes; its functional ground. State it as the X in "to achieve X, do Y". This is the section that makes the leaf applicable and prunable.
2. **`## Guidance`** — the durable fact, rule, or runbook itself (the "do Y"). Carries the body that the leaf existed for before the schema. Cite sources for OS / binary / version-dependent claims with a `> verified by: …` line.
3. **`## See also`** — cross-references: `[[other-leaf]]` links and pointers into the difficulty graph. May be **present-but-empty** if there are genuinely none — the header is still required.

### Grandfather rule

The schema is enforced **only on opted-in leaves** (those carrying `schema: leaf/v1`) and on new leaves; existing un-migrated leaves are grandfathered:

- A leaf **with** `schema: leaf/v1` → the 3 sections are required (deny on any missing).
- A leaf **without** the tag → grandfathered. For `system-knowledge/` the carried-over **difficulty-lead baseline** still applies (the `description` names the difficulty, OR the body leads with a `> **Difficulty/Затруднение …** ` blockquote) — this is the rule the retired `verify-difficulty-lead.py` enforced, now folded into the same verifier. For other dirs, only the existing type + index guarantee applies.

Migrate a grandfathered leaf to `leaf/v1` opportunistically when you next touch it.

### What enforces it

`scripts/verify-leaf-structure.py` (subsumes the retired `verify-difficulty-lead.py`):

- **Scope:** non-experience leaves under any `leaves/**` (INCLUDING `system-knowledge/`, EXCLUDING `experience/`) plus project `.claude/agent-memory/**` leaves. `MEMORY.md` and `.gitkeep` are skipped.
- **Modes:** default scan / `--staged` / `--hook` (PreToolUse, reads `tool_input.content`, exit 2 on violation) / `<path>` — mirroring `verify-experience-leaf.py`.
- Wired into `verify-all.py` (staged-aware) and the PreToolUse verifier-hook list in `scripts/install-reminder-hooks.sh`.

## See also

- [[experience-leaf-schema]] — the `difficulty/v1` schema for experience leaves (out of scope for `leaf/v1`). It and the `principle/v1` principle leaf are two profiles — generality 0 vs ≥1 — of one difficulty-record model; `leaf/v1` here is a separate, unrelated shape, not a point on that continuum.
- [[principle-leaf-schema]] — the `principle/v1` schema (the generality≥1 profile of that same model).
- [[memory-hierarchy]] — when to spin off `<subdir>/MEMORY.md` sub-indexes.
