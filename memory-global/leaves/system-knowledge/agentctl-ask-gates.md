---
name: agentctl-ask-gates
description: "The three Ask gates of agentctl (resolution ask, content-waiver ask, plan-approval ask): per gate the marker the AskUserQuestion must carry, the preceding event it must follow, the escape, the verb and call sites, how evaluations are logged and what code is shared; the test of 'one parameterized ask-evidence gate' (verdict: partial) and a keep/merge/fix decision per gate with real-session counts"
type: reference
schema: leaf/v1
created: 2026-10-26
last_verified: 2026-10-26
---

## Difficulty

Three refusals in agentctl demand that the user was asked a question: `resolve` wants an answered
`[resolution-ask]`, `record-landing --accept-content-loss` wants an answered `[content-waiver-ask]`, and the
plan-approval ask is checked by the `PreToolUse` hook `hook-plan-delivery-gate.py` (and, on its delivery-proof
half, by `approve`). They look like one rule with three parameters (marker, preceding event, escape flag), and
a reader trimming the engine is tempted to fold them into one gate. The code shows how far that holds: two
of them scan the same transcript for the same shape; the third judges a different object. The gate log also
counts only two of the three, and the third (plan approval) is split between the log and the judge ledger.

## Guidance

Counts are over real sessions only (the frozen list beside the judge) and rows before the cutoff, copied from
`measure.py gatecounts --family ask`, never retyped.

Counts cutoff: 2026-10-26T00:00:00+00:00

Hypothesis verdict: partial

**Per gate** (line numbers at base 00267dd6). The triple the hypothesis names is: marker the ask must carry,
preceding event it must follow, escape flag that bypasses it.

- `resolution_ask` (`cli.py:10592` `_resolution_ask_gate`, `gates.py:256` `resolution_ask_gate_active`).
  - *Marker:* `[resolution-ask]` (`state.py:1356`) in an option label or description; in the question stem
    it is refused with its own message (`has_marker_in_question_text`).
  - *Preceding event:* the session's latest `verify_final` stamp in engine history (`at`); the ask must be
    timestamped after it and answered (`ask_user_question_calls` third element).
  - *Escape:* `--resolution-ask-unverifiable '<why>'`, honoured only when the transcript cannot be read
    (refused with a note when it can); env `AGENTCTL_RESOLUTION_ASK_GATE=0/1`; inactive for non-SUBSTANTIVE
    sessions. Both recorded in the returned `{override, unverifiable}` record, never silent.
  - *Verb and call sites:* `resolve`, `cli.py:10683` in `cmd_resolve`; `cmd_close` reaches it only through
    its read-only probe of `cmd_resolve`, and `verify-final` only names the marker (`cli.py:10472`).
  - *Logging:* the blockers fold into the `resolution` label (`cli.py:10685`), which also carries the
    other resolve blockers; the gate's refusals are told apart by the `resolution ask: ` text prefix
    (`cli.py:10585`). Logged on every `resolve` call that reaches it.
- `content_waiver_ask` (`cli.py:2748` `_content_waiver_blocker`, with `_content_waiver_asks` `cli.py:2705`,
  `_chose_waiver_option` `cli.py:2689`, `_option_carries_waiver_marker` `cli.py:2684`).
  - *Marker:* `[content-waiver-ask]` (`state.py:1360`) in an option label or description.
  - *Preceding event:* the latest `record_landing` event of any stage (one answer licenses one waiver), plus
    an earlier `record_landing` of the stage itself; the ask must be timestamped after it, answered, and the
    answer must be a marked option among a mix of marked and unmarked ones (consent, not just an answer).
  - *Escape:* none. The flag `--accept-content-loss` is the thing the gate guards, not an escape from it.
  - *Verb and call sites:* `record-landing`, one call site, `cli.py:2817` in `cmd_record_landing`.
  - *Logging:* none (no `_log_gate` label), so counts are n/a; the use is read from the code and from
    `test_landed_check_recorded_landing.py` (waiver cases at lines 574-626).
- `approval_ask` (`hook-plan-delivery-gate.py:353` `gate_decision` with `_same_turn_denied`, `_delivery_observed`,
  `_has_show_full_plan_option`, `_receipt_stale_reason`, `_judge_approval_ask`, `_decide_session`;
  `advisor.py:1651` `approval_ask_prefilter`, `advisor.py:1661` `judge_approval_ask`; `gates.py:2521`
  `plan_presentation_active`, `gates.py:2663` `plan_presentation_blockers`, `_delivery_stamp_blocker`
  `gates.py:2630`, `_receipt_binding_blocker` `gates.py:2604`, `_no_stamp_blocker` `gates.py:2566`).
  - *Marker:* `[show-full-plan]` (`state.py:1350`) in an option label or description, **required** on the ask
    (`_NO_MARKER_REASON`). The ask itself is identified not by the marker but by a model judge behind a
    lexical prefilter (`advisor.py:1651`, `1661`), fail-open.
  - *Preceding event:* not an engine event but the plan's registered essence rendering landing as a
    completed turn's FINAL text (`delivered_final_texts`) after the presentation receipt (`_delivery_observed`),
    with the plan not submitted in the same turn (`_same_turn_denied`) and the receipt bound to the current
    plan version (`plan_sha256`, `_receipt_binding_blocker`). It checks the ask *before* it is asked, and not
    that it was answered.
  - *Escape:* `confirm-delivery --by --note --escape-reason` writes a delivery override stamp; env
    `AGENTCTL_PLAN_PRESENTATION=0`; the judge kill switch `CLAUDE_APPROVAL_ASK_SEMANTIC=0`
    (`hook-plan-delivery-gate.py:164`).
  - *Verb and call sites:* the `PreToolUse` hook on an `AskUserQuestion` at `PLAN_READY`
    (`_decide_session`, `hook-plan-delivery-gate.py:558`) and `approve`, `cli.py:7232` in `cmd_approve`
    (delivery-stamp half only; not an ask predicate).
  - *Logging:* `cmd_approve` writes one `plan_approval` row merging every approve blocker, so the presentation
    gate's refusals are told apart by blocker text (`present-plan`, `delivery proof`, `delivery override`,
    `delivery stamp`). The hook never writes the gate log: it writes `plan_delivery` rows to the judge ledger,
    and a deny is a `final` row with a directive. The count in the table adds both, same cutoff and real-session
    rule.

