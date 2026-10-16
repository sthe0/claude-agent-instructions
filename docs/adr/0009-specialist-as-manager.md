# ADR-0009: Specialist as manager — the parent stage's norm as the child's order

- Status: Accepted 2026-10-11
- Plan: `specialist-manager.toml`

## Context

A spawned specialist is not a different kind of actor. It is the manager one level down: specialization = manager ⊕ role SKILL.md, and the manager itself is the empty specialization (`spawn-specialist.py --kind manager`). A role completes the manager; it never replaces its contract. Four earlier portions of the recursion repair made this the norm in prose and in two objects: the depth n+1 manager and its owner-node check in the state gate, `StageNorm` with its index-free `norm_digest` ([ADR-0007](0007-stage-norm.md)), questions and acceptance keyed by that digest, and the parent-difficulty env seam (`AGENTCTL_PARENT_DIFFICULTY`).

What was missing is the handover itself. A child's engine session did not know which stage of which parent it served: `recursion_depth` was written by `agentctl start --recursion-depth` (which the autostart hook never passes) and read by nobody, `loop-sensitivity-depth` was read by nobody, the loop detector existed only as a prompt sentence, and a child that delegated further passed its own plan's grants to the grandchild with no check against what it had been given itself. "The child plans, overcomes and delegates within the parent's norm" could not be enforced because the engine did not hold the norm, the depth or the lineage.

## Decision

1. **One digest-bound handoff record.** When `spawn-specialist.py` is given `--session` and `--stage-index` and the stage resolves in the session's plan, it writes `norm-handoff/v1` under `<state root>/handoffs/` and exports `AGENTCTL_NORM_HANDOFF=<path>:<sha256 of the file>`. The record carries the parent stage's `StageNorm` and its `norm_digest`, the stage's effective engine grants, the child's depth and `norm_lineage` — the chain of ancestor norms, outermost first, ending with the parent stage. Any other spawn exports nothing and removes an inherited value, so a link never leaks to an unrelated child.
2. **The child adopts the record as its order.** At `start`, `reset` and `classify` the child's engine verifies the hash and the recomputed `norm_digest`, then stores `SessionState.parent_link` and sets `recursion_depth` from it. A missing, tampered or malformed record is logged and ignored: the session stays unlinked and behaves as before. Adoption never raises.
3. **The recorded depth is read.** The spawn depth cap counts `max(env AGENT_RECURSION_DEPTH, the --session state's recursion_depth)`, so losing the env variable cannot reset the depth.
4. **Rule and perception are split at the spawn seam.** Delegating a norm unchanged is a digest equality, so the wrapper decides it: a spawn from a linked session for a stage whose `norm_digest` equals any `norm_lineage` entry is refused before launch (**norm-loop**, exit 3, first stdout line `LOOP_DETECTED:`), and `dispatch` routes it like `ESCALATE`. Re-framing an ancestor's order under new wording is a judgement of meaning and is never decided lexically: at `recursion_depth` ≥ `loop-sensitivity-depth` the child's `classify` directive carries `loop_self_check` with the ancestors' titles and done criteria, and the child answers with the `LOOP_DETECTED` return marker, which `dispatch` routes like `ESCALATE`.
5. **A child cannot delegate wider grants than its own** (**grant-widening**). Every engine grant the grandchild would receive must be covered by the session's handoff grants or by a runtime grant a **user** resolved on that session; an agent self-grant never counts, an unconsumed `once` grant does. Coverage is an identical entry or resource coverage through the existing `plan_resources` / `Resource.covers` primitive; a wildcard-tail Bash rule resolves to no bounded resource and is covered only by an identical entry. A refusal exits 4 with a `PERMISSION-REQUEST:` block carrying `Refusal: grant-widening` and one `Rule:` per uncovered entry. `dispatch` parks it as an ask to the user, never as a materialization defect, a self-grant or a difficulty. Order of refusals: recursion cap, then norm-loop, then grant-widening.
6. **A parent can see its children.** `agentctl children --session P` lists sessions whose `parent_link` names P. It scans the store under one state root, so the spawn wrapper never writes the parent's state and no file has a second writer.
7. **What the premise answers fix.** The loop rule refuses only an exact digest match (Q3). Spawns without `--session`/`--stage-index`, sessions without a handoff and the state gate's depth ≥ 1 bypass behave as before (Q4). Grant narrowing covers engine stage grants, not the per-kind baseline table (Q5). The names are the ones above (Q6). Children are found by scanning one state root (Q8). The work ships as portion 4a; 4b and 4c are separate orders (Q9).

