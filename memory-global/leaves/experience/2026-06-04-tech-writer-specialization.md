---
name: 2026-06-04-tech-writer-specialization
description: Created the tech-writer specialization (Russian technical writer/editor); lessons on verify-language proximity rule for non-English skill content and the CLAUDE.md 400-line cap collision.
type: reference
resolution_confirmed_by_user: "Да, решено (Recommended)"
---

# 2026-06-04 — tech-writer specialization

User asked for a new skill: a professional Russian technical writer/editor (popular-blog persona) that produces clear, concise Russian without English calques or jargon, invoked when writing README.md, right after plan creation, and when writing detailed Russian comments. The ask was also a behavior correction — the agent's Russian regularly carries calques (`запушить`, `пайплайн`) and bureaucratic clutter. Routed through `self-improvement`.

## Final plan as executed

No separate plan file (small-change-sized steps under an approved in-thread proposal). Steps: (1) new `skills/specializations/tech-writer/SKILL.md`; (2) wire into the enumeration points — `CLAUDE.md` (delegation table, specializations table, two specialist enumerations), `cursor/rules/claude-code-sync.mdc`, `README.md`, `policy.md` layout tree, `verify-layout-contract.sh`; (3) `setup-symlinks.sh` (auto-flattens `specializations/*`); (4) verify + commit + push. User picked the name `tech-writer` and the trigger scope "plan + README + long answers (short replies excluded)" via `AskUserQuestion` before applying.

## Difficulties

1. **CLAUDE.md was exactly at the 400-line cap.** Adding two table rows (one to the delegation table, one to the bottom specializations table) pushed it to 402 → `lint-prose-length` FAIL. Resolved by collapsing the `### Task-spawned subagents` header into a `**bold lead-in.**` paragraph (−2 lines, no content lost) rather than extracting a section to a leaf. Keep both tables consistent (a new specialization belongs in both) and reclaim the lines from genuine structural slack.

2. **`verify-language` blocked the commit on the skill's own Russian content.** The check requires a `Language exception` note **within 3 lines of each** Cyrillic line. A single note above a 19-row calque table does NOT cover the rows far from it → 24 violations. It also flagged bare Cyrillic I had sprinkled into English prose (`Префер`, `Удаляй`) and Russian glosses in parentheses. Fix: the linter **strips fenced code blocks and backtick/guillemet spans before checking** — so I moved the calque table into a ```fenced block``` (`term → replacement` lines) and wrapped short inline Russian examples in backticks; rewrote the prose verbs to English. 0 violations after.

## Artifacts

- `skills/specializations/tech-writer/SKILL.md` (new).
- Commit `2c089cf` on `~/claude-agent-instructions` main, pushed to origin.
- Live in the session skill catalog immediately after `setup-symlinks.sh`.

## Lessons

- **A skill whose output or working material is non-English must hold that content in fenced code blocks or backtick/guillemet spans**, not in plain markdown tables/prose. `verify-language`'s per-line, within-3-lines exception rule does not scale to a multi-row table — one note can't cover it. Fenced blocks and `` `...` `` / `«...»` spans are stripped before the check, so they need no exception note. Saves rediscovering the 24-violation wall.
- **Adding any specialization costs ≥2 lines in CLAUDE.md** (both the delegation and the bottom reference tables). With CLAUDE.md chronically at its 400 cap, plan to reclaim lines in the same edit — collapsing a thin `###` subsection header into a bold lead-in is a clean, lossless source.
- Creating a specialization is mechanical once the enumeration points are known: `CLAUDE.md` ×3 sites, cursor mirror, `README.md` ×1, `policy.md` tree, `verify-layout-contract.sh`. `setup-symlinks.sh` handles the symlink (it flattens `specializations/*`).

## Self-critique of the agent system

The `self-improvement` `policy.md` documents the language rule and that quoted/fenced regions are stripped, but it does **not** warn that a new **skill about non-English output** is itself the worst case for the per-line proximity rule. A one-line note in `policy.md` § Instruction language ("non-English working material → fenced block / backtick spans, not tables") would have pre-empted difficulty 2. This is a single observed instance, not yet a recurring pattern across leaves — recording the lesson here is enough for now; do not add the `policy.md` note until a second non-English-skill task hits the same wall (avoid premature machinery per `policy.md` § What NOT to encode).

## Cost, effort, and tool usage

- Single in-thread session, no spawns ($0 spawn cost). Wall-clock ~10 min. One user intervention (the bundled name/trigger/apply question) plus the final resolution+push gate.
- Skills/specializations used: `self-improvement` ×1 (route the skill-creation as agent-system change). No `Agent`/`Task` spawns — work fit the in-context substantive carve-out (all steps single-file, small).
- Cost driver: the `verify-language` pre-commit gate (one blocked commit → one rewrite pass). Cheap because the gate caught it locally before push.
