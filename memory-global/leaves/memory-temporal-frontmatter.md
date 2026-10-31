---
name: memory-temporal-frontmatter
description: Two-field temporal contract for every memory leaf — created (required, set once) and last_verified (required, bumped on revision) — plus the retired note for last_accessed (removed; validators now reject re-introduction).
type: reference
schema: ontology/v1
converted_from: leaf/v1
created: 2026-06-29
last_verified: 2026-06-30
---

# Memory temporal frontmatter (`created` / `last_verified`)

## Difficulty

A memory leaf with no temporal metadata cannot be reasoned about over time: you cannot tell how old a fact is or when its content was last confirmed still true. Without age you cannot detect staleness; without a last-confirmed date you cannot tell a verified fact from a guess that has drifted. Git records a file's create/modify dates mechanically, but those are commit-level events — the leaf needs its own authored-event stamps so tooling can require and verify them independently of git history.

## Distinction

Separates two authored-event date stamps carried in every memory leaf's frontmatter, across all scopes and schemas, from git's commit-level create and modify dates. created is the date the fact was first recorded, set once at birth and never changed; last_verified is the date the content was last confirmed still true, equal to created at birth and bumped on re-confirmation or revision, never earlier than created. Together they separate the age of a fact from the time it was last confirmed true, so a verified fact can be told from a guess that has drifted. A third notion, last_accessed (the date of an explicit Read), is retired: no consumer read it, it only churned the working tree, and recall does not pass through the Read tool, so validators reject it if present.

## Adequacy

The distinction is inadequate if leaves whose last_verified is current are found to hold facts that had drifted, because last_verified is bumped on edits without re-confirmation and so does not separate verified from drifted; or if staleness questions are answered as well by git commit dates alone, so the authored stamps add nothing; or if a consumer appears that needs a recall or access date and cannot be served by the two fields.

## Guidance

Every memory leaf — across all three scopes (global engineering, project, personal auto-memory) and all schemas (`difficulty/v1`, `regularity/v1`, `principle/v1`, `ontology/v1`, and the bare auto-memory shape) — carries two required date fields in its YAML frontmatter. Both use ISO `YYYY-MM-DD` (day granularity, no time-of-day — this bounds churn).

| Field | Required | Written by | Meaning |
|---|---|---|---|
| `created` | yes | recording tooling, once | The date the fact was first recorded. Set once at birth and never changed. |
| `last_verified` | yes | recording tooling / hand on revision | The date the leaf's content was last confirmed still true. Equals `created` at birth; bump it (`record-experience.py set-last-verified`, or by hand) whenever you re-confirm or revise the fact. Must be `>= created`. |

```
---
name: <slug>
description: <one line>
type: reference | feedback | project | user
# … any schema-specific keys (schema:, generality:, resolution_confirmed_by_user:, …) …
created: 2026-06-29
last_verified: 2026-06-29
---
```

### Who writes what

- `created` + `last_verified` are **authored-event** stamps on the **write** path → `scripts/record-experience.py` emits them on `new` / `extend`; `set-last-verified` bumps `last_verified`.

### Enforcement & migration

`scripts/verify-memory-index.py` requires `created` + `last_verified` (valid ISO dates, `last_verified >= created`) on every leaf. `scripts/hook-memory-consistency.py` (PreToolUse) reminds when the fields are missing or malformed. Existing leaves were backfilled by the `stamp-memory-dates.py` migration script under `scripts/` (created from the git add-date, else the `YYYY-MM-DD-` filename prefix, else file mtime; `last_verified` from the last commit touching the file, else mtime).

### last_accessed — retired

`last_accessed` was removed as a frontmatter field. It was stamped by a `PostToolUse(Read)` hook on every explicit Read, but:

- No consumer ever read the field — no prune, audit, or report used it.
- Its only effect was working-tree churn on every memory Read (one diff per leaf per day).
- "Accessed" as *recall* is not implementable anyway — auto-recall does not pass through the `Read` tool and fires no hook event; a recall hook does not exist in the harness's hook surface.

**Removal:** the writer hook (`hook-memory-accessed-stamp.py`) was deleted (Stage 1). All `last_accessed:` frontmatter lines were stripped from existing leaves (Stage 2). Validators now **reject** `last_accessed` if present (presence → retired-field error), so re-introduction is a hard validation failure. Do NOT re-add this field.

## See also

- [[leaf-schema]] — the retired `leaf/v1` section shape (these fields sat alongside its `name`/`description`/`type`/`schema`).
- [[experience-leaf-schema]] — the `difficulty/v1` experience-leaf schema.
- [[regularity-leaf-schema]] — the `regularity/v1` knowledge-leaf schema.
- [[principle-leaf-schema]] — the `principle/v1` principle-leaf schema.
- [[memory-usage]] — when to read / verify / write memory (the hygiene rules these dates instrument).
