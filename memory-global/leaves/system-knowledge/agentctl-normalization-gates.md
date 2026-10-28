---
name: agentctl-normalization-gates
description: "The ten normalization-closure gates of scripts/agentctl/gates.py (what each refuses, where it is called, how its gate-log label is written, which refusals overlap) and a keep/merge/delete/fix decision per gate, with real-session counts at a fixed cutoff; none is duplicate, dead or buggy"
type: reference
schema: leaf/v1
created: 2026-10-26
last_verified: 2026-10-26
---

## Difficulty

Ten functions in `scripts/agentctl/gates.py` guard the closing of a difficulty and the norm edits it
owes, and they look like one family: several are named `*_blockers`, run inside `replan`, and cite
`normalize`. A reader trimming the engine, or reading the gate log for a "dead" or "always blocking"
gate, has to rediscover three things from the code: which verb each gate guards, whether its
`_log_gate` label is written on every evaluation or only on refusal (a refusal-only label has no
denominator), and which gates really share a predicate. Rates computed over the whole log mostly
describe the test suite, because test sessions leave no engine state file and write most rows.

## Guidance

Counts below are over real sessions only (the frozen list beside the judge) and over rows before the
cutoff, copied from `measure.py gatecounts`, never retyped.

Counts cutoff: 2026-10-26T00:00:00+00:00

**Reading a count.** `real evaluated` is the number of log rows, not of calls: a label that is logged
on refusal only (or on a subset of paths) has an evaluated count that does not measure use. `-` means
the gate is never logged.

**Superseded since the decision-telemetry stage.** `renorm_final`, `escalation_orphans` and
`frozen_stage` are no longer refusal-only: `cli.py` logs them on every evaluation, and every
`_log_gate` call also appends an `engine:<label>` row to the decision log (see
`docs/operations/decision-telemetry.md`). The "refusal-only" remarks below describe the counts
at the cutoff above, not the current code.

**Per gate** (line numbers at base 00267dd6):

- `normalization_blockers` (`gates.py:536`). Refuses `replan` at DIAGNOSING closure when the difficulty
  cycle is complete and carries no normalization factor (and no `--normalize-factor` pending). Called
  from `cli.py:11922` (`_cmd_replan`) and from `difficulty_close_blockers` (`gates.py:636`). Logged
  on every evaluation that reaches it (`cli.py:11927`, `11932` refusals, `11942` pass); the waiver
  branch logs under `normalization_waiver` instead. Overlap: none with another gate at `replan`;
  `difficulty_close_blockers` reuses it by call, which is composition, not duplication.
- `normalization_waiver_blockers` (`gates.py:624`). Refuses a `replan --normalization-waiver` when the
  open record names norm targets. Called once, `cli.py:11913`. Its label `normalization_waiver` is
  shared by four more sites that are not this function: `cli.py:11345`/`11349` (the `normalize`
  verb's own refusals and acceptance), `11905` (agent-acknowledged fire), `11939` (waiver accepted
  in `replan`). Logged on refusals and on accepted waivers only, never when no waiver is passed, so
  the row counts are label-level events, not evaluations of this function. Overlap: the same
  predicate (waiver plus `norm_targets`) is written inline in `_record_normalization_waiver`
  (`cli.py:11341`) for the `normalize` verb and inside `difficulty_close_blockers`
  (`gates.py:661`) for `difficulty-close`; three verbs, three states, so none absorbs another.
- `failure_address_blockers` (`gates.py:570`). Refuses `replan` at closure when the critique has no
  `failure_address` (an explicit `not_applicable` clears). Called at `cli.py:11950` and by
  `difficulty_close_blockers`. Logged on every evaluation (`cli.py:11951`), including replans outside
  DIAGNOSING where it returns `[]`, which is why evaluated is near `normalization_blockers`. Overlap:
  none; it is the only check of that field.
- `difficulty_stack_blockers` (`gates.py:608`). Refuses `replan` in DIAGNOSING while a nested
  difficulty is open. Called at `cli.py:11753`, logged on every evaluation (`11754`). Overlap:
  `difficulty_close_blockers` refuses the opposite state (no nested record open), so they are
  complementary, not shared.
- `difficulty_close_blockers` (`gates.py:636`). Guards the `difficulty-close` verb: the nested record
  must be complete, routed and normalized (or carry a waiver while it has no norm targets). Called at
  `cli.py:11444`, logged on every evaluation (`11445`). It calls `difficulty_blockers`,
  `failure_address_blockers` and `normalization_blockers` and adds the nested-waiver rule; a different
  verb from the `replan` gates, so a composition.
- `normative_edit_blockers` (`gates.py:704`). Refuses a DIAGNOSING `replan` whose record carries norm
  targets unless the corrected plan really edits them (stage digests, `renorms`, parent-norm
  baseline). Called at `cli.py:12092`, logged on every evaluation (`12094`). Overlap:
  `renorm_retention_blockers` shares the helper `_is_installing` and so emits a similar message for a
  target that is the plan file being installed, but it judges `state.pending_renorms` (obligations
  admitted by earlier replans), this gate judges the open record's `norm_targets`; different state.
