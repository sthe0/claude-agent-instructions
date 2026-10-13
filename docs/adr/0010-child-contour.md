# ADR-0010: The child's own contour — approval, escalation and state gate inside the parent's norm

- Status: Accepted 2026-10-12
- Plan: `child-contour.toml`

## Context

[ADR-0009](0009-specialist-as-manager.md) (portion 4a) hands a spawned child its parent stage's norm as a verifiable record: `SessionState.parent_link` holds the norm, its `norm_digest`, the stage's grants, the depth and the `norm_lineage`. The child still could not act as a manager inside that norm, for four reasons:

- **Approval.** Every child plan had to go up for the user's approval: `gates.autonomy_boundary` knew no basis tied to a parent norm, although the parent stage's approval already covers a plan inside that norm (ADR-0009 Q1).
- **Norm mismatch.** A child difficulty that is really the parent's norm being wrong had no engine route back. The child either overcame it locally, re-norming a norm it does not own, or returned `REPLAN` as prose the parent could not verify.
- **State gate.** `hook-state-gate.py` let any session at depth ≥ 1 edit production files, so a child was gated by nothing but its parent's stage.
- **Channel.** The child's brief gave it only `agentctl classify` and `status` on its own session, so it could not walk its own contour even if one were required.

## Decision

1. **A parent-norm approval basis.** A child plan that declares `[meta.order] parent_norm = <norm_digest>` is judged by `gates.parent_norm_boundary`, not by the order-approvals ledger, and self-approves with `approve --by agent` on basis `parent_norm` exactly when all of these hold. Each failure is a named reason code, and all failing codes are reported:
   - `parent-norm-digest-mismatch` — the declared digest is not the handoff's `norm_digest`;
   - `parent-acceptance-review` — the parent stage is decided by the user's acceptance, so there is no check the child could carry;
   - `parent-check-dropped` — no final check carries the parent stage's `verify_command` byte-for-byte after stripping surrounding whitespace, with the parent's `expected_exit` (a measurable parent stage with no `verify_command` fails here too);
   - `resource-not-in-handoff` — a resource of the child plan is not covered by a handoff grant entry (`specialist(<role>)` is exempt, because grant narrowing already gates every delegation at the spawn seam);
   - `unresolved-not-in-handoff` — an engine-executed command or wildcard rule that cannot be resolved to a resource is not carried by the handoff either, except the parent's own check;
   - `depth-over-cap` — the handoff's depth is above `max-recursion-depth`;
   - `not-linked` — the session has no handoff at all.

   The plan-review gate is unchanged: an in-norm child plan keeps its own thinker review (Q2). A plan that fails any code goes up to the user as before. `parent_norm` enters the order digest only when it is set, so existing plans keep their digests and review bindings.