**Code shared across the three.** `transcript_turns.has_marker_option` (`lib/transcript_turns.py:178`) is
the one marker walker for all three (hook, `_resolution_ask_gate`, `_option_carries_waiver_marker`);
`has_marker_in_question_text` (`:204`) serves resolution only; `ask_user_question_calls` (`:222`) serves
resolution only. The content-waiver ask does **not** use it: `_content_waiver_asks` is a second transcript
parser with the same ask/answered/timestamp walk plus `toolUseResult.answers`. The hook uses
`delivered_final_texts` and `latest_turn_start`, which neither of the other two uses.

**Test of the hypothesis** ("one ask-evidence gate parameterized by (marker, preceding event, escape flag)").
- *resolution_ask and content_waiver_ask:* both reduce to "an answered AskUserQuestion with the marker in an
  option, timestamped after an engine-recorded event". Residue beyond the three parameters: resolution has a
  stem-misplacement diagnostic, a missing-timestamp message, a refused-escape note and the `verify_final`
  stamp precondition; content-waiver has the consent clause (marked option chosen among a mix) and the
  earlier-stage-record precondition. Named residue, same predicate: the hypothesis holds with it.
- *approval_ask:* does not reduce. The marker is a required option, not the identifier of the ask; the ask is
  identified by a model judge; the preceding event is a transcript event (a delivered final text), not an
  engine event; it fires before the ask is asked rather than checking an answered ask at a later verb; it
  also binds the receipt to `plan_sha256`, which is no ask predicate. Residue is most of the gate.
- Verdict *partial*: two gates share a predicate with named residue; the third does not. A single function
  for all three would have to keep a judge, a pre-ask denial and a receipt binding behind parameters, which
  are the gate.

**Method note.** Real-session refusal texts of the gate log could not be sampled in this stage (the log is
outside the stage's read scope); the refusal conditions are read from the code and the gates' tests
(`test_resolution_ask_marker_gate.py`, `test_landed_check_recorded_landing.py`, `test_plan_presentation.py`).
No test or docstring shows a real refusal blocking a state the gate is meant to pass, so no `fix` row is
proposed. Counts were used to rank where to look: `resolution` refused 40 of 1106 and `plan_approval` 518 of
4505 on its mixed row, so both gates are live; the content-waiver ask has no label to count.

**Decision rules applied.** Keep is the default; no delete (each gate guards a distinct verb). A `merge:` row
names one shared function, `ask_after_event`: scan the transcript once for answered asks carrying a marker
in an option, with their time and chosen label, and filter to those after an engine-recorded anchor. It is
the same in every row it merges and keeps each gate's verb, refusal text and escape.

**Residue that stays in its gate outside `ask_after_event`** (for the merge rows, so stage 6 keeps it there):
- `resolution_ask`: the `verify_final` stamp lookup and its no-stamp message; the stem-misplacement,
  missing-timestamp and predates-verify-final diagnostics; `--resolution-ask-unverifiable` and the env
  override with their record; `RESOLUTION_ASK_RULE_HINT`.
- `content_waiver_ask`: the earlier-`record_landing` precondition and the any-stage anchor; the consent
  clause (`_chose_waiver_option`: marked option chosen among marked and unmarked ones, fail-closed on several
  results); its refusal texts.
- `approval_ask`: keep; no merge. Everything stays.

## Decisions

| gate label | gate | real evaluated | real refused | real refused 28d | lines | decision | evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| resolution | resolution_ask | 1106 | 40 | 40 | 81 | merge:ask_after_event | same marked-ask-after-engine-event scan as the content-waiver ask, over a different parser (scripts/agentctl/cli.py:10592, scripts/lib/transcript_turns.py:222) |
| - | content_waiver_ask | n/a | n/a | n/a | 89 | merge:ask_after_event | second hand-written copy of the transcript ask walk, adding answers (scripts/agentctl/cli.py:2705) |
| plan_approval | approval_ask | 4505 | 518 | 234 | 543 | keep | model-judged ask, pre-ask denial on a delivered final text and plan_sha256 binding: most of the gate is residue (scripts/hook-plan-delivery-gate.py:353) |

## See also

- [agentctl-normalization-gates.md](agentctl-normalization-gates.md) — the sibling findings leaf for the ten normalization-closure gates.
- `scripts/agentctl/cli.py`, `scripts/agentctl/gates.py`, `scripts/hook-plan-delivery-gate.py`, `scripts/lib/transcript_turns.py`.
