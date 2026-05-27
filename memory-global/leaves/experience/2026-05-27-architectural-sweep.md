---
name: 2026-05-27-architectural-sweep
description: Session-long sweep — token-saving audit of DEEPAGENT sessions led to 5 distinct architectural improvements (allow-list parity, project-memory trigger leaves, skill-first dispatch, memory hierarchy, systemic-pattern-scan discipline) all converging on the same root pattern "capability exists, trigger does not fire"
type: reference
resolution_confirmed_by_user: "Да, resolved (Recommended)"
---

User asked an audit of 3 recent DEEPAGENT-tagged sessions for over-asking permissions, missed specializations, and unused `yandex-guru`. The session expanded organically as each finding revealed a sibling gap; what ended as 9 commits (4 global, 5 project) was conceptually one architectural sweep — every fix instance of the same meta-pattern.

## Final plan as executed

In-conversation plan (no separate plan file; carve-out — each commit ≤ small-change-max-lines). Phases as they unfolded:

1. **Audit transcripts.** Parsed 9 jsonl files from `~/.claude/projects/-home-the0-arcadia-robot-deepagent/`. Counted tool calls per kind, AskUserQuestion questions, Skill invocations, Agent invocations, top Bash commands. Key numbers: 117 Edit + 29 Write per session unallow-listed; 0 yandex-guru; 0 overcome-difficulty; 7 Skill total (3 unique); 482 Bash.
2. **First fix wave** (token-saving — commit `db327af15e` project arc):
   - A: path-scoped Edit/Write + read-only MCP in `.claude/settings.local.json`.
   - B: project memory leaves `yandex-guru-trigger.md`, `plan-design-paths.md`, `bundle-asks.md`, project CLAUDE.md "Yandex-specifics first" section, project experience leaf `2026-05-27-token-saving-audit.md`.
3. **Second fix wave** (overcome-difficulty mirror — commits `ff5062604a` arc + previous turn). User asked "how many overcome-difficulty invocations?" — answer was 0. Added `overcome-difficulty-trigger.md` project leaf, project CLAUDE.md section.
4. **Third fix wave** (skill-first dispatch — commits `3084795` git + `579bcd6761` arc). User asked to analyze other skills for invisibility. Numbers: Skill calls = 7 (3 unique) across 9 sessions; Agent calls = 0. Added global `skill-first-dispatch.md` + project `skill-dispatch-yandex.md` mapping ~25 ops to skills; CLAUDE.md "Skill-first over direct CLI" addition.
5. **Fourth fix wave** (read-only Bash allow-list — commit `9ec167c` git). User pointed out I still asked permission for `ls/head/cat/find/wc` etc. while editing their instructions. Added comprehensive read-only Bash + path-scoped Read/Edit/Write to global `~/.claude/settings.json`; added "Policy ↔ settings.json alignment" paragraph to `acting-without-asking.md`.
6. **Dedup pass** (commit `07c3534a47` arc). Checked global vs project CLAUDE.md duplication. Found one real dupe (overcome-difficulty repeated in project CLAUDE.md though already in 3 places globally) — removed.
7. **Memory hierarchy** (commits `31d88b4` git + `0e6ffae332` arc). User noted memory was physically 3-level but navigationally 2-level. Spun off `experience/MEMORY.md` and `system-knowledge/MEMORY.md` sub-indexes (project + global); added global `memory-hierarchy.md` leaf with spin-off principles; CLAUDE.md inline pointer (0 new lines, file at 400 ceiling). Patched `verify-experience-leaf.py` regex to exclude MEMORY.md sub-indexes from leaf detection.
8. **Systemic-pattern-scan discipline** (commit `fbb45df` git, current turn). User reframed: "your instructions ARE the plan you work by." So OD applies naturally to the agent-system-as-plan. Added the resolution-time pattern-scan discipline as a new global leaf + OD SKILL.md addendum + CLAUDE.md inline extensions in § What to record and § Auto-trigger self-improvement.

## Difficulties

- **Single-question turnament reflex.** First batch ask had 5 options for one question and crashed with `InputValidationError: too_big maximum 4`. Replaced with bundled multi-question form. Surface signal for the eventual `bundle-asks.md` leaf.
- **Almost wrote experience-leaf without explicit "resolved" confirmation.** Caught by `scripts/verify-experience-leaf.py` requiring `resolution_confirmed_by_user` frontmatter. Asked first, then wrote. Mechanism worked as designed.
- **Verify-experience-leaf false-positive on new sub-index.** `experience/MEMORY.md` (sub-index file) doesn't carry experience-leaf frontmatter, but the regex caught it. One-character fix (negative lookahead).
- **Project-only constraint misread on round 4.** User clarified the constraint was "don't put project-specific content in global", not "don't touch global at all". Cleaner mental model adopted from that point.
- **400-line CLAUDE.md ceiling met twice.** Both pattern-scan additions and memory-hierarchy additions had to be inline extensions of existing lines/bullets, not new sections. The constraint forces concision (good) but signals that the ceiling will eventually demand a refactor toward leaves carrying more detail (next-session concern).

## Artifacts

