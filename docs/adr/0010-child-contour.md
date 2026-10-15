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
- **Parent-side accounting is portion 4c, decided in [ADR-0011](0011-parent-accounting.md).** A child's spend and wall-clock roll into the parent stage's effort measure, and a child's norm escalation carries a typed link whose closure is derived on read.

## Amendment (child-session-reuse)

- Plan: `child-plan-review.toml`, stage R12

**Difficulty.** Each dispatch of a linked stage minted a fresh child session. Anything the root recorded on the previous child's engine session between two spawns never reached the next child: a plan approval, a review-pair override, order elements.

**Decision.**

1. **The record.** For each linked spawn, `spawn-specialist.py` writes `child-sessions/<parent session>-<stage index>.json` under the engine state root, beside `handoffs/`. The file holds `{"session", "workdir"}` for the child it is about to launch and replaces any earlier record for that stage. It is written before the launch and never in a dry run. `scripts/agentctl/norm_handoff.py` owns the record's shape (`child_session_path`, `write_child_session`, `read_child_session`).
2. **The reuse rule.** A linked re-dispatch of stage S of parent P resumes the recorded child session only when all five of these hold, checked in this order:
   - a record exists for (P, S);
   - this spawn's workdir equals the recorded workdir;
   - the child's engine session is not `RESOLVED` (the terminal-state test);
   - the child's engine session holds no `parent_link` yet, or its `parent_link.norm_digest` equals the `norm_digest` of this spawn's handoff record;
   - the transcript `<id>.jsonl` exists under that workdir's project directory in one of the projects roots.

   The workdir check comes before the transcript check because the harness keys transcripts by working directory: a different workdir has no transcript to resume. The digest check exists because the engine never replaces an adopted `parent_link`: a child resumed after a replan changed the parent stage's norm would keep the old norm in its engine while its brief carries the new one, so it starts clean instead. When all five hold, the spawn runs `claude -p --resume <id>` without `--session-id`, keeps `AGENTCTL_OWN_SESSION=<id>`, and exports the handoff the child's session already adopted. `_adopt_norm_handoff` keeps the first link and ignores a differing value, so the child gets the adopted value rather than a fresh handoff. If any condition fails (including an unreadable record or child state), the spawn mints a fresh uuid as before and replaces the record. Unlinked spawns neither read nor write the record.
3. **Why the record is not in the parent's stage state.** The base `Stage.from_dict` drops an unknown top-level key and raises on an unknown key inside a nested group such as `outcome`. The base engine also loads and saves the parent's state on every verb until the stage that ships the new engine. A key added there would be silently lost on the next save, or would make the parent's state fail to load. No engine code that loads and saves session state reads a separate file, and the parent's session state carries no new key.
4. **Two more own-channel verbs.** `OWN_CONTOUR_VERBS` gains `order-raise` and `order-dispose`. This lets a linked child satisfy the order-coverage gate on its own plan through `agentctl-own.py`. `question-raise` and `question-dispose` were already admitted. Both new verbs act only on the child's own session, and a foreign `--session` is still refused.

**Consequence.** A resumed child keeps its engine session. Approvals of its plan, review-pair overrides and the order elements recorded on it between the two spawns survive the re-dispatch. A child whose contour reached `RESOLVED`, a child whose adopted norm is no longer the stage's norm, or a re-dispatch in another workdir starts clean as before.

**Remaining gaps, accepted.**

- If the record write fails, the spawn prints a warning and still launches. That re-dispatch then mints a fresh session, which is the behaviour before this amendment.
- A child resumed before its engine adopted any handoff has no `parent_link` to compare, so the digest check admits it; it then adopts the handoff it is given at this spawn, which carries the current norm.
- Concurrent dispatch of the same stage is not guarded. Dispatch is assumed serial per stage: two spawns racing on one record could both resume the same child session, and the later record write wins.

## Amendment (resume-after-norm-fix)

