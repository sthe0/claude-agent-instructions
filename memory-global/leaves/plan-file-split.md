---
name: plan-file-split
description: For substantive multi-stage plans that grow above ~20 KB, split the single plan markdown into an index file (`<slug>.md`) plus per-stage files (`<slug>-stage-<N>.md`). The index carries the structural sections (Problem, Stages-overview pointers, Final verification, Risks); each stage file carries that stage's Output / Expected result image / Actual effort. This lets later Read calls pull only the active stage instead of re-loading the entire plan.
type: reference
---

# Plan-file split

The planner skill (`skills/specializations/planner/SKILL.md`) writes plans to `~/.claude/plans/<slug>.md`. For a typical 4–6-stage plan this is fine. For larger plans, observed cost in the 2026-05-27 deepagent sessions reached **25 KB per plan file, re-read 3–10 times** as the work moved through stages. See [token-economy-plan.md](token-economy-plan.md) item 6.

The harness Read dedup ([system-knowledge/harness-read-dedup.md](system-knowledge/harness-read-dedup.md)) suppresses redundant Reads of an unchanged plan. The split still helps the **changed-plan** path: when the manager appends `Actual effort:` for stage N or refines stage N+1, the entire single-file plan is considered changed, and the next Read returns the full 25 KB again. With per-stage files, only the touched stage file changes and the next Read of an untouched stage stays deduped.

## When to split

Apply the split if **any** of:

- The plan exceeds ~20 KB or ~600 lines.
- More than three stages will need `Actual effort:` updates as work progresses.
- Stages have substantially independent reference-file lists / specialist instructions.

For small plans (single sprint, ≤ 3 stages, < 10 KB): keep the single file. Splitting overhead is not worth it.

## Layout

```
~/.claude/plans/<slug>.md                    # index
~/.claude/plans/<slug>-stage-1-<short>.md    # stage 1 detail
~/.claude/plans/<slug>-stage-2-<short>.md
~/.claude/plans/<slug>-stage-3-<short>.md
~/.claude/plans/<slug>-final-verification.md # if Final verification block is long
```

### Index file (`<slug>.md`)

Must still satisfy `verify-plan-file.py` — it checks for the required `##` headings. Keep all four:

1. **Problem.** One paragraph.
2. **Stages.** A summary table with one row per stage:

   ```
   | # | Name | Specialist | Output | Stage file |
   |---|---|---|---|---|
   | 1 | Discover schema | developer | inventory.md | [stage-1-discover.md](<slug>-stage-1-discover.md) |
   | 2 | … | … | … | [stage-2-….md](<slug>-stage-2-….md) |
   ```

   At least one `Expected result image:` line must appear in this section — put it in the table or just below as a global summary, so `verify-plan-file.py` passes. The detailed `Expected result image:` per stage lives in the stage file.
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

When spawning a specialist for stage N, pass the index plus only that stage's file:

```bash
spawn-specialist.py \
  --kind developer \
  --plan ~/.claude/plans/<slug>.md \
  --plan ~/.claude/plans/<slug>-stage-<N>-<short>.md \
  ...
```

(Tool currently accepts a single `--plan` arg — if you need both, concatenate them into a scratch file and pass that. Repeating-flag support can be added later if this pattern becomes common; see [token-economy-plan.md](token-economy-plan.md) item 6 follow-up.)

## What the planner skill should do

The planner does **not** auto-split. It writes a single file by default — `verify-plan-file.py` accepts that. If during planning the planner sees the plan growing beyond the trigger thresholds above, it should:

1. Stop, decide to split.
2. Write the index + stage files.
3. Return `PLAN-READY:` pointing at the index path. (The manager reads the index to present to the user; the user approves; later spawns pull individual stage files as needed.)

A planner that produces single 30-KB plans is not wrong — but for long-running tickets, the manager re-reading the plan repeatedly costs more than the split would have cost to author.
