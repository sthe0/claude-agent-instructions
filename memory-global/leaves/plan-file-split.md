---
name: plan-file-split
description: For substantive multi-stage plans that grow above ~20 KB, split the single plan markdown into an index file (`<slug>.md`) plus per-stage files (`<slug>-stage-<N>.md`). The index carries the structural sections (Problem, Stages-overview pointers, Final verification, Risks); each stage file carries that stage's Output / Expected result image / Actual effort. This lets later Read calls pull only the active stage instead of re-loading the entire plan.
type: reference
created: 2026-05-27
last_verified: 2026-07-22
---

# Plan-file split

> **Legacy (`.md`-era) + one corrected hazard — read this first.** This leaf predates the
> `agentctl` engine, which now writes a **single TOML** plan (`~/.claude-agent/plans/<slug>.toml`)
> and tracks stages itself; the per-stage-`.md` split below is a token-economy technique for the
> old markdown plans, not a requirement. **Its functional ground is token economy — never
> dispatchability.** The old § "How this interacts with `spawn-specialist.py`" advised
> *concatenating* files into one `--plan` argument; that path reproduced the E2BIG dispatch failure
> (Linux `MAX_ARG_STRLEN` = 131072 B per argv string), because `agentctl dispatch` inlines the whole
> plan into the spawn prompt. Since commit `5d96cd6` `spawn-specialist.py` delivers that prompt via
> **stdin**, so a plan of any size dispatches cleanly — splitting is **no longer needed to make a
> large plan dispatchable**, only to save re-read tokens. See the corrected § below.

The planner skill (`skills/specializations/planner/SKILL.md`) writes plans to `~/.claude-agent/plans/<slug>.md`. For a typical 4–6-stage plan this is fine. For larger plans, observed cost in the 2026-05-27 deepagent sessions reached **25 KB per plan file, re-read 3–10 times** as the work moved through stages. See [token-economy-plan.md](token-economy-plan.md) item 6.

The harness Read dedup ([system-knowledge/harness-read-dedup.md](system-knowledge/harness-read-dedup.md)) suppresses redundant Reads of an unchanged plan. The split still helps the **changed-plan** path: when the manager appends `Actual effort:` for stage N or refines stage N+1, the entire single-file plan is considered changed, and the next Read returns the full 25 KB again. With per-stage files, only the touched stage file changes and the next Read of an untouched stage stays deduped.

## When to split

Apply the split if **any** of:

- The plan exceeds ~20 KB or ~600 lines.
- More than three stages will need `Actual effort:` updates as work progresses.
- Stages have substantially independent reference-file lists / specialist instructions.

For small plans (single sprint, ≤ 3 stages, < 10 KB): keep the single file. Splitting overhead is not worth it.

## Layout

```
~/.claude-agent/plans/<slug>.md                    # index
~/.claude-agent/plans/<slug>-stage-1-<short>.md    # stage 1 detail
~/.claude-agent/plans/<slug>-stage-2-<short>.md
~/.claude-agent/plans/<slug>-stage-3-<short>.md
~/.claude-agent/plans/<slug>-final-verification.md # if Final verification block is long
```

### Index file (`<slug>.md`)

Must still satisfy the legacy markdown-plan validator (retired along with the markdown plan class — see the note at top) — it checked for the required `##` headings. Keep all four:

1. **Problem.** One paragraph.
2. **Stages.** A summary table with one row per stage:

   ```
   | # | Name | Specialist | Output | Stage file |
   |---|---|---|---|---|
   | 1 | Discover schema | developer | inventory.md | [stage-1-discover.md](<slug>-stage-1-discover.md) |
   | 2 | … | … | … | [stage-2-….md](<slug>-stage-2-….md) |
   ```

   At least one `Expected result image:` line must appear in this section — put it in the table or just below as a global summary. The detailed `Expected result image:` per stage lives in the stage file.
3. **Final verification.** One paragraph or pointer to `<slug>-final-verification.md`.
4. **Risks.** One paragraph.

### Stage file (`<slug>-stage-<N>-<short>.md`)

Per-stage detail:

```markdown
# Stage <N> — <name>

Specialist: developer | thinker | …
Owns: this step
Reads: <list of plan-index sections / earlier stage outputs>

## Output

<artifact>

## Expected result image

<concrete observable + expected value/state>

## Actual effort

<post-hoc, filled by manager after stage completes>

## Notes

<rejected options, reference file list, dependencies>
```

No frontmatter required — these are plan working files, not memory leaves.

## How this interacts with `spawn-specialist.py`

**TOML-engine reality (current).** You do not spawn specialists by hand for a plan the engine drives —
`agentctl dispatch` reads `meta` + the active stage from the single `.toml`, inlines the plan into the
spawn prompt, and pipes the whole prompt to `spawn-specialist.py` via **stdin** (commit `5d96cd6`).
Prompt size is therefore bounded only by memory, not by `MAX_ARG_STRLEN` — the 172 KB
question-provenance plan dispatches cleanly. **Do not** concatenate plan files into one `--plan`
argument or otherwise push a large payload onto argv: that path raises `OSError: [Errno 7] Argument
list too long` before the child starts. This is the corrected form of the old advice, which suggested
exactly that concatenation and predated the stdin channel.

**Legacy `.md` split (only if you still hand-split a markdown plan).** Pass the index and just the
active stage file — but merge them into the prompt *content* (or let the caller assemble one prompt),
never as two argv strings, and never via a concatenated-into-argv scratch file.

## What the planner skill should do

The planner does **not** auto-split. It writes a single file by default. If during planning the planner sees the plan growing beyond the trigger thresholds above, it should:

1. Stop, decide to split.
2. Write the index + stage files.
3. Return `PLAN-READY:` pointing at the index path. (The manager reads the index to present to the user; the user approves; later spawns pull individual stage files as needed.)

A planner that produces single 30-KB plans is not wrong — but for long-running tickets, the manager re-reading the plan repeatedly costs more than the split would have cost to author.
