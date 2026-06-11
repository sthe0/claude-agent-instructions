---
name: 2026-06-04-non-english-skill-content-language-wall
description: Difficulty — creating a specialization whose working material is non-English hits verify-language's per-line within-3-lines exception wall (one note can't cover a multi-row Cyrillic table); plus the CLAUDE.md 400-line cap collision when adding specialization rows (a recurring minor friction).
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "Да, решено (Recommended)"
refs: [2026-05-26-agent-system-plan-vs-reality-drift]
---

# Non-English skill content vs. verify-language; the CLAUDE.md cap

## Difficulty
Creating the `tech-writer` specialization (a Russian writer/editor whose working material is a calque→replacement table) hit two walls: (1) `verify-language` requires a `Language exception` note within 3 lines of *each* Cyrillic line — a single note above a 19-row table doesn't cover the far rows → 24 violations; (2) CLAUDE.md was exactly at its 400-line cap, and adding the two required specialization rows (delegation table + bottom reference table) pushed it to 402 → `lint-prose-length` FAIL.

## Order & criterion
Hold non-English working material in **fenced code blocks or backtick/guillemet spans** (`verify-language` strips these before checking, so they need no exception note), not in plain markdown tables/prose. When adding a specialization, **reclaim CLAUDE.md lines in the same edit** — collapsing a thin `###` subsection header into a bold lead-in is a clean lossless source. **Acceptance check:** `verify-language` reports 0 violations; `lint-prose-length` passes; the specialization is wired at all enumeration points (CLAUDE.md ×3 sites, cursor mirror, README, `policy.md` tree, `verify-layout-contract.sh`; `setup-symlinks.sh` flattens `specializations/*`).

## Contexts

### 2026-06-04 — tech-writer specialization creation
Moved the calque table into a fenced ```term → replacement``` block and wrapped inline Russian examples in backticks; rewrote prose verbs to English → 0 violations. Reclaimed 2 CLAUDE.md lines by collapsing the `### Task-spawned subagents` header to a bold lead-in. Commit `2c089cf`. **Recurring secondary friction — the 400-line cap collision recurred** the same day on the token-economy task ([[2026-06-04-verify-load-bearing-axis]] forced a reclaim by compressing git/instruction-language sections to pointers) and twice during the 2026-05-27 architectural sweep ([[2026-05-26-agent-system-plan-vs-reality-drift]]). Each time the reclaim was clean and the file got tighter, so the cap is *doing its job*; the agreed escalation (per `policy.md` § What NOT to encode) is: if a future addition cannot reclaim without losing substance, extract a CLAUDE.md section to an imported leaf — not before.

## Cost
Single in-thread session, $0 spawn, ~10 min. Cost driver: the `verify-language` pre-commit gate (one blocked commit → one rewrite pass) — cheap because it caught the 24-violation wall locally before push. Lesson held back from `policy.md` deliberately (single instance at the time); this leaf is the record so a second non-English-skill task can match it and *then* add the `policy.md` note.
