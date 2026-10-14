# ADR-0011: Parent-side accounting — what a delegated child costs, and how its escalations end

- Status: Accepted 2026-10-15
- Plan: `parent-accounting.toml`

## Context

[ADR-0009](0009-specialist-as-manager.md) (portion 4a) lets a parent stage hand its norm to a spawned child, and [ADR-0010](0010-child-contour.md) (portion 4b) lets that child plan, self-approve and escalate inside the norm. The parent's own contour did not see the child's side of that delegation. The effort-divergence trigger, which notices a norm that is missing something essential about the real situation, was therefore blind exactly where delegation is deepest. Three gaps, each measured on the base commit:

- **Spend.** The parent summed cost-ledger rows whose `plan_path` equals its own plan. The child's own process row carries that path and was counted. The rows of the child's own spawns (a grandchild, its thinker and code-reviewer runs) carry the child's `plan_path` and were dropped. A running child contributed nothing until its process exited.
- **Wall-clock.** The parent's active minutes grew only at the prompt hook, capped at 30 minutes per prompt gap, and `dispatch` blocks for the whole child run. A three-hour child therefore added at most 30 minutes. The child's own minutes stay near zero, because its turns are not user prompts.
- **Closure.** The link between a parent difficulty opened by a child's escalation and the child's difficulty lived only in the history log. Nothing typed it, a second dispatch of the same `REPLAN` opened a second difficulty, and the child's record stayed open with no way to see that the parent had closed it.

The premise of the order, carried from 4a and 4b: the manager is the trivial specialization; a spawned specialist is a manager plus a role.

## Decision

1. **Pull, not push (D1).** The parent computes its subtree's spend from the cost ledger at its existing spend-refresh sites. Push-on-child-resolve is rejected: the child would write the parent's state (ADR-0010 D4), a running child would count nothing, and a crashed child would be dropped.

2. **Row attribution (D2).** Every cost row `spawn-specialist.py` writes carries `spawner_session`: the engine session that owns the spawn (`--session`, else `AGENTCTL_OWN_SESSION`, else absent). The descendants of a session are the sessions whose `parent_link` chain reaches it, cycle-guarded and bounded by `max-recursion-depth`; the session itself is not among them. A row whose `spawner_session` is a descendant (a linked grandchild's process row, or the row of a descendant's own unlinked thinker or code-reviewer spawn) is counted in a subtree term and nowhere else. Every other row is counted by `plan_path` as before, including the direct child's process row (its spawner is the parent) and the spawns of an unlinked spawn (their spawner is no descendant). Each row is therefore counted exactly once. A per-descendant watermark, `SessionState.effort_subtree_seen`, survives sub-plan push and pop, so custody changes neither re-count nor drop a row. A running child's finished descendants count live; the child's own process counts at its exit.

3. **Wall-clock is the union of intervals (D4).** `dispatch` appends `{child_session, stage_index, started, ended, dispatcher_pid}` to `SessionState.effort_child_runs` and saves it before the blocking spawn, then closes it afterwards without overwriting the hook's concurrent prompt stamps. The wall-clock actual is the session's own active minutes plus the union of the child-run intervals, an open run counting up to a `now` the caller passes. The prompt hook books `min(max(0, gap − covered), cap)` per gap, where `covered` is the part of the gap inside a child run, so no minute is counted twice and child runs are not capped. A run whose dispatcher process is gone is closed at the next read, at the child state's last write. Grandchild runs lie inside the child's run and are not added.

4. **A typed escalation link (D5).** The child's `critique --norm-target parent` sets `escalated_to = {parent_session, stage_index, norm_digest}` on its difficulty. The parent difficulty that dispatch opens carries `escalated_from = {child_session, child_uid, norm_digest}`. The parent opens at most one difficulty per `(child_session, child_uid)`, over open and closed difficulties; a repeated `REPLAN` gets the standing difficulty's directive.

5. **Closure is derived on read, and an orphan blocks.** The pure `escalation_status(child_difficulty, parent_state)` returns `open`, `closed` or `unadopted`; `children` (in the parent) and `status` (in the child, reading the parent read-only) show it, so neither session writes the other. `gates.escalation_orphans` lists each linked child's verified escalation addressed to this session that has no counterpart, and `verify-final` is blocked while the list is non-empty. `adopt-escalation --child <sid>` opens the counterpart through the same route as dispatch and is refused when the child's record does not verify. It belongs to `AGENTCTL_USER_AUTHORITY_VERBS`, so a spawned process cannot call it on its parent.

6. **What the premise answers fix.** One landing, with no partition (QM). Wall-clock is the union of child runs plus the uncovered part of prompt gaps (QP). Child-side closure is derived on read (QQ). An unadopted escalation blocks `verify-final` (QR). The names are the ones above (QS).

## Premises

Premise questions of the order, with the disposition and the answer (or, for an assumed premise, the basis) recorded in the premise gate of session 09321034-d4e2-43ae-8bae-bdde96759e00. For an assumed premise the right-hand side is its basis, not a decision of the user.

- QM (escalated): user 2026-10-12: one landing, no partition
- QN (assumed): claude -p total_cost_usd covers the whole process including in-process Agent sub-agents; only separate claude -p processes write their own rows
- QO (assumed): the cost row is written on child exit; there is no live cost feed while a child runs
- QP (escalated): user 2026-10-12: union of child-run intervals, uncapped, plus only the uncovered part of prompt gaps
- QQ (escalated): user 2026-10-12: child-side closure status derived on read; parent never writes child state
- QR (escalated): user 2026-10-12: an unadopted escalation blocks the parent's verify-final; remedy adopt-escalation
- QS (assumed): internal names consistent with agentctl naming; renaming later is cheap
- QL (researched): yes: org-internal.toml is installed
- QT (assumed): a linked descendant's thinker/code-reviewer spawns are work for the parent's stage; an unlinked spawn has no norm link, so its own subtree is outside D(P)
- QU (assumed): the child state's last write is the latest evidence the child was alive
- QV (assumed): same as 4b stage 6
- QW (assumed): no engine path deletes a session state file

## Consequences

- **Mechanical now:** the per-row attribution and the subtree partition of spend, the child-run intervals and their union with the prompt gaps, the typed link with its idempotence key, the derived escalation status, the orphan gate and the adoption verb.
- **Still perception:** whether an overrun means the parent's norm is wrong. The trigger names the overrun; the difficulty cycle that follows is the same one as for any other divergence.
- **Unlinked behaviour is unchanged.** Sessions without linked descendants, cost rows without `spawner_session`, unlinked spawns, dispatches that record no child run, and states written before the new fields load and measure exactly as before.

### Remaining gaps, accepted

- **A running child's own cost shows late (QO).** Its process row is written at exit, so mid-run spend is visible only through its finished descendants; the wall-clock scale still counts the open run.
- **A removed descendant's late rows drop out (QW).** Rows already booked stay counted through the watermark.
- **A reused process id keeps a dead run open** until that process exits.
- **The shared order window can still count a row twice or miss one** in the cases listed under Limits in the [agentctl README](../../scripts/agentctl/README.md#effort-divergence).
