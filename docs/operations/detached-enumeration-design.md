# Detached question-enumeration — design settled against the code

Stage 3 of the `advisor-timeout-f3b` plan. The order was "the enumeration must leave the
blocking path"; that names a direction, not a design. This note settles the three questions
whose answers change what stage 4 builds, each against the code rather than against
preference. Every claim carries a `file:line` citation into the delivery worktree at commit
`615a9c5`; a question the code cannot settle is marked **OPEN** with its consequence for
stage 4 named.

Line numbers are re-derived against this worktree, not copied from the plan.

## Q1 — where the launch hooks in

### (a) `cmd_submit_plan` is a launch site, and it is not the only one

`state.plan_path` is set at `cli.py:1538`, inside `cmd_submit_plan`, and
`plugins_premise.py:150-153` computes the content digest by loading exactly that path. So the
first moment a digest exists is `cmd_submit_plan`, and the gate arms one line later
(`cli.py:1551`). That makes submit-plan the obvious launch site.

It is not sufficient. See (c).

### (b) `cmd_replan` — ordering, which blocker fires, and whether to launch at all

**Ordering.** `cmd_replan` evaluates the `plan_approval` plugin gate as a precondition of
replan *itself*, at `cli.py:3261`, inside a try/finally that temporarily swaps
`state.plan_path` to `args.plan` (`cli.py:3258-3263`). That evaluation sits **before**
`new = _load(args.plan)` at `cli.py:3282`. A fire-and-forget child launched by the same call
cannot land before line 3261 returns, so:

- the clear-and-stamp and the launch must sit **inside** the swapped-`plan_path` block, before
  the `cli.py:3261` evaluation;
- the first replan attempt therefore **refuses** — "enumeration launched, **not landed**,
  N s remaining" — and succeeds on retry once the child has written its sidecar.

The digest is computable there without hoisting anything: `_plan_content_digest`
(`plugins_premise.py:60-73`) takes a `PlanDoc`, and `plugins_premise.py:151` already obtains
one via `plan.load_plan(plan_path)` from the swapped path. Stage 4 does the same, in the same
block. Hoisting the strict `_load` from `cli.py:3282` would instead move malformed-plan errors
ahead of the plan-review gate at `cli.py:3242` — an ordering change with no benefit. Do not.

**Which blocker fires.** `plugins_premise.py:170-173` is an `if`/`elif` over mutually exclusive
branches:

```python
if not bag.get("enumerated"):
    blockers.append(_ENUMERATE_NOT_RUN)
elif content_digest is not None and bag.get("enumerated_at") != content_digest:
    blockers.append(_ENUMERATE_STALE)
```

On a **first** submission `enumerated` is unset, so `_ENUMERATE_NOT_RUN` fires — the branch
stage 5's escape is built for. On a **replan against a plan that already carries a prior
enumeration**, `enumerated` is already `True` (`cli.py:1402`), so a changed digest trips the
`elif` and raises `_ENUMERATE_STALE`, which has **no escape**. Hence stage 4's clear-and-stamp:
clearing `enumerated`/`enumerated_at` atomically with stamping `enumerate_deadline` routes the
outstanding-child window onto the escapable branch.

**Whether the launch should fire at all.** It must be gated on the digest comparison.
`_plan_content_digest` (`plugins_premise.py:66-72`) hashes only `goal`, `done_criterion`,
`criterion_type`, `weight_class`, `repo_root`, and each stage's `(index, stage_question_key)`.
It does **not** hash `means`, `method`, `conditions`, `invariants` or `verify_command`. The
`refinement` branch (`cli.py:3360-3384`) exists precisely to carry prose-only corrections, and
`no_change` (`cli.py:3319-3358`) is byte- or comment-identical; the comment at
`cli.py:3250-3253` confirms the gate applies to both. Neither moves the digest.

So: compute the proposed plan's digest, compare against `bag['enumerated_at']`, and fire the
clear + stamp + launch **only when they differ**. When they match, skip all three — the
existing result is still valid for this content, and clearing it would trip
`_ENUMERATE_NOT_RUN` and force a 15–480 s refuse-then-retry on a path that completes instantly
today. That is the liveness regression an unconditional clear introduces, and the reason the
gate is not optional.

**Consequence if the comparison is omitted:** every typo fix in a `verify_command` during a
DIAGNOSING retry costs a full enumeration round-trip.

### (c) A path reaches `approve` without `cmd_submit_plan` — the substantive replan

