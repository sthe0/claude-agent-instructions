# Instruction language (English by default)

## Metadata

| Field | Value |
|------|----------|
| `last_verified` | 2026-05-21 |
| `staleness_triggers` | new non-English block in `~/claude-agent-instructions/` without exception note |
| `revalidate` | `rg '[\\p{Cyrillic}]' ~/claude-agent-instructions --glob '*.md'` — each hit must have adjacent exception comment |

## Rule

**Default:** all agent instructions — prompts in `agents/`, `CLAUDE.md`, `cursor-rules/`, `memory-global/`, and README policy sections — are written in **English**.

**Exception:** a non-English fragment is allowed only if **immediately next to it** (same paragraph or the line above/below) there is an explicit note **why English cannot be used** (product constraint, quoted user gate phrase, legal term, etc.).

**Not covered by this rule (no exception note required):**

- **User-facing replies** — agents may answer in the user's language; that is output, not stored instruction text.
- **Quoted examples** of what the user might say (`"ok"`, `"do it now"`) — keep quotes literal; surrounding prose stays English.
- **Proper nouns and API identifiers** (Tracker, Arcadia, `arc`, ticket keys) — not "another language".

## When editing instructions

| Action | Requirement |
|--------|-------------|
| New paragraph in git instructions | English |
| Translating legacy Russian | English + remove obsolete duplicate |
| Must keep non-English | Add `> **Language exception:** …` (markdown) or `<!-- Language exception: … -->` on the adjacent line |
| Review PR / self-improvement | Reject new Cyrillic (or other non-EN prose) without exception note |

## Canonical placement

- Policy summary: [../../CLAUDE.md](../../CLAUDE.md) § Instruction language
- Cooperation README: [../../README.md](../../README.md) § Agent cooperation → Principles
- Enforcer: **self-improvement** on feedback; **memory** does not relax this for domain runbooks in **local** `~/.claude/memory/` (local tree may stay Russian until migrated — document in local INDEX if needed)