**Global git (`claude-agent-instructions`, `main` branch):**
- `3084795` skill-first dispatch + fewer-permission-prompts habit (+ leaf, CLAUDE.md note).
- `9ec167c` acting-without-asking § Policy ↔ settings.json alignment.
- `31d88b4` memory hierarchy + 4 sub-indexes + verify-experience-leaf regex fix.
- `fbb45df` systemic-pattern-scan discipline + OD SKILL.md note + CLAUDE.md inline extensions.

**Project arc (`arcadia_claude_local`, `users/the0/agents-bootstrap-deepagent` branch):**
- `db327af15e` settings allow-list (paths + MCP) + project CLAUDE.md yandex-guru section + project leaves (yandex-guru-trigger, plan-design-paths, bundle-asks).
- `03a7074a8f` project experience leaf for token-saving audit.
- `ff5062604a` overcome-difficulty trigger leaf + project CLAUDE.md section (later removed in dedup).
- `579bcd6761` project skill-dispatch-yandex.md table.
- `07c3534a47` dedup — drop OD section from project CLAUDE.md.
- `0e6ffae332` project experience/+system-knowledge/ sub-indexes.

**Machine-local (not under VCS):** `~/.claude/settings.json` extended allow-list (~50 new entries: read-only Bash, arc/git read-only, path-scoped Read/Edit/Write).

## Lessons

1. **"Capability exists, trigger doesn't fire" is a meta-pattern, not a list of incidents.** yandex-guru, overcome-difficulty, fewer-permission-prompts, ~95 unused skills — all the same shape. Naming the meta-pattern unlocked the architectural fix (project-memory trigger leaves with concrete signals + skill-first dispatch leaf + systemic-pattern-scan discipline as the proactive scanner). Without naming it, would have produced 4 disconnected fixes.
2. **Inline extensions of existing lines is a useful tool for hitting an L-line ceiling.** When CLAUDE.md is at the limit, extending an existing bullet (no new line) lets a new rule land without bloating. Limit signal: when you can no longer extend cleanly, that's the signal that detail must move to a leaf.
3. **Sub-indexes are a precondition for the pattern-scan discipline.** Without `experience/MEMORY.md`, "scan recent experience" doesn't have a cheap entry point. Memory hierarchy and pattern-scan are mutually reinforcing: hierarchy made the scan affordable; the scan justified the hierarchy.
4. **Dedup pass on global vs project CLAUDE.md should be routine.** Found one real dupe (overcome-difficulty repeated). The pattern to watch: when project memory adds a trigger because global isn't firing, it's tempting to also add a CLAUDE.md paragraph in the project — that's the duplicate-injection moment.
5. **User reframes can shorten 100 lines of distinguishing prose.** "Your instructions ARE the plan you work by" collapsed an entire mental model (task plan vs agent-system, meta-OD vs OD) into one sentence. Listen for those moments; they often reveal that I was about to write artificial distinctions.

## Self-critique of the agent system

- **The whole need for this session is a self-critique.** That so much architectural surface (5 distinct improvements) was waiting to be discovered means the previous sessions' self-critique sections were either too tactical or were skipped. The systemic-pattern-scan discipline added today exists to make this proactive — but it's untested in actual future use. Mark for revisit after 3–5 sessions.
- **`AskUserQuestion` 4-option limit not documented.** Hit the `too_big maximum 4` error early in the session. Not in any leaf I could find. Either add to acting-without-asking.md or to a CLAUDE.md § Escalation note. (Not done this session — small enough to defer; flagged here.)
- **Spawn-specialist not used once across all 9 commits.** Carve-out applied — every commit fit small-change-max-lines, no multi-file refactor. But part of why: the work was reactive (find-and-fix) not designed (plan-then-execute). Planner would have produced a tighter sweep if I had spawned one at the start instead of letting the work unfold one user message at a time. Not necessarily wrong (the user's questions WERE the plan), but worth noting.
- **Memory-hierarchy leaf at 96 lines and pattern-scan leaf at 96 lines are at the upper end** of what's comfortable to read in one sitting. The principles inside are dense enough that further factoring would harm coherence, but next time prefer shorter prose with more examples.

## Cost & effort

- Wall-clock: ~5 hours of conversation (09:39 → 13:47).
- Spawns: 0. All work in-thread by manager + Skill inline (self-improvement invoked multiple times).
- $ spent: estimate $4–6 — long-context audit of 9 jsonl files (largest 6.4 MB), 9 commits across two VCSs, repeated re-reads of CLAUDE.md / MEMORY.md / leaves as edits accumulated.
- User interventions / re-prompts: 7 substantive ones, all refinements not corrections — each unlocked the next fix wave.
- Resources that drove cost:
  - Reading and parsing transcripts (`jsonl` files in `~/.claude/projects/...`). Single largest line item. Sub-indexes will help next time; raw jsonl scanning remains expensive.
  - Repeated re-reads of `CLAUDE.md` (398→400 lines, edited 4 times in session). The 400-cap means each addition needs careful in-line surgery.
  - Multiple verify-all runs after each commit (cheap individually, ~$0.01 each, but adds up).