`machine.py:50-51` admits `approve` only from `PLAN_READY`, and `machine.py:50,76` show
`submit_plan` and `revise_plan` as the only *edges* into it. But the substantive-replan branch
does not use an edge: `cli.py:3401` assigns `state.plan_path = args.plan` and `cli.py:3407`
sets `state.node = Node.PLAN_READY.value` **directly** — documented as such at
`machine.py:29`. The session then reaches `cmd_approve` having never re-entered
`cmd_submit_plan`.

This settles (a) rather than merely supplementing it: **`cmd_replan` is a required launch site,
not an optional one.** A stage 4 that hooks only `cmd_submit_plan` ships a fix that works on
first approval and silently does nothing on every substantive replan — the exact shape of the
defect this task exists to remove.

The manual `agentctl question-enumerate` stays reachable regardless (`--plan`, `cli.py:1341`),
which is what the escape flow and re-runs use.

### (d) What the child needs to survive its parent

The fleet has a precedent; use it rather than inventing one. `scripts/proc_tree.py` provides
`launch_supervised` (`proc_tree.py:42-51`), which forces `start_new_session=True` so the child calls
`setsid()` and becomes its own session and process-group leader. Its module docstring
(`proc_tree.py:11-12`) records the empirical justification: a parent-only SIGTERM left 2 of 3
grandchildren alive; `start_new_session` + `killpg` left zero. The live in-repo caller is
`hook-review-monitor-arm.py:198-219`, which pairs `start_new_session=True` with
`stdin=subprocess.DEVNULL` and `stdout`/`stderr` to `os.devnull`.

Point by point:

- **Process group / session:** `start_new_session=True`, via `proc_tree.launch_supervised`. A
  plain `Popen` shares the parent's group, so whatever ends the agent's turn signals the child
  too — the failure mode that makes a "launch returns fast" test pass while the feature does
  not work.
- **stdio:** `stdin=DEVNULL`, `stdout`/`stderr` to `DEVNULL` or to a log file under the state
  dir. A child holding the parent's pipes can block on a full buffer or die on `SIGPIPE`.
- **Reaping:** the parent here is the short-lived `agentctl` CLI process, which exits within
  milliseconds of the launch. The child is reparented to `init`/`launchd`, which reaps it —
  so no zombie, and no `wait()` is needed. This differs from the long-lived-parent case
  `proc_tree.install_teardown` (`proc_tree.py:216`) exists for; stage 4 must state the
  disposition explicitly rather than leave it to accident.
- **Environment:** `Popen` with no `env=` inherits the parent's, which is how `PATH` and the
  `claude` credentials reach the child. This is inherited *today only because the caller is
  interactive* — stage 4 must not filter the environment, and a child that cannot find
  `claude` must land a sidecar recording that failure rather than vanishing.

**What the child is.** Not a bare `claude -p`: it must write the sidecar. Stage 4 launches a
dedicated `python3 -m agentctl` worker subcommand which loads the plan, calls
`advisor.enumerate_questions_health` (`advisor.py:111`) with a runner bound to
`ENUMERATE_TIMEOUT_S` instead of `advisor.py:456`'s default `_ADVISOR_TIMEOUT_S = 20`
(`advisor.py:22`), and writes the sidecar. It must **never** touch session state — see Q2.

A child that dies at 3 s and a child that runs 480 s and finds nothing are indistinguishable at
the gate. That is why the deadline stamp (Q3) is written by the *parent*, synchronously, at
launch.

## Q2 — the write race

**Confirmed, and worse than the plan assumed.** `store.py:49-52` — `FileStateStore.save()` is a
whole-state `write_text` of `state.to_json()`, with **no lock and no temp+rename**:

```python
def save(self, state: SessionState) -> None:
    state.check_invariants()
    self.root.mkdir(parents=True, exist_ok=True)
    self.path(state.session_id).write_text(state.to_json(), encoding="utf-8")
```

`cmd_question_enumerate` ends in exactly that call (`cli.py:1408`). A detached child doing the
same while the parent session runs other commands would clobber the parent's writes with a
stale read-modify-write snapshot — and, because `write_text` truncates before writing, a
concurrent reader can observe a truncated file. Silent, intermittent, on a gate whose whole
purpose is to be non-skippable.

This makes the sidecar **mandatory**, not merely preferable, and imposes an atomicity
requirement on the sidecar write itself.

**The design.** The child never writes session state. It writes a sidecar keyed by
`(session_id, plan content digest)` carrying: `runner_ok`, the `(target, question)` pairs, the
digest it was computed against, the child's exit status and captured stderr. The write is
`tempfile` + `os.replace` within the same directory, so a reader sees either the old file or
the complete new one, never a partial. The parent folds it in.