- Plan: `pretrial-fixes.toml`, stage 4 (R4+R9; issue #423)
- Narrows: § Amendment (child-session-reuse), reuse rule item 4 and the **Consequence** paragraph.

**Problem.** A child that escalated the parent's norm (`critique --norm-target parent`, then `REPLAN`) stays at the difficulty it opened. The parent fixes the norm by a replan, which moves the stage's `norm_digest`. The child-session-reuse rule treated every changed digest as "start clean", so the very child that held the open difficulty, the escalation record and the explanation of what was wrong with the old norm was dropped at the moment the parent had answered it.

**The exception.** A changed digest no longer mints a fresh session when the child's open difficulty carries `escalated_to` naming this parent session and this stage at the digest the child's `parent_link` adopted: that child is **resumed after the parent fixes the norm** it escalated. Any other changed norm (no escalation, an escalation addressed to another stage or parent, an escalation of a different digest) still starts clean, as before. The other four reuse conditions are unchanged.

**What the resume renews.** Before the child runs, `spawn-specialist.py` (`refresh_resumed_child_link`) writes the fresh handoff record, replaces the child's `parent_link` and `recursion_depth` with it, clears the child's plan approval, and moves an approved-or-later node (`APPROVED`, `PARTITIONED`, `EXECUTING`, `VERIFYING`, `RESOLUTION`, or a `BLOCKED` parked from one) to `PLAN_READY`, so the child plans against the new norm and has it approved again. It exports the fresh handoff, not the previously adopted one. The wrapper edits the child's state only here, while no child process is running, and logs a `norm_handoff_refreshed` event. If the record or the refresh cannot be written the spawn proceeds unlinked, as for any failed handoff.

**Remaining gap, accepted.** A refresh moves the node but leaves the child's stage outcomes as they were; the child's own replan reconciles them.

## Amendment (child-plan-review)

- Plan: `child-plan-review.toml`, stage 2 (R1, R2, R4–R7)

**Difficulty.** A linked child must have its plan reviewed before `approve --by agent`, but the topological review driver (`plan-review-topological.py`) could not run on the child's contour: it needs `--session` against `agentctl-cli.py`, which the channel neither grants nor can (a prefix rule over `agentctl-cli.py` cannot fix `--session`), and the verbs it runs — `plan-review-walk`, `plan-review-pair-history`, `plan-review-compose` — were outside `OWN_CONTOUR_VERBS`. The child either skipped review or relied on the root to run it for it.

**Decision (D1).**

1. **Three channel verbs.** `OWN_CONTOUR_VERBS` gains `plan-review-walk`, `plan-review-pair-history` and `plan-review-compose` (`plan-review` and `plan-review-delta` were already in it). Each acts only on the child's own session; a foreign `--session` is still refused and `compose` only composes pair records already recorded.
2. **One seam-produced driver rule.** For a linked spawn only, `spawn-specialist.py` adds `own_contour_review_rule()` — exactly `Bash(python3 <scripts>/plan-review-topological.py --own:*)` — next to `own_contour_allow_rule()`, and the brief names `python3 <scripts>/plan-review-topological.py --own --plan <plan>`. An unlinked spawn, reviewers included, gets neither.
3. **Driver own mode, entered only by `--own`.** With `--own` the driver takes the session from `AGENTCTL_OWN_SESSION`, refuses an unset or empty variable, a `--session` that differs, and `--ledger` (exit 2, before any engine call), and sends every engine call — `plan-review-walk`, `plan-review-pair-history`, the `plan-review` records, `plan-review-compose` — through `agentctl-own.py` with no `--session`. Without `--own` the driver runs base mode as before, but exits 2 when `AGENTCTL_OWN_SESSION` is set, so a child cannot reach base mode. The flag, not the variable, selects the mode so that both directions fail closed: the grant's prefix includes `--own`, so the granted command cannot run base mode, and it cannot run at all without the contour's variable. Nothing rests on how the harness matches an env-prefixed command (`AGENTCTL_OWN_SESSION= python3 …` would otherwise select base mode under the same rule).
4. **Why the grant does not widen the child.** The rule names one script. The driver's reviewer spawn argv is fixed (kind `thinker`, `--review-topo`, no `--session`, no `--stage-index`), so every reviewer is an unlinked thinker — the spawner drops the own-contour env for an unlinked spawn — with the read-only topological view, at the child's depth + 1 under `max-recursion-depth`. The engine calls pass the channel, which refuses user-authority verbs, `approve` other than `--by agent`, and any foreign session.
5. **Where the driver writes.** The pair-history temp dir (`topo-history-*`), the materialized unit tree (`agentctl/topo-units` under the config root), the spawn cost log (`claude-spawn-costs.jsonl`) and the child's own session state through the channel — never another session's state. The README (§ The own contour of a linked child) lists the same locations.

**Premise answers.**

- Q1 (mechanism): a seam-produced exact driver rule, `own_contour_review_rule`, plus the three channel verbs, rather than per-plan declared grants.
- Q5 (`--ledger`): refused in own mode, because a ledger path is a write location outside the child's own session state.
- Q8 (names): `own_contour_review_rule` for the seam in `spawn-specialist.py`; `review_check_script_dirs` is the name of the reviewer's check-script-directory view, introduced by the next stage of the same plan (R3, R6, R7).

**Reviewer visibility of check scripts (R3, R6, R7).** `review_check_script_dirs(plan, plan_path)` in `spawn-specialist.py` yields, for every `--review-topo` spawn (unit and pair), the directories of the scripts a plan's stage `verify_command`, `negative_control` or `final_check` runs as one `bash`/`sh`/`python3` call on an absolute path to an existing file. Each is granted `--add-dir`, a `Read(//<dir>/**)` allow and an `Edit(//<dir>/**)` deny, and the brief lists the paths under `## Readable check scripts`. Filesystem roots, the temp root, `$HOME`, the config root and its direct children, the plans root, the plan file's directory, the topo-units root and their ancestors are never granted. The rule reads only the plan, so the root's driver and a linked child's driver give reviewers the same view.

**Remaining gap (Q2).** This is a known limitation: the thinker review of a linked child's plan still rests on the child's word. A `plan-review --reviewer thinker --verdict pass` the child records by hand through the channel is admitted exactly as one the driver recorded, so a hand-recorded pass is indistinguishable from a driver-produced one. This amendment does not close it — the user kept it out of scope (Q2). The driver makes the honest path available on the child's own contour; it does not make the dishonest one impossible. A Core follow-up issue is filed by the root after landing.

## Amendment (kill switch)

*Difficulty removed:* the gaps above make a linked child stumble when parallel sessions share the machine, and backing the contour out would cost a revert of every portion.

Linking ships **off by default**. The `child-contour-linking` row of `config.md` (`off` | `on`) decides it, and the `AGENTCTL_CHILD_LINKING` env var overrides the row in both directions (`1` forces on, `0` forces off). Only `agentctl.config.child_contour_linking_enabled` resolves the two; a missing row, any other value or an unreadable `config.md` resolves to off, and the resolver never raises.

`spawn-specialist.py` asks the resolver at **one decision point**, the branch that would write the norm handoff for a `--session` + `--stage-index` spawn; it never reads the env var itself. When the switch is off, the spawner skips `norm_handoff_record`, prints one stderr line naming the key and the variable, and takes the pre-linking unlinked path, the same one a handoff record that cannot be written takes: no handoff, no own session id, no `agentctl-own.py` allow rule, and the brief's "The engine belongs to the parent" section. Everything this ADR decides is unchanged for a spawn the switch links.

To turn linking on, change the value of the `child-contour-linking` row in `config.md` from `off` to `on` (one line), or export `AGENTCTL_CHILD_LINKING=1` for one spawn. The test suite pins the variable to `1` in `scripts/tests/conftest.py`, so the linked path stays covered while the default is off.

## Amendment (child-contour-gaps)

- Plan: `child-contour-gaps.toml`, stages 1–3 (R1–R6)

**Difficulty.** Running portion 4c as a linked child showed two gaps that made the root do the child's work. First, the child had no path it could write its own plan to: the harness denied the write, the state gate denied it while the child was planning, and a declared rule under the agent home is refused by `validate_rule`'s protected-target check, so the root wrote the child's plan and every later edit. Second, a child plan with a measurable stage could not self-approve. Its stage checks are engine-executed texts, `parent_norm_boundary` held such a text only when a handoff rule matched it byte for byte, and parent stages grant wildcard rules (`Bash(P:*)`); a pytest check also resolves to a write in its run directory, which the handoff does not hold.

**Decision.**

1. **The child's own plan path (`own-plans`).** `config_root.own_plans_dir()` is `<agent-home>/own-plans`; `own_contour.own_plan_path(sid)` is `own_plans_dir()/<sid>/plan.toml`. For a linked spawn only — the `child_linked` side of the single kill-switch decision point (`child-contour-linking` row / `AGENTCTL_CHILD_LINKING`), so an unlinked or switched-off spawn gets none of it — `spawn-specialist.py` creates the `<sid>` directory (mode 0700) and adds `--add-dir` for it and one allow rule, `Edit(//<abs plan.toml>)`. The rule is produced at the seam like `own_contour_allow_rule`, not through `validate_rule`, so a declared rule under the agent home is still refused. The spawn is refused when an emitted deny would void the allow. The brief names the path and `agentctl-own.py submit-plan --plan <path>`.
2. **Ownership is decided from the path.** `exempt_paths.is_own_plan_file` accepts only the canonical path (absolute, no `..`, realpath equal to `own_plans_dir()/<sid>/plan.toml`), so the hook's plan-write position governs it: writable while the session plans, frozen once it is the tracked plan at `EXECUTING`. `exempt_paths.own_plan_owner` resolves the realpath and names the owner `<sid>`; `hook-state-gate.py` refuses `foreign-own-plan` for any writer other than `<sid>` — the root, a linked sibling, an unlinked child, with or without engine state — before every early return, so a `..` path or a symlink to a child's plan is refused for every writer, its owner included. This cannot reach another session's plan or the parent's: the grant is one exact file, the hook keys the file to the session named in the path, and the parent plan stays under `plans/`, covered by the developer kind's deny `Edit(//plans/**)` and by `foreign_plan_owner`.
3. **Wildcard cover for a child's checks.** `grants.bash_command_covered_by_rules(rules, command)` is the single matcher and `_bash_call_covered` delegates to it. A segment is covered by `Bash(P:*)` when it equals `P` or starts with `P` and a space; an exact `Bash(C)` covers only `C`; a command is covered when it splits into top-level segments and every one is covered. In `parent_norm_boundary` an unresolved `verify_command`, `negative_control`, `final_check` or declared exact Bash rule is held when the wildcard-tail rules the handoff holds cover it this way. `compute_boundary_view` tags each resource with the check texts it came from, and a resource produced only by covered texts (the pytest run-directory write) is not charged, except under a protected root. A resource that another origin also produces (a declared rule, an uncovered check, an output artifact) stays charged.
4. **Still refused.** A check outside the parent's grants, a compound check with any uncovered segment, a segment that does not split, a prefix without a word boundary, and a protected-root write all go up to the user as before.
5. **Which pinned test changes.** `test_child_approval.py::test_unresolved_stage_verify_command_is_covered_only_by_the_identical_handoff_rule` changes only its command: its handoff holds `Bash(python3 mod.py:*)`, which now covers the old command `python3 mod.py --check`. The assertions and the meaning are unchanged, and the commit carries a `Changed-test:` trailer. The refusal test beside it is unchanged, because its parent holds no covering wildcard.

**Consequence.** A linked child can go from its brief to `EXECUTING` with no write by another session; `test_child_contour_e2e.py` drives that chain, and `test_own_plan_path.py` and `test_child_check_cover.py` pin each half. Unlinked spawns, sessions without `parent_link`, plans without `parent_norm` and `validate_rule` behave as before. Turning linking on stays a separate decision (GI).

**Premise answers.** Recorded in the premise gate of session a8a7646d-dea5-4def-9560-c056c7f80ba2; for an assumed premise the right-hand side is its basis, not a decision of the user.

- GA (escalated): user: own_plans_dir()/<sid>/plan.toml, granted at spawn, frozen at EXECUTING, foreign writes refused (re-confirmed by user 2026-10-14 against the rebased plan)
- GB (escalated): user: option A — accept a child check when held parent wildcards cover every top-level part, via the shared matcher (re-confirmed by user 2026-10-14 against the rebased plan)
- GC (escalated): user: yes — accept a covered pytest check's write to its run directory, protected locations excluded (re-confirmed by user 2026-10-14 against the rebased plan)
- GD (assumed): base code 8e3cd82c has neither fix (kill switch landed, linking off by default; conftest forces AGENTCTL_CHILD_LINKING=1), so this plan's own children would hit both gaps; exact-text grants are the cover test_child_approval.py already accepts, and the root writes a child plan from its draft
- GE (assumed): the two fixes touch the same files (spawn-specialist.py, gates.py, grants.py) and the e2e test needs both; partition markers M1-M4 do not fire
- GF (assumed): names follow agentctl naming (own_contour, is_*_file, kebab-case refusal reasons): own_plans_dir, own_plan_path, own_plan_owner (broad, realpath), is_own_plan_file (narrow, canonical), foreign-own-plan, bash_command_covered_by_rules
- GG (escalated): user: yes — the brief's own-contour section names the child's plan path (re-confirmed by user 2026-10-14 against the rebased plan)
- GH (assumed): same practice as 4b stage 6: merge fixes amended into the merge commit so the suite report names the final head
- GI (escalated): user (2026-10-14): leave child-contour-linking off in this task; decide turning it on separately after landing