## Premises

Premise questions of the order, with the disposition and the answer (or, for an assumed premise, the basis) recorded in the premise gate. For an assumed premise the right-hand side is its basis, not a decision of the user.

- Q1 (escalated): Yes: the parent stage's approval covers a child plan inside the handed-down norm; a child plan goes up only on a norm mismatch or when the parent stage is acceptance-review. Mechanized in portion 4b.
- Q2 (escalated): Yes: an in-norm child plan keeps its own thinker plan review before self-approval.
- Q3 (assumed): An exact digest match is decidable; semantic re-framing is perception and belongs to the model per the rule/perception split in CLAUDE.md. Re-confirmed against the current stage 2 method (a)/(b).
- Q4 (assumed): Order requirement R6: unlinked sessions keep base behaviour so 4a cannot regress current spawns; gating linked children is 4b. Re-confirmed against the current stage 1 invariants (test-integrity clause added, R6 unchanged).
- Q5 (assumed): kind_baselines are role defaults set by the spawn wrapper, not engine grants; narrowing them needs the child engine loop of 4b. Re-confirmed against the current stage 2 method (d)/(d').
- Q6 (assumed): Names follow existing agentctl naming (AGENTCTL_* env, snake_case state fields, kebab refusal codes).
- Q7 (assumed): Same suite-green definition was used and accepted in portions 3 and 4 (suite-compare.sh, NEW_FAILURES=0).
- Q8 (assumed): All sessions of one machine share one state root by default; spawn-specialist passes the parent's --state-root.
- Q9 (escalated): Yes: portion 4a now (handoff, parent link, loop rule, grant narrowing, ADR); 4b (child engine loop, self-approval) and 4c (child effort in the parent's budget, cross-session difficulty closing) later.

## Consequences

- **Mechanical now:** the handoff and its verification, the adopted depth and its use in the cap, the exact-digest loop refusal, the trigger and payload of the loop self-check, grant narrowing at delegation, the parent's child listing.
- **Still perception:** whether a task re-frames an ancestor's order. A reworded loop passes the engine and relies on the child's self-check, exactly as before this change (Q3).
- **Known gaps, accepted:** a read-only child may spawn a `developer` that receives the developer baseline (Q5); a child started under a different state root is not listed by `children` (Q8); the state gate still treats every depth ≥ 1 session as an executor, so a child's own plan is not yet gated by its own contour (Q4); closed for linked children by [ADR-0010](0010-child-contour.md).
- **Fail-open on unreadable state.** A spawn whose session state or plan cannot be read skips the norm-loop and grant-widening checks with a stderr warning rather than refusing, so a transient read error does not park every linked stage.
- **Test isolation.** Every spawned session is linked from now on, so the test suite scrubs `AGENTCTL_NORM_HANDOFF` for each test; a test that needs it sets it explicitly.

## Not decided here

- **Portion 4b — the child's own contour inside the norm** (decided in [ADR-0010](0010-child-contour.md)). A child plan that declares the parent norm's digest becomes self-approvable when its final checks contain the parent stage's verify command, its resources are covered by the handoff grants and the depth is within the cap; a difficulty addressed to the parent's norm itself returns `REPLAN` and opens a difficulty on the parent's stage, everything else the child overcomes itself; a linked child is gated by its own contour instead of the env-only executor bypass. Q1 and Q2 record the direction (in-norm child plans are covered by the parent stage's approval and keep their own thinker review); the mechanism changes the approval gate and is its own reviewed change.
- **Portion 4c — parent-side accounting.** A child's spend and wall-clock rolled into the parent stage's effort-divergence measure, and cross-session difficulty closure. Decided in [ADR-0011](0011-parent-accounting.md).
