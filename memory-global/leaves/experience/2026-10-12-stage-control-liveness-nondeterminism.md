---
name: 2026-10-12-stage-control-liveness-nondeterminism
description: Network and live-API reads and a local exit-1 that does not reproduce, scored as a missing artifact or a failed stage, plus a loop guard that cannot tell a repeat from a new failure.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor (AskUserQuestion: 'Да — решено')"
created: 2026-10-12
last_verified: 2026-10-12
generalized_into: [a-non-hermetic-read-fails-independently-of-the-artifact, a-two-valued-judge-absorbs-could-not-look]
---

# A stage control's verdict rests on a live or non-reproducing read, so a transient failure is scored as a defect

## Difficulty
A stage check reads a live external resource, or a local suite that sometimes exits 1 without a reproducible cause. A single transient failure is recorded as a failed stage and routed into diagnosis, where manual re-runs come back clean and no factual cause can be named. The loop guard meanwhile treats the repeat as a new digest or as noise, so the system can neither retry cheaply nor stop honestly.

**Functional ground:** A control consumes an input that is not hermetic (a network read, a live API, a flaky local run) and has no way to separate a transient failure from a real one, so one non-reproducing exit code costs a full difficulty cycle.

## Order & criterion
Split out of [2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan](2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan.md) by memory-curator split on 2026-10-12: the contexts below arose under that leaf's difficulty and cluster here by the functional ground named above. The order and acceptance check of the original stay in the source leaf.

## Contexts

### 2026-08-19 — 2026-08-19 residue-capture-to-backlog: FLAKY - a network read whose failure is scored as an absent artifact
- Where it arose: ~/.claude-agent/plans/residue-capture-to-backlog-v13..v16.toml (6 stages, 7 final checks; the capture of this machine's uncommitted residue into a public gist plus one org-internal tracker ticket). final_check[6] certifies that the two halves partition the residue; it and stage[5].verify_command both read the internal half over the network.
- Working plan: residue-capture-to-backlog v13->v16: rewrite BOTH final_check[6].command and stage[5].verify_command so every remote read resolves the artifact by its PINNED KEY rather than by a search, captures the reading command's own exit status instead of piping it into jq, runs under `timeout 60` with up to five attempts, and exits through a distinct UNREADABLE outcome that explicitly asserts NOTHING about the artifact; interpret an exit-0-empty read PER CALL SITE (empty gist index = the public half is absent, a finding; empty read of a pinned key = unreadable, not a finding); state the residual in the label. Proven by an 18-case world-mutation battery, both clean runs green. Closed by a user-authored plan-review override at round 7 rather than a seventh spawn.

### 2026-09-23 — TRANSIENT-EXTERNAL — single non-reproducing exit-1 from a live-API final_check, confirmed via independent manual re-run
- Where it arose: an org-internal ticket-driven refresh-and-merge task (session cc963b8c-...), verify-final final_check 2: three sequential external subprocess calls (an internal code-review CLI's PR active-diff lookup, that same CLI's checks-by-diff-id lookup, and a tracker-CLI comments read) run fresh on EVERY verify-final invocation against live code-review + tracker state.
- Working plan: an org-local plan file (path/ticket identifiers elided — org-internal, not committed to this public leaf)

### 2026-10-02 — acceptance-judge-sonnet stage 1 — non-reproducing exit-1 on a LOCAL differential-verify suite block, plus a loop-guard digest-invariance gap
- Where it arose: agentctl session 05163133-e4d8-4c3b-97f8-b4939c99d63c, task acceptance-judge-sonnet, stage 1 record-result (delivery worktree /home/the0/claude-agent-instructions-acceptance-judge-sonnet, HEAD 4a9ff6314fd075e4f437e1f5a88084f82992365d)
- Working plan: /home/the0/.claude-agent/plans/acceptance-judge-sonnet-v4.toml
- What this context adds: NOT a new vector — a same-machine variant of the 2026-09-23 TRANSIENT-EXTERNAL entry, this time on a purely LOCAL differential-verify `verify_command` (the whole-suite `pytest scripts/tests/ -q -rfE` block with a pinned 2-test baseline-exception allowlist, no network/external-API call anywhere in the chain). The engine's one internal re-check at `record-result` returned exit 1; five independent reproductions of the exact same ~30-clause `&&`-chained command against the same unchanged delivery-worktree HEAD (the developer's own self-report, two manual reruns of a saved `verify.sh` evidence script, a full clause-by-clause reproduction of all 27 grep/test assertions plus the two git-log checks, and a live full run of the trailing whole-suite pytest block — `RC=1, OBS={the 2 pinned baseline-exception test IDs}, NEW=empty, 2 failed/7244 passed/4 skipped in 1015.37s`) all came back clean, matching the documented PASS condition every time. `stage.outcome.actual` on a `verify_command` failure is always the STATIC text `f"verify_command exit {rc} != expected {exp}: {verify_command}"` — the full command, not its stdout — so for an `&&`-chained compound command spanning dozens of clauses there is no way to tell from state alone which clause (if any specific one) actually tripped; the only way to localize was full manual reproduction of every clause. Closed as `failure_address: ресурсное` (an unstable verification RESOURCE — one unrepeated ~17-minute subprocess run under unknown ambient load — not a wrong check/code/norm), the same address as the 2026-09-23 entry. Secondary, independent finding surfaced by the same investigation: the saved evidence-dir `verify.sh` for this stage only ever ran the narrow `test_stage_review_scope.py` pytest clause (one of ~30), so it was never representative of the real gate — a reviewer trusting the evidence file alone would have under-verified the stage even on a clean run; not the cause of this difficulty, but a planning/evidence-capture gap worth naming separately. **Structural gap flagged, not yet fixed:** the loop-guard at `record-result` (`cli.py` ~line 7353, `dig = _digest(actual); repeat = dig in stage.outcome.fail_digests`) digests the SAME static command-text note described above — which is invariant across any two FAILED executions of one `verify_command` regardless of which underlying clause or cause differed each time — so a genuinely unrelated SECOND transient failure through this same check would compute an identical digest to the first and incorrectly trigger "same digest twice → escalate" instead of routing back to DIAGNOSING for its own (different) root cause. The loop-guard needs to digest something that varies with the actual failure (captured stdout/stderr, or at minimum which clause index failed), not the command's own fixed text.

## Cost
Not costed separately: the contexts keep the cost recorded in [2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan](2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan.md).
