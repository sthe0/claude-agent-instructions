---
name: memory-temporal-frontmatter
description: The created / last_verified / last_accessed date fields every memory leaf carries — what each means, the ISO format, who writes each (recording tooling vs the PostToolUse Read hook), and why "accessed" means an explicit Read, not a recall (recall is not hookable).
type: reference
schema: leaf/v1
---

# Memory temporal frontmatter (`created` / `last_verified` / `last_accessed`)

## Difficulty

A memory leaf with no temporal metadata cannot be reasoned about over time: you cannot tell how old a fact is, when its content was last confirmed still true, or whether anything still uses it. Without age you cannot detect staleness; without a last-confirmed date you cannot tell a verified fact from a guess that has drifted; without a last-used date you cannot prune memory by disuse. Git records a file's create/modify dates mechanically, but git **cannot attribute a recall** — the moment a leaf's content is actually consulted — so the usage signal has to live in the leaf itself. This leaf defines the three date fields that carry that metadata and pins down the one subtle point: what "accessed" can and cannot mean given the harness's hook surface.

## Guidance

Every memory leaf — across all three scopes (global engineering, project, personal auto-memory) and all schemas (`leaf/v1`, `difficulty/v1`, `principle/v1`, and the bare auto-memory shape) — carries three date fields in its YAML frontmatter. All use ISO `YYYY-MM-DD` (day granularity, no time-of-day — this bounds churn).

| Field | Required | Written by | Meaning |
|---|---|---|---|
| `created` | yes | recording tooling, once | The date the fact was first recorded. Set once at birth and never changed. |
| `last_verified` | yes | recording tooling / hand on revision | The date the leaf's content was last confirmed still true. Equals `created` at birth; bump it (`record-experience.py set-last-verified`, or by hand) whenever you re-confirm or revise the fact. Must be `>= created`. |
| `last_accessed` | no (hook-managed) | the PostToolUse(Read) stamping hook | The date the leaf was last **opened via the Read tool**. Never hand-edited. Absent until the leaf is first Read after the field was introduced. |

```
---
name: <slug>
description: <one line>
type: reference | feedback | project | user
# … any schema-specific keys (schema:, generality:, resolution_confirmed_by_user:, …) …
created: 2026-06-29
last_verified: 2026-06-29
last_accessed: 2026-06-29        # optional; written only by the stamping hook
---
```

### "Accessed" = an explicit Read, NOT a recall

The intuitive meaning of "accessed" is *recall* — the moment the harness surfaces a leaf's content into the model's context (e.g. the `MEMORY.md` index loaded at session start, or a leaf quoted inside a `<system-reminder>`). **That moment is not observable to any hook.** Auto-recall does not pass through the `Read` tool and fires no hook event — verified against the official hooks reference (code.claude.com/docs/en/hooks): there is no memory-recall / context-injection hook among the events. So "stamp `last_accessed` on every recall" is **not implementable**, and no future reader should try to wire a recall hook — it does not exist.

The implementable, honest definition is: a leaf is "accessed" when it is **explicitly opened with the Read tool**. A `PostToolUse` hook matched on `Read` (the `hook-memory-accessed-stamp.py` script under `scripts/`) stamps `last_accessed = <today>` when the Read target is a memory leaf. The stamp is:

- **day-granular** — only the date, so re-reads within a day collapse to one value;
- **idempotent** — if `last_accessed` already equals today, the hook writes nothing (zero git churn on the second+ read of the day);
- **non-blocking** — the hook always exits 0 and never fails a Read; a leaf without YAML frontmatter is left untouched.

Net git cost: at most one diff per leaf per day, only for leaves actually opened that day. Personal-scope leaves are not git-tracked, so stamping them is free.

### Who writes what

- `created` + `last_verified` are **authored-event** stamps on the **write** path → `scripts/record-experience.py` emits them on `new` / `extend`; `set-last-verified` bumps `last_verified`.
- `last_accessed` is a **usage** stamp on the **read** path → the `PostToolUse(Read)` hook owns it exclusively.

Conflating the two onto one mechanism would either miss reads (write-path only) or churn on every write (read-path only) — hence two distinct mechanisms.

### Enforcement & migration

`scripts/verify-memory-index.py` requires `created` + `last_verified` (valid ISO dates, `last_verified >= created`) on every leaf; `last_accessed` is optional and only format-checked when present. `scripts/hook-memory-consistency.py` (PreToolUse) reminds when the fields are missing or malformed. Existing leaves were backfilled by the `stamp-memory-dates.py` migration script under `scripts/` (created from the git add-date, else the `YYYY-MM-DD-` filename prefix, else file mtime; `last_verified` from the last commit touching the file, else mtime).

## See also

- [[leaf-schema]] — the `leaf/v1` ordinary-leaf schema (these fields sit alongside its `name`/`description`/`type`/`schema`).
- [[experience-leaf-schema]] — the `difficulty/v1` experience-leaf schema.
- [[principle-leaf-schema]] — the `principle/v1` principle-leaf schema.
- [[memory-usage]] — when to read / verify / write memory (the hygiene rules these dates instrument).
