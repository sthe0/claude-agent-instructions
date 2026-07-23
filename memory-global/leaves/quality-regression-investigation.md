---
name: quality-regression-investigation
description: Runbook for the task-quality tracking loop — how the 1-5 rating is proposed and confirmed at the resolution gate, the rating rubric, the in-flight signal inventory, and the investigation procedure (commit shortlist → hypothesis → fix ladder) when the scorecard flags a quality degradation after instruction edits.
type: reference
schema: leaf/v1
refs:
  - policy-effectiveness-tracking
  - autocompact-threshold-policy
created: 2026-07-03
last_verified: 2026-07-03
---

# Quality-regression investigation runbook

## Difficulty

Edits to the agent's instructions and code (CLAUDE.md slimming, rule mechanization,
skill/hook changes) can silently degrade task-solving quality: the prose that carried
salience is gone, the mechanized gate misses the perception half, or a rule lost its
functional ground. Without a per-task quality series stamped with the instructions-repo
HEAD, the degradation is invisible until the user complains — and by then nobody can
say WHICH commit range caused it, so the fix is guesswork and the instructions either
bloat back or stay broken.

## Guidance

### 1. Rating flow at the resolution gate

Every `agentctl resolve` requires `--quality 1..5` (refused without it). The rating is
**agent-proposed, user-confirmed** — inside the SAME resolution `AskUserQuestion`,
never a separate free-text question:

- Propose a rating from the rubric (§ 2) adjusted by this task's in-flight signals (§ 3).
- Option labels (user's language): «Решена, оценка N (Recommended)» FIRST (N = the
  proposed rating), then adjacent ratings as alternates («Решена, оценка N−1»,
  «Решена, оценка N+1» when in range), «Не решена» LAST.
- On a click of the proposed option → `--quality N --quality-by user-confirmed`;
  an adjacent option → `--quality M --quality-by user-adjusted`; an "Other" free-text
  answer carrying a rating → `--quality-by user-other --quality-note "<text>"`.
- On «Не решена» → NO rating, no resolve — route to `overcome-difficulty`; the task
  is not resolved.

The resolve writes one row to `~/.local/log/claude-task-quality.jsonl` carrying
`instructions_head` (git HEAD of the instructions repo at resolve time) — this stamp
is what makes regression → commit-range correlation possible later.

### 2. Rating rubric

| Rating | Criterion |
|---|---|
| 5 | First-pass resolution: no user corrections, no replans, done criterion met as planned |
| 4 | Minor friction: ≤1 correction or clarifying round-trip, no replan, outcome as intended |
| 3 | Resolved, but with replans, repeated corrections, or a materially revised approach |
| ≤2 | Substantial problems: wrong scope delivered, user had to re-explain, partial/reworked outcome |

Propose the rubric value, then shade it by the § 3 signals (e.g. the rubric says 4 but
three free-text AskUserQuestion answers accumulated this task → propose 3).

### 3. In-flight signal inventory

Accumulated automatically per session by `scripts/policy-scorecard.py` (joined to
task rows by session id):

- `n_user_corrections` — user prompts matching `CORRECTION_RE` (behavior corrections).
- `n_user_questions` — user prompts containing a question (confusion / missing info).
- `n_freetext_askuser_answers` — AskUserQuestion answers matching no offered option
  label (the offered options missed the user's intent).
- `n_interrupts` — user interrupted a running turn.
- Per-task (from the resolve row): `n_replans`, `n_failed_stage_results`,
  `n_difficulty_records`, `spawn_count`, `total_cost_usd`.

### 4. Investigation procedure (when the scorecard flag fires)

`scripts/policy-scorecard.py` flags: task-quality avg < 3.5 or down > 0.5 vs the
previous window; correction/free-text rate up > 50%. On fire it prints the
instruction-commit range between the two windows and a hint to run the helper.

1. Run `scripts/quality-regression-investigate.py --good <rev> --bad <rev>` (or
   `--good-days A --bad-days B` to resolve revs from the ledger). It shortlists
   commits in the range touching salience-bearing paths (CLAUDE.md, config.md,
   `skills/`, `agents/`, `memory-global/`, hooks, `scripts/agentctl/`), tags each
   (`prose-removed` / `rule-moved` / `mechanized`), and ranks largest net prose
   deletion first (the failure-mode prior: salience lives in prose).
2. Per shortlisted commit, form a hypothesis: WHAT behavior did the removed/moved
   prose or the new mechanization carry, and does the degradation signature (which
   § 3 signals rose) match losing it?
3. Verify against the ledger: do the degraded tasks' `instructions_head` values
   postdate the suspect commit?
4. Apply the fix ladder (§ 5). Re-check: the flag must clear over the next window —
   the silent interval is the observable, not the edit itself.

### 5. Fix ladder (in order — try each rung before the next)

1. **Mechanize** — turn the lost rule into a structural gate (engine state, hook,
   typed contract) so it cannot fade again; prose remains only for the perception
   half the model genuinely owns.
2. **Restore salience without growth** — reword/reposition within existing text
   (move the rule up, bold the trigger, add it to an existing table row) at zero
   net line cost.
3. **Re-add prose within ceilings** — last resort: re-add the removed text, staying
   under `claude-md-max-lines` / `claude-md-max-chars`; if the ceiling blocks,
   extract something less load-bearing to a leaf first.

Never clear the flag by deleting the check or widening the threshold without a
recorded reason.

### 6. Loop closure

Record each fired-flag investigation as a movement in
[[policy-effectiveness-tracking]] (a Contexts entry: flag → hypothesis → fix rung →
observed clearance one window later), so the "instruction edit → measured outcome →
adjustment" loop stays closed.

## See also

- [[policy-effectiveness-tracking]] — the standing instrument this loop feeds;
  scorecard mechanics and the weekly nudge.
- `scripts/policy-scorecard.py` — signal counters, the Task quality section,
  degradation flags, commit-range output.
- `scripts/quality-regression-investigate.py` — commit-shortlist helper
  (flag → ranked hypotheses).
- `scripts/agentctl/cli.py` — the `resolve --quality` contract and the
  task-quality ledger row schema.
- [[autocompact-threshold-policy]] — precedent of a measured-regression →
  tuned-constant loop.
