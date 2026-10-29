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
- **Verify** specific paths / function names / flags from memory before recommending them — code moves. A leaf describing **mutable state** (PR/ticket status, working-tree contents, "pending"/"in progress" work, a session checkpoint) must be reconciled against the live source (VCS status / VCS log, PR API) **before** you present it as current; a checkpoint's own "next session" checklist counts only if you actually run it.
- **Write** when a fact is durable and non-obvious: corrections that should not recur, decisions and their reasons, user role and preferences, project state, prod / external-pipeline runbooks, **post-resolution task experiences** (CLAUDE.md § On task resolution).
- **Cite the source for OS / binary / version-dependent claims** — add a `> verified by: …` line (manpage, log line, command output, doc URL). Without it, future you treats a stale claim as ground truth and wastes diagnosis time.
- **Do not** write: ephemeral task state (use the task list), one-session plan drafts (use a plan file), secrets, content already covered by `CLAUDE.md`.
- **Behavioral rules** ("always X", "never Y") belong in `CLAUDE.md` or a skill / agent prompt — not in memory.

## Retrieval channel

*Difficulty removed: a recorded norm sits in a leaf nobody opens because the index line that points at it never matched the moment it was needed.*

- **What it surfaces.** `scripts/hook-memory-retrieval.py` runs on every `UserPromptSubmit`, scores the prompt against each leaf's description (weighted double) and first body characters with the shared BM25F scorer (`scripts/lib/bm25f.py`), and prints up to three `[memory-retrieval] <name> — <description> — <real path>` lines under a one-line header. Global leaves and the project's `.claude/agent-memory/` are both searched, recursively, indexes excluded; a leaf is shown once per session.
- **The model judges.** The score only nominates candidates — it is not a relevance verdict. Read a surfaced leaf only if its description fits the task; ignore the rest. A silent hook means nothing cleared the cutoff, not that no leaf applies.
- **Known limits.** The cutoff is tuned on long prompts; prompts under about ten content words rarely clear it, and fewer than three content words are never scored. Measured recall and the thin margin over the best negative are recorded in `docs/operations/memory-retrieval-calibration.json`.
- **Re-calibrate** after the corpus changes materially (hundreds of added or rewritten leaves): run `python3 scripts/memory-retrieval-calibrate.py`, then update `MIN_SCORE` and `BODY_PREFIX_CHARS` in the hook to the JSON's `min_score` and `body_prefix_chars` — `test_calibration_json_matches_the_hook_constants` fails until they agree. The JSON holds aggregates only, never prompt text.

## Curating grown leaves

*Difficulty removed: a leaf that keeps gaining contexts outgrows reading and retrieval, and nobody owns splitting it.*

When `self-diagnose` reports an `oversized-leaf` finding (action: curator split), the root spawns `curator` in memory mode (`spawn-specialist.py --kind curator`, `--workdir` a linked worktree brought to the intended base) with the leaf named. The curator clusters the leaf's contexts by functional ground, moves each cluster into its own leaf, writes regularity candidates, and stops with an uncommitted diff — it never commits. The root shows that diff to the user and commits it only on an explicit accept; a rejected diff is discarded with the worktree. See `skills/specializations/curator/SKILL.md`.

## See also

- [memory-hierarchy.md](memory-hierarchy.md) — when to spin off `<subdir>/MEMORY.md` sub-indexes.
- [leaf-schema.md](leaf-schema.md) — the `leaf/v1` section shape ordinary leaves opt into.