- `renorm_retention_blockers` (`gates.py:786`). Refuses any `replan` that stops naming a still-open
  norm-edit obligation in `renorms` of a stage that is yet to run. Called from `_cmd_replan`
  (`cli.py:12095`) and from the `--renormalize` path (`cli.py:11573`); logged on every evaluation at
  both (`12097`, `11578`). Overlap: see `normative_edit_blockers`; the `--renormalize` call is the only
  place the retention rule is checked on that path.
- `pending_renorm_stage_blockers` (`gates.py:849`). Refuses a PASS of the stage bound to a norm
  target whose file is unchanged. Called at `cli.py:9829` (`record-result`) and `cli.py:7737`
  (`_try_reattest`, which only declines). Never logged: it has no label, so no count exists.
  Overlap: `pending_renorm_final_blockers` reads the same `open_renorms` but refuses at another verb
  and for every obligation of the frame, not for one stage.
- `pending_renorm_final_blockers` (`gates.py:861`). Refuses `verify-final` and `resolve` while any
  open norm-edit obligation of this plan frame remains. Called at `cli.py:10294` and `10668`. Logged
  as `renorm_final` on refusal only, and only at `verify-final` (`cli.py:10296`); at `resolve` its
  blockers are folded into the `resolution` label. Zero real rows therefore means no real refusal at
  `verify-final`, not that the gate is unused: the obligation is normally discharged earlier by
  `record-result`.
- `renormalization_blockers` (`gates.py:3610`). Refuses a `replan --renormalize` that edits anything
  but a stage's `means.procedure` (protected fields, stage set, meta). Called at `cli.py:11564`,
  logged on every evaluation (`11565`). Overlap: none; it is a plan-to-plan diff, with no state
  predicate in common with the others. Its 12 real refusals are the gate doing what its docstring
  says.

**Method note.** The real-session refusal texts of each label could not be sampled from the gate log
in this stage (the log is outside the stage's read scope); the refusal conditions above are read from
the code and the gate's own tests (`test_normalization_gate.py`, `test_renormalization.py`,
`test_failure_address.py`, `test_failure_address_consequence.py`, `test_norm_mismatch.py`,
`test_optional_stages.py`). No test or docstring shows a real refusal blocking a state the gate is
meant to pass, so no `fix` row is proposed.

**Decision rules applied.** Keep is the default. A gate is `delete` only when the code shows it can
never refuse, or when every refusal it can produce is also produced by another gate at the same verb
for the same state; `merge` only for such an overlap. Every pair that shares a predicate does so at a
different verb or over different state (the table's evidence column says which), and each gate has at
least one reachable refusal, so no gate qualifies. Zero or low counts were used only to rank where to
look: `renorm_final` and `pending_renorm_stage_blockers` carry no denominator; `renorm_retention` has
398 evaluations and 0 refusals, which shows the obligation it protects was never dropped by a real
replan, not that the check is dead (it can refuse, `gates.py:811`).

## Decisions

| gate label | function | real evaluated | real refused | real refused 28d | lines | decision | evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| normalization_blockers | normalization_blockers | 2583 | 109 | 19 | 32 | keep | sole check of the normalization record at replan; reused by difficulty_close_blockers by call (gates.py:536) |
| normalization_waiver | normalization_waiver_blockers | 104 | 5 | 5 | 10 | keep | same predicate also inline for the normalize verb and in difficulty-close, but at other verbs and states (cli.py:11341) |
| failure_address_blockers | failure_address_blockers | 2573 | 3 | 0 | 30 | keep | only check of the critique's failure_address (gates.py:570) |
| difficulty_stack | difficulty_stack_blockers | 776 | 6 | 6 | 14 | keep | refuses open nested record at replan; difficulty-close refuses the opposite state (gates.py:608) |
| difficulty_close | difficulty_close_blockers | 24 | 2 | 2 | 30 | keep | guards the difficulty-close verb; composes other gates by call (gates.py:636) |
| normative_edit | normative_edit_blockers | 398 | 22 | 22 | 72 | keep | judges the record's norm_targets; retention judges pending_renorms (gates.py:704) |
| renorm_retention | renorm_retention_blockers | 398 | 0 | 0 | 31 | keep | judges pending_renorms at replan and at replan --renormalize (gates.py:786) |
| - | pending_renorm_stage_blockers | n/a | n/a | n/a | 10 | keep | never logged, so no count; refuses at record-result, a verb the final gate does not cover (gates.py:849) |
| renorm_final | pending_renorm_final_blockers | 0 | 0 | 0 | 13 | keep | refusal-only label at verify-final, no denominator; also gates resolve (gates.py:861) |
| renormalization | renormalization_blockers | 35 | 12 | 1 | 92 | keep | plan-to-plan diff for --renormalize, no shared predicate (gates.py:3610) |

## See also

- `scripts/agentctl/gates.py`, `scripts/agentctl/cli.py` (`_log_gate`, whose docstring names the refusal-only labels).
- [agentctl-acceptance-judge-gate.md](agentctl-acceptance-judge-gate.md) — another reading of a gate from `cli.py`.