2. **A norm mismatch is escalated from the child's engine record.** `critique --failure-address нормативное --norm-target parent` is accepted only in a linked session; it records `{target: "parent", kind: "parent_norm", baseline: <the handoff's norm_digest>}` and directs the child to return `REPLAN` naming the mismatch. On the parent's side, `dispatch` takes the child session id from the stem of the transcript path the spawn reported, loads that state read-only, and accepts the escalation only if the child's `parent_link` names this session and stage, its open difficulty carries a parent target whose baseline equals the stage's **current** `norm_digest`, and its declaration is complete. Then the stage is marked `FAILED` and the session opens `DIAGNOSING` with the child's declaration and `data['norm_mismatch']`. Any other `REPLAN` keeps the directive it always had, and the marker text never decides. Every difficulty not addressed to the parent's norm the child overcomes inside its own contour.

3. **A linked child is gated by its own contour and drives it through a fixed-session channel.** `hook-state-gate.py` no longer applies the depth ≥ 1 bypass to a session with a `parent_link`: production edits are released only at `EXECUTING`, `VERIFYING` and `RESOLUTION`, and `CHAT` is denied. An unlinked depth ≥ 1 session keeps the bypass. A session whose env carries `AGENTCTL_OWN_SESSION` or the norm handoff but holds no `parent_link` (or whose state cannot be read), or whose `AGENTCTL_OWN_SESSION` names another session, is denied with a named message, so a link that fails to load never silently restores the bypass. The child moves its own session only through `scripts/agentctl-own.py`: it reads `AGENTCTL_OWN_SESSION`, places `--session` itself, parses with exact option spelling (`allow_abbrev=False`) and refuses an explicit `--session`, a missing env value, an unlinked session and any verb outside `OWN_CONTOUR_VERBS` — `classify`, `status`, `plan`, `next-stage`, `submit-plan`, `plan-review`, `plan-review-delta`, `approve` (`--by agent` only), `partition` (marker-free only), `dispatch`, `record-result`, `code-review`, `verify-final`, `declare`, `investigate`, `critique`, `normalize`, `replan`, `stage-grants`, `plan-grants`, `question-raise`, `question-dispose`. The set and the env name live once, in `scripts/agentctl/own_contour.py`. A wrapper rather than a prefix allow rule, because `argparse` keeps the last `--session`, so a rule over `agentctl-cli.py` would let a trailing `--session` retarget any session. For a linked spawn only, `spawn-specialist.py` pre-assigns `claude -p --session-id <uuid>`, exports `AGENTCTL_OWN_SESSION=<uuid>`, grants the wrapper by exact path and replaces the brief's "The engine belongs to the parent" section with "Your own engine contour".

4. **The terminal state is the child's own `verify-final`.** `resolve`, `resolve-permission`, `reset`, `present-plan`, `mandate-*`, `drive`, `accept`, `risk-accept` and the other verbs that act on the order, a user decision or the mandate stay with the root. A linked child ends its contour with its own `verify-final` and returns `COMPLETED`; its session stays at `RESOLUTION`, because `resolve` is the root's, and the parent never writes the child's state.

5. **What the premise answers fix.** One delivery and one landing, with no partition (QH). A new ADR rather than an amendment of ADR-0009 (QC). The names are the ones above (QG).

## Premises

Premise questions of the order, with the disposition and the answer (or, for an assumed premise, the basis) recorded in the premise gate of session 09321034-d4e2-43ae-8bae-bdde96759e00. For an assumed premise the right-hand side is its basis, not a decision of the user.

- QA (escalated): User (2026-10-12, AskUserQuestion): yes — open the child's own contour verbs to linked children only through the fixed-session wrapper agentctl-own.py; order-level user-authority verbs stay with the root.
- QH (assumed): (iii) without (i)/(ii) would brick children; the three code stages share fixtures and state
- QB (assumed): 4a grant narrowing already gates delegation at the spawn seam
- QC (assumed): ADR-0009 names 4b under Not decided here; a separate record keeps 0009 stable
- QD (assumed): existing plans must keep their digests and review bindings (back-compat R5)
- QE (assumed): a linked child must walk its own contour; CHAT carries no plan to gate against (unchanged by adding plan and marker-free partition to the channel)
- QF (assumed): the child's engine session id equals its transcript stem under the shared state root
- QG (assumed): names follow existing gates.py reason-code style
- QI (assumed): the autostart hook keys state on the harness session id (unchanged by the verb-set fix)
- QJ (assumed): plan-review is still in OWN_CONTOUR_VERBS; keeps the user's Q2 answer
- QK (assumed): accepted gap limited to linked sessions; recorded in ADR-0010; the two added verbs act only on the child's own session and partition markers stay refused
- QL (researched): yes: org-internal ruleset (deny=20)

## Consequences

- **Mechanical now:** the approval basis and its reason codes, the escalation route from the child's state record, the state gate for a linked child with its fail-closed signals, the fixed-session channel with its verb set, the spawn wiring of the session id, the grant and the brief.
- **Still perception:** whether a fault is the parent's norm rather than the child's own plan is the child's judgement in `critique`; whether the child's plan is *acceptable* is the thinker's plan review, which the engine requires but does not perform.
- **Unlinked behaviour is unchanged.** Sessions without `parent_link`, spawns without a handoff, plans without `parent_norm` and `REPLAN` returns without a verified child record behave as before. The one deliberate difference is that an unlinked spawn removes an inherited `AGENTCTL_OWN_SESSION`, like an inherited handoff.

### Remaining gaps, accepted

- **The session id is assumed, not observed (QI).** That the harness reports the `--session-id` passed to `claude -p` as the hook payload's session id is not checked at runtime; the test shows only that the spawn exports that id. If it differs, the channel refuses every call (unlinked session) and the child is gated with no way to move its node.
- **The thinker review rests on the child's word (QJ).** A linked child records `plan-review --reviewer thinker --verdict pass` through the channel; nothing ties it to a thinker spawn having run, so Q2's review gate holds only on paper for a linked child.
- **The channel trusts its env (QK).** A child that writes an env prefix (`AGENTCTL_OWN_SESSION=<other> python3 .../agentctl-own.py ...`) may act on another session if the harness matches that command to the allow rule. The wrapper still refuses an unlinked target, so only another linked session is reachable and only through `OWN_CONTOUR_VERBS`. Closing it needs a caller identity the harness does not expose to a Bash subprocess.
- **A handoff record can be orphaned.** A spawn refusal after the record is written (prompt-size ceiling, missing `claude`) leaves it unused, as on base.
- **Parent-side accounting is portion 4c.** A child's spend and wall-clock rolled into the parent stage's effort measure, and cross-session difficulty closure, have no plan yet.

## Amendment (child-session-reuse)

- Plan: `child-plan-review.toml`, stage R12

**Difficulty.** Each dispatch of a linked stage minted a fresh child session. Anything the root recorded on the previous child's engine session between two spawns never reached the next child: a plan approval, a review-pair override, order elements.

**Decision.**

1. **The record.** For each linked spawn, `spawn-specialist.py` writes `child-sessions/<parent session>-<stage index>.json` under the engine state root, beside `handoffs/`. The file holds `{"session", "workdir"}` for the child it is about to launch and replaces any earlier record for that stage. It is written before the launch and never in a dry run. `scripts/agentctl/norm_handoff.py` owns the record's shape (`child_session_path`, `write_child_session`, `read_child_session`).
2. **The reuse rule.** A linked re-dispatch of stage S of parent P resumes the recorded child session only when all four of these hold:
   - a record exists for (P, S);
   - the child's engine session is not `RESOLVED` (the terminal-state test);
   - this spawn's workdir equals the recorded workdir;
   - the transcript `<id>.jsonl` exists under that workdir's project directory in one of the projects roots.

   The workdir check comes before the transcript check because the harness keys transcripts by working directory: a different workdir has no transcript to resume. When all four hold, the spawn runs `claude -p --resume <id>` without `--session-id`, keeps `AGENTCTL_OWN_SESSION=<id>`, and exports the handoff the child's session already adopted. `_adopt_norm_handoff` keeps the first link and ignores a differing value, so the child gets the adopted value rather than a fresh handoff. If any condition fails (including an unreadable record or child state), the spawn mints a fresh uuid as before and replaces the record. Unlinked spawns neither read nor write the record.
3. **Why the record is not in the parent's stage state.** The base `Stage.from_dict` drops an unknown top-level key and raises on an unknown key inside a nested group such as `outcome`. The base engine also loads and saves the parent's state on every verb until the stage that ships the new engine. A key added there would be silently lost on the next save, or would make the parent's state fail to load. No engine code that loads and saves session state reads a separate file, and the parent's session state carries no new key.
4. **Two more own-channel verbs.** `OWN_CONTOUR_VERBS` gains `order-raise` and `order-dispose`. This lets a linked child satisfy the order-coverage gate on its own plan through `agentctl-own.py`. `question-raise` and `question-dispose` were already admitted. Both new verbs act only on the child's own session, and a foreign `--session` is still refused.

**Consequence.** A resumed child keeps its engine session. Approvals of its plan, review-pair overrides and the order elements recorded on it between the two spawns survive the re-dispatch. A child whose contour reached `RESOLVED`, or a re-dispatch in another workdir, starts clean as before.

**Remaining gaps, accepted.**

- If the record write fails, the spawn prints a warning and still launches. That re-dispatch then mints a fresh session, which is the behaviour before this amendment.
- A resumed child keeps the `parent_link` it adopted first. If a replan changed the parent stage's norm between the two spawns, the brief carries the new norm while the child's engine still holds the old `norm_digest`. The reuse rule does not compare digests.