**Where the fold happens.** Not inside `premise_blockers` — `plugins.plugin_gate_blockers`
(`plugins.py:213-217`) passes `state.plugins.get(plugin.name) or {}`, so on a missing bag the
guardian would mutate a throwaway dict, and no gate-evaluation path calls `store.save()`
afterwards. The fold belongs in the mutating commands, immediately before they evaluate the
gate: `cmd_approve` (before `cli.py:2048`) and `cmd_replan` (inside the swapped block, before
`cli.py:3261`). `cmd_status` may fold read-only for display.

**Implementation note (stage 6, correcting the two claims above against the shipped code):**
the "costs nothing" / "already perform" framing above did not survive contact with the actual
save sites, on both counts:

- **A fold lost on a refusing `cmd_approve` does NOT cost nothing.** Shipped `cmd_approve`
  (`cli.py:2505-2512`) calls `store.save(state)` immediately after a mutating fold and
  **before** `plan_approval`'s blockers are computed — i.e. before the refusal path that
  returns without reaching the function's own success-path save. The governing comment names
  the cost directly: *"Persist BEFORE the gate is evaluated, not after: the blockers below are
  computed from the folded bag and name its `qenum-N` candidates, and this function returns on
  any blocker WITHOUT reaching its own `store.save()` — so a fold left in memory would refuse
  the approve while `question-candidate-dispose --id qenum-1` had nothing to find"*
  (`cli.py:2507-2511`). A lost fold would have left exactly that dangling reference; the extra
  save exists because it is not free, not because it is unnecessary.
- **`cmd_replan` does NOT persist the fold via a save it already performs.** The fold sits
  inside the swapped-`plan_path` `try` block, and `cmd_replan`'s ordinary save sites are
  unreached on the refusal path this fold shares. Shipped `cmd_replan` (`cli.py:3747,
  3755-3759, 3763-3771`) instead threads a dedicated `enumeration_bag_dirty` flag — set when
  either the fold mutates the bag or a fresh enumeration is launched — and saves on it
  explicitly, once the swapped `plan_path` is restored: *"AFTER the finally restored
  `plan_path` — a save inside the swapped block would persist the PROPOSED plan as the
  session's current one. Before the `pblock` return below, because this path refuses without
  reaching any of `cmd_replan`'s own save sites: unsaved, the deadline stamp Stage 5's escape
  reads would never exist on disk, and the not-run clear would leave the bag pinned to the
  superseded digest — i.e. the inescapable `_ENUMERATE_STALE`, the exact routing the clear
  exists to prevent"* (`cli.py:3763-3771`). This is a new save site the design above did not
  anticipate, not a reuse of an existing one.

**Why the digest key makes the fold safe.** A sidecar computed against different plan content
is discarded by the same rule the gate already applies at `plugins_premise.py:172` — the
comparison of `bag['enumerated_at']` against the live `content_digest`. A stale background
result therefore cannot discharge anything.

That rule only holds because the key is an assertion the *worker* makes, not one it inherits.
The design as written had the launcher hand `--digest` down and the worker write under it
verbatim, which bought key agreement at the price of content agreement: the plan can be edited
during the child's flight, and a sidecar keyed by the launcher's promise while carrying an
enumeration of other bytes would be folded as healthy — and the verb, which is in `COMMANDS`
and the parser, would accept any `--digest` a hand-caller typed. Shipped
`cmd_question_enumerate_worker` therefore recomputes `_plan_content_digest` from the doc it
actually loaded and refuses to write on disagreement, so the deadline expires into its escape
instead.

**What the key still does not cover.** `_plan_content_digest` is a digest of *parsed* content:
goal, done criterion, criterion type, weight class, repo root, and the per-stage question keys.
The enumeration reads the whole file, so `meta.final_check`, `meta.external_research`,
`meta.delivery_worktree` and comments can change without moving the key — an enumeration may
have read a pre-edit version of those. This is deliberate on both halves. Comments are excluded
by construction (tomllib never surfaces them). The three `meta` fields are excluded because the
gate's notion of "the plan changed" is deliberately narrower than "the bytes changed": widening
it would re-block approve — and, since the fold is digest-keyed, discard an in-flight
enumeration — on a `final_check` refinement, which
`test_digest_unchanged_replan_does_not_clear_or_relaunch` pins as a no-op on purpose. Carrying a
raw-bytes `sha256` in the payload and comparing it at fold time was considered and rejected for
the same reason: it would refuse folds for edits the gate itself treats as no-ops, so a comment
fix mid-flight would cost a full `ENUMERATE_TIMEOUT_S` wait and an escape. The residual is that
the *questions raised* may reflect a superseded `final_check`; the questions are advisory
candidates an operator dispositions, and the gate's identity is unaffected.

**Abandoned sidecars.** A sidecar left by a session that never returned is inert: it is keyed
by session id, so no other session reads it, and within its own session a digest mismatch
discards it. Stage 4 writes them under the state directory alongside the session JSON and
deletes a session's sidecars at `cmd_resolve`; a stale file is a few KB, not a correctness
hazard.

## Q3 — `approve` arrives before the pass lands

Two figures that must not be conflated: the user-visible **wait**, and the **latency to being
able to approve**.

**The answer is refusal *plus* the typed escape, not one of them.**

Refusal is the normal path: it is already the engine's vocabulary for "not yet"
(`Directive(False, …)`), it costs zero wall-clock, and it keeps the synchronous
plan-length-dependent wait — the thing the user rejected — out of the design entirely. The
directive must say what is pending and roughly how long, or a refusal with no horizon is just
a wall.

Refusal alone has no floor, and the code confirms why. A child that hangs, dies before
writing, or never starts leaves `enumerated` unset, so `plugins_premise.py:171` raises
`_ENUMERATE_NOT_RUN` forever. Stage 5's escape keys on `enumerated_runner_ok is False` — a
value only written once the child has landed (`cli.py:1405`). So in exactly the failure case
the escape exists for, there would be no escape and `approve` would be permanently refused:
a **worse** liveness posture than today's fail-open. Confirmed, not refuted. Hence
`enumeration_not_landed` is its own admissible reason in stage 5's closed set, distinct from a
runner failure because it means something different and should count separately.

**The precondition, and who writes it.** An absolute `enumerate_deadline` in the premise bag,
stamped by the **launching parent, synchronously, at every site Q1 names** — `cmd_submit_plan`
and `cmd_replan` — as `launch_instant + ENUMERATE_TIMEOUT_S`. A child that never starts must
still leave behind the record saying when it should have finished; a precondition read from a
field nobody assigns is not a precondition but a branch that never admits, and a suite whose
fixtures set the field by hand cannot see that.

**No existing field carries this fact.** `state.plan_submitted_ts` (`state.py:953`, stamped at
`cli.py:1557`) is the closest candidate and does not serve: it is a submission instant, not a
deadline; it is re-stamped on every `submit_plan`/`revise_plan`; and the substantive-replan
branch (`cli.py:3401-3407`) never touches it, so on the very path Q1(c) identified it would be
arbitrarily stale. `PlanPresentation.presented_ts` (`state.py:522`) is bound to a presentation
receipt and unrelated. A new bag field is warranted.

**The three numbers.**

| | value | what it is |
|---|---|---|
| User-visible synchronous wait at `approve` | **0 s** | the bag is read, not computed |
| Expected latency before `approve` stops refusing | **15–170 s** | the enumeration's own runtime across the measured range (stage-2 dataset) |
| Worst case before a route out exists | **480 s** | `ENUMERATE_TIMEOUT_S`, after which the escape admits `enumeration_not_landed` |

The middle number is the honest cost of detaching; the third is the honest cost of a hang.

## Open items

**OPEN-1 — sessions predating this change.** A premise bag minted before `enumerate_deadline`
existed has no such field, so the `enumeration_not_landed` escape cannot admit for it.
*Consequence for stage 4:* treat a missing `enumerate_deadline` as "no launch was ever made"
and fire the launch on the next `cmd_submit_plan`/`cmd_replan`, rather than inventing a
retroactive deadline. Already surfaced to the user in the plan essence; not a blocker.

**OPEN-2 — the zero-pair discharge.** A *successful* run returning zero pairs still discharges
the mandatory check (`cli.py:1402`, unconditional on the pair count; the rationale is in
`advisor.py:133-138`). Detaching does not touch this hole and stage 5's escape does not close
it, because `runner_ok` is `True`. *Consequence:* stage 6 must document it explicitly rather
than let the delivery read as if the check were now sound. **Addressed at stage 6:** documented
in [`memory-global/leaves/question-provenance-gate.md`](../../memory-global/leaves/question-provenance-gate.md)
§ Honest ceiling's rewritten F3b bullet, which names the zero-pair successful run explicitly as
a residual that still discharges silently by design and is **not** escapable (nothing failed,
so there is nothing to attach a typed escape reason to) — distinct from the runner-failure case
stage 5 closed. This item now points there rather than standing alone.

## Principle

When a difficulty is closed by changing *where* work runs rather than how long it may run, the
concurrency question **is** the design, and must be settled before implementation rather than
during it. Refutable: had `store.py` shown a per-key write path rather than a whole-state save,
Q2 would have collapsed to a formality and this stage could have folded into stage 4. It does
not — `store.py:52` is a whole-file truncating write — so the sidecar is load-bearing.
