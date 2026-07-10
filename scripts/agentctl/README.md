# `agentctl` — the coordination engine

`agentctl` is the **deterministic control-flow engine** for a substantive task. It owns the *spine* — classify → plan → approve → execute → verify → resolve — while **prose supplies the cognition** at each step (the classification judgment, the plan content, the marker handling). The canon: **code = deterministic control-flow, prose = cognition.** The engine never decides *what* the right answer is; it decides *which step is legal next* and *which gate blocks*.

Run it from the repo `scripts/` dir:

```bash
cd ~/claude-agent-instructions/scripts && python3 -m agentctl <cmd>
```

Each command returns a **Directive** (JSON): the next node, which cognitive leaf to run, and whether a gate blocks.

## State machine

A substantive task moves through this node lifecycle (`agentctl.state.Node`):

```text
start → CLASSIFIED → ROUTED → PLANNING → PLAN_READY ──■APPROVAL GATE■──→ APPROVED
                                            ⟲ revise_plan                    │
                       │              (resubmit pre-approval)          PARTITIONED
        small change ──┘                                                    │
                       │                                                    │
                       └──────────────────────────────→  EXECUTING  ⇄  VERIFYING
                                                              │             │
       (stage FAILED → DIAGNOSING: declare→investigate→critique → replan ──┘
                          → retry, or PLANNING on a substantive replan)      │
                                    ▲                                  RESOLUTION
                                    └───────────── reject ────────────────┘  │
                                        (user rejects the delivery)          │
                                                            ──■RESOLUTION GATE■──→ RESOLVED
```

The two gates (`■`) are **non-skippable**:

- **Approval gate** — [`hook-state-gate.py`](../hook-state-gate.py) hard-denies production Edit/Write until the engine reaches an execution node. "Production" includes the agent's own config/instructions; only memory and `/tmp/` scratch are unconditionally exempt.
- **Resolution gate** — requires explicit user confirmation; [`hook-resolution-reminder.py`](../hook-resolution-reminder.py) enforces the ask.

[`verify-agentctl.py`](../verify-agentctl.py) checks that every gate has its guardian hook and that the schema, transitions, and cognitive leaves stay consistent.

### States

Each node is a phase in the task lifecycle (`Node` in `state.py`). The *route* picked at `ROUTED` decides the path: **chat** ends there (answer in-thread), **small change** jumps straight to `EXECUTING`, **substantive** walks the full spine.

| State | Meaning |
|---|---|
| `CLASSIFIED` | Session armed; awaiting the weight-class judgment. |
| `ROUTED` | Weight class + route recorded. Terminal for chat (`DIRECT`); small change → `EXECUTING`; substantive → `PLANNING`. |
| `PLANNING` | Authoring the plan (stages, each with its result image + done criterion). |
| `PLAN_READY` | Plan authored; **held at the approval gate** until the user approves. |
| `APPROVED` | Plan-approval gate passed. |
| `PARTITIONED` | M1–M4 **delivery-partition** verdict recorded — into how many independently-shippable units (PRs/tickets) the approved plan is cut. |
| `EXECUTING` | Running the active stage — in-thread or via a dispatched specialist. |
| `VERIFYING` | A stage result was recorded; checking it, then choosing next stage vs final. |
| `RESOLUTION` | All stages PASSED; **held at the resolution gate** awaiting user confirmation. Exits via `resolve` (confirmed) or `reject` (user rejects the delivery, re-opening the difficulty cycle at `DIAGNOSING` — see `reject` below). |
| `RESOLVED` | User confirmed the task is done (terminal). |
| `DIAGNOSING` | A stage FAILED — or the user `reject`ed a `RESOLUTION`-gate delivery — entering the **overcome-difficulty** sub-spine. The engine runs `declare → investigate → critique` (filling the `Difficulty` record) and **blocks `replan` until the record is complete** (`gates.difficulty_blockers`); this same completeness check gates authoring the corrected plan file at `DIAGNOSING` (`hook-state-gate.py`'s `diagnosing_plan_write_ok`). When `critique` records the structured similarities/differences split, `replan` additionally enforces a **coverage gate** (`gates.replan_coverage_blockers`): every named similarity must reappear as a stage condition/invariant (normalized substring match) and naming any difference forces a means/method to change; a failing coverage gate can be bypassed with a recorded `--coverage-waiver <reason>` (never the difficulty-record precondition). `replan` is the sole exit — back to `VERIFYING` to retry, or to `PLAN_READY` for a substantive re-plan. The *cognition* of each phase lives in the `overcome-difficulty` skill. |
| `BLOCKED` | A structural blocker (spawn refusal, escalation) interrupted the flow; `unblock` / `replan` returns to the prior node. |

### Sub-plan stack

When a stage's unmet element requires a full substantive plan to supply it, the engine nests a **service sub-plan** via `push-subplan` / `pop-subplan`. On `push-subplan` a `PlanFrame` snapshot of the current parent context (plan path, node, stages, gates, repo root, `final_check`, `partition`, `originating_stage`) is pushed onto `state.plan_stack`; the child starts a fresh `CLASSIFIED → … → RESOLVED` cycle. On `pop-subplan` the parent frame is restored and the originating stage is marked `PASSED`. **No auto-pop across an unresolved child** — `pop-subplan`'s source node is `RESOLVED`, which already requires `resolution.passed`, so the guarantee is structural (machine transition table), not an extra check. Stack depth is bounded by `_MAX_PLAN_STACK` (mirrors `max-recursion-depth` from `~/.claude-agent/config.md`).

## Commands

The happy-path spine, in order:

```text
start → classify → plan → submit-plan → approve → partition → next-stage
      → dispatch → record-result → verify-final → resolve
```

| Command | Effect (transition) |
|---|---|
| `start` | Arm a session (idempotent with `--if-absent`; auto-run by `hook-engine-start.py` on each prompt). |
| `reset` | Re-arm for a new task at a task boundary; refuses mid-substantive without `--force`. |
| `classify` | Record weight class + route (`CLASSIFIED → ROUTED`). |
| `plan` | Enter planning for a substantive task (`ROUTED → PLANNING`). |
| `submit-plan` | Mark the plan authored and ready for approval (`PLANNING → PLAN_READY`). A TOML plan may set `[meta] repo_root` — the directory every stage's `verify_command` runs in (as `cd <repo_root> && <cmd>`); unset, commands inherit the invoker's cwd, so their paths must then be absolute. Called again **at `PLAN_READY`** (pre-approval revision, e.g. after a thinker `revise` verdict), it resubmits via the `revise_plan` self-loop instead of `submit_plan` — no `reset --force` needed — and unconditionally clears any recorded `plan-review` so that gate re-arms for the new plan version. |
| `plan-review` | Record a thinker's review of a plan version (`--verdict pass\|revise\|override --reviewer <name> [--concern …] [--note …] [--target <plan>]`). Backs the **plan-review gate** (`gates.plan_review_blockers`): on a SUBSTANTIVE session `approve` and `replan` are refused until a review exists that is bound to the exact current plan path and whose verdict is `pass` (or a user-authored `override` — distinct reviewer, non-empty note). `AGENTCTL_PLAN_REVIEW=1/0` force-enables/disables the gate. No node change. |
| `approve` | Pass the plan-approval gate (also gated by `plan_review_blockers`, see `plan-review`); `--by` names the approver (`PLAN_READY → APPROVED`). Also snapshots the plan **as approved** (content-hash-named file under the state dir, recorded as `plan_snapshot_path`/`plan_snapshot_hash`) — the immutable baseline `replan` diffs against, so an in-place edit of `plan_path` can never masquerade as "no change". |
| `partition` | Record the M1–M4 **delivery-partition** assessment — into how many independently-shippable, separately-reviewable units (PRs/tickets) the approved plan is cut. Delivery segmentation, **not** the planner's step-level decomposition (`APPROVED → PARTITIONED`). Optionally materializes the split as **units** via repeatable `--unit '<mode>\|<stages csv>\|<title>[\|<ref>]'` — each unit groups approved-plan stage indices (pairwise disjoint) under an org-neutral execution mode `inline` / `spawn` / `subtask`; see `memory-global/leaves/partition-markers.md` § Materialization. |
| `partition-units` | Record (or replace) the per-unit delivery routing **after** the verdict is surfaced — the user's structure decision arrives once they have seen the M1–M4 verdict. Same `--unit` syntax as `partition`; allowed only at `PARTITIONED` or `EXECUTING`; replaces the whole units list, leaves verdict + node untouched. Re-recording at `EXECUTING` does not re-validate against already-PASSED stages (documented limitation). |
| `next-stage` | Select the next ready stage and enter execution (`→ EXECUTING`). |
| `dispatch` | At `EXECUTING`, route the active stage to its actor (`in_thread` / `spawn:<specialization>`) and return the cognitive leaf + return-marker handling; no node change. For a spawn stage this command **is** the spawn — it runs `spawn-specialist.py` itself, synchronously (blocking until the specialist returns); do **not** spawn manually with `spawn-specialist.py` or `claude -p` first. `--dry-run` is a pure preview: echoes the spawn command with no event log, no state save, no marker routing. |
| `record-result` | Record a stage's actual result + status, plus an optional general `--control` attestation (how element #3, the control criterion, was met). PASSED → `VERIFYING`; FAILED → `DIAGNOSING` (enter the overcome-difficulty sub-spine). A `spawn:developer` stage is **refused** PASSED without a non-empty `--control` — see *Control attestation* below. |
| `declare` / `investigate` / `critique` | In `DIAGNOSING`, fill the three sections of the `Difficulty` record **in order** (the engine refuses out-of-order calls). Each records one phase's artifact; the content is the `overcome-difficulty` skill's cognition. `critique` also accepts the structured split `--invariant-to-preserve` / `--difference-to-remove` (both repeatable) that the `replan` coverage gate checks. |
| `verify-final` | Pass the final-verification gate: all stages PASSED + the plan's *Final verification* (`VERIFYING → RESOLUTION`). |
| `resolve` | Pass the resolution gate; `--by` names the confirmer (`RESOLUTION → RESOLVED`). |
| `reject` | The resolution gate's **negative exit**: the user rejects the delivery as not matching intent (`RESOLUTION → DIAGNOSING`). Requires `--reason <text>` (non-empty; seeds the `Difficulty` record's `declaration.actual`); marks the stage(s) named by repeatable `--stage <n>` FAILED, defaulting to the final stage so a reject is never a structural no-op. Hands off to `overcome-difficulty` exactly like a stage failure — `declare → investigate → critique → replan`. |
| `replan` | Apply a revised `--plan`. **Precondition-gated** in `DIAGNOSING`: refused until the `Difficulty` record is complete, and — when the critique recorded a split — until the **coverage gate** passes (similarities carried into conditions/invariants, matched by substring after normalizing case/whitespace on both sides; a means/method changed for the declared differences). A failing coverage gate can be bypassed with `--coverage-waiver <reason>` — refused if the reason is empty, recorded in session history and the gate log (`replan_coverage_waiver`), and never bypasses the difficulty-record completeness precondition. Diffs the corrected plan against the **approved snapshot** (`plan_snapshot_path`, falling back to `plan_path` for a legacy pre-snapshot session), never the possibly-in-place-edited `plan_path`. A means/method/conditions/invariants/verify_command/expected_exit-only delta (or a `[meta] repo_root` change) is a **refinement** (retries the re-armed stage, `→ VERIFYING`; carries the corrected check command into live state without resetting PASSED stages); executor/done-criterion changes are **substantive** (re-arm the approval gate, `→ PLAN_READY`, **carrying forward PASSED status** — and its recorded `Outcome` — for any stage whose full definition is unchanged by the diff). |
| `block` / `unblock` | Mark a difficulty (`any → BLOCKED`) and return to the prior node. |
| `resolve-permission` | Record the decision on a specialist's `PERMISSION-REQUEST`. |
| `plugin-activate` | Activate a registered plugin for this session (`--plugin <name>`); the owning skill calls it on invocation. See § Plugins. |
| `plugin-deactivate` | Manual escape hatch: deactivate an active plugin (`--plugin <name>`). |
| `plugin-record` | Mark a plugin phase done (`--plugin <name> --phase <…>`, e.g. `experience`'s `searched` / `recorded` / `skipped`; a `skipped` requires `--note`). |
| `push-subplan` | Start a service sub-plan: snapshot parent context onto `plan_stack`, arm child plan (`EXECUTING → CLASSIFIED`). Requires `--plan <path>`; `--originating-stage <n>` is optional, defaulting to the session's current stage. |
| `pop-subplan` | Finish a service sub-plan: restore parent frame from `plan_stack`, mark originating stage PASSED (`RESOLVED → EXECUTING`). |
| `status` | Inspect the current node + directive; no transition. |
| `drive` | **Orchestrator** — walk the *opening* spine (`classify → … → next-stage`) in one call, firing only legal forward edges from the current node. **Stops at the plan-approval gate** (`PLAN_READY`) unless given `--approved-by <who>` (threaded into `approve --by`). Routes chat/small-change without a plan gate; stops at `PARTITIONED` when the M1–M4 verdict suggests a split. Idempotent (no-op at/after `EXECUTING`). Adds **no** node/transition/gate. |
| `close` | **Orchestrator** — walk the *closing* spine (`record-result → verify-final → resolution-probe`) in one call. **Stops at the resolution gate** (`RESOLUTION`) unless given `--confirmed-by <who>`. Surfaces resolution blockers — core **and** plugin-phase (e.g. `experience`) — by delegating to `cmd_resolve` (empty `--by` = read-only probe). A FAILED stage routes to `DIAGNOSING` and is surfaced, never swallowed; remaining stages are never auto-run. Idempotent (no-op at `RESOLVED`). Adds **no** node/transition/gate. |

### Spine orchestrators (`drive` / `close`)

`drive` and `close` are **thin orchestrators**: they sequence the existing `cmd_*` commands above and branch on the `Directive` each returns — collapsing the ~15–20 hand-issued calls per task into two invocations (the [CLAUDE.md root principle](../../CLAUDE.md) *"Separate rule from perception; determinize the rule at its proper structural level"*, whose named canonical instance is this spine — the engine mechanizes the rule, the coordinator supplies the perception). They introduce **no new node, machine edge, or gate**: every state mutation is performed by a delegated `cmd_*`, so the engine's invariants hold by construction. Their one rule beyond sequencing is to **never auto-cross a human gate** — `drive` halts at `PLAN_READY` and `close` at `RESOLUTION` unless handed the explicit `--approved-by` / `--confirmed-by` token, which the coordinator may pass **only after** the real user-approval / confirmation round. Each returns the full step `trace` under `Directive.data`.

### Control attestation (`record-result --control`)

The **control criterion** (element #3 of the plan activity ontology) is a general property of *every* stage: how the result's conformance to the order is checked. `record-result` carries it as an optional general field, `--control <attestation>`, stored on the stage. It is mandatory in exactly one data-driven case: a stage whose **actor** (element #6) is `spawn:developer`. Recording such a stage PASSED without a non-empty `--control` is **refused** (a Directive, no node transition) — `Stage.needs_control()` is true iff the actor is `spawn:developer`, and the precondition lives in `cmd_record_result` as the single chokepoint where PASSED is set. `status=failed` and every non-developer stage (`in_thread`, other `spawn:*`) are unaffected, and `--control` stays optional-and-accepted on them (any stage may attest its control). Review is the value this control criterion takes for a delegated code producer — there is deliberately no review-specific command: the engine enforces the general structural fact, the specific content (reviewed by whom, or a waiver with its reason) is free-text cognition (**code = control-flow, prose = cognition**).

### Advisory judge (warn-only)

`advisor.judge(kind, payload, runner, enabled=...)` shells a fail-open `claude -p --model sonnet` judgment (hard `_ADVISOR_TIMEOUT_S`-second timeout via `advisor.subprocess_runner`) at four cognition points: weight classification, plan completeness, hypothesis genuine-distinctness, and acceptance-observation adequacy. Results (a `list[str]` of advisory strings) are attached under `directive.data['advisories']` by `_attach_advisories`. **Non-blocking contract: `judge` never sets `directive.ok = False` and never changes `directive.node`.** With the advisor disabled or errored the return is `[]` and control flow is byte-identical to advisor-absent — preserving the engine's determinism canon (**code = deterministic control-flow, prose = cognition**). The advisor is the cognition half of the hybrid design: deterministic gates **block**; the advisor **warns**.

`_attach_advisories` is the single chokepoint that resolves `enabled` via `advisor.resolve_enabled(weight_class)`, threading each call site's session `weight_class` through rather than re-deriving the rule per site: `AGENTCTL_ADVISOR` env overrides in both directions (`1` forces on, `0` forces off); absent the env override, the advisor is on iff config.md's `advisor-mode` key (`off` | `substantive`, shipped `substantive`) is `substantive` **and** the session's `weight_class` is `SUBSTANTIVE` — auto-activation is scoped to substantive work only, and a missing/unreadable `advisor-mode` key fails closed (off).

## Modules

Core spine: `classify`, `config`, `state`, `store`, `machine`, `gates`, `directive`, `cli`, `dispatch`, `partition`, `permissions`, `plan`, `continuations`, `advisor`, `cost`, `exempt_paths`, `text_shape`. Plugin framework: `plugins`, `plugins_tracker`, `plugins_experience` (§ Plugins). `cost` is covered in § Cost tracking; `exempt_paths` backs the `hook-state-gate.py` production-path lockstep, `text_shape` renders Directive text.

Two modules carry the load-bearing invariants:

- **`gates.py` is pure** — every guardian is `(state: SessionState) -> list[str]` (a list of blocker strings). No subprocess, no I/O, no changeset. `verify-agentctl.py` guards this purity and the `GUARDIANS → GATE_TO_HOOK → install-reminder-hooks` consistency.
- **`state.py` + `plan.py` are the canonical plan model** — see below.

## The plan model

Plan structure is defined **primarily by typed code**, not prose. `plan.py`'s `parse_plan` builds grouped dataclasses (`Subject`, `Means`, `Actor`, `Criterion`, `Principle`, `Supply`, `Outcome`) from the flat author TOML and validates the 8-element activity ontology for substantive plans. The canonical description of the 8 elements and where each lives in the schema is [`memory-global/leaves/plan-activity-ontology.md`](../../memory-global/leaves/plan-activity-ontology.md); [`verify-plan-file.py`](../verify-plan-file.py) is a prose mirror. On any divergence, the code wins.

**Substantive plans must be TOML.** `submit-plan` refuses a substantive plan whose path does not end in `.toml` — markdown may mirror a plan as prose but cannot carry typed stages the engine tracks. Every substantive stage must also declare `capability_required` (Actor-cluster: the skill or knowledge the executor needs); `parse_plan` raises `PlanError` on any missing field.

**`verify_command` is mandatory on measurable substantive stages.** A measurable criterion you cannot execute is really `acceptance_review` — `_validate_substantive_stage` raises `PlanError` if `criterion_type == measurable` and `verify_command` is absent.

**Acceptance-review passes require a distinct recorded observation.** When a stage's `criterion_type` is `acceptance_review`, `record-result --status passed` requires `--observation <text>` that (after normalization) differs from `subject.result` (the expected image). The reviewer records what they actually observed, not an echo of the target.

**Executable end-to-end checks (`[[final_check]]`).** An optional typed `[[final_check]]` list (each entry: `command`, `expected_exit`, `label`) may be added to the plan. `verify-final` runs each entry after the per-stage re-runs; any mismatch refuses the `RESOLUTION` transition. Absent: back-compat (empty list, behaviour unchanged). Turns the plan's *Final verification* from prose the engine never reads into a machine fact.

## Plugins

The core spine above is a **closed monolith** — its nodes and gates are the contract every session obeys, edited only via `self-improvement`. But a skill/tool/specialization whose own workflow is deterministic (most acutely `tracker-management`) can hang a **sub-state-machine** off that spine without touching the three core literals (`machine.TRANSITIONS`, `gates.GUARDIANS`, `cli.COMMANDS`). That mechanism is [`plugins.py`](plugins.py).

A `Plugin` (registered into the module-level `REGISTRY` at import) carries:

- **observers** — `{event: fn(state, bag) -> [PluginDirective]}`. Each coordination command maps to one event (`EVENT_FOR_COMMAND`); after the command runs, `cli._fire_plugins` fires the event on every *active* plugin and appends any `PluginDirective`s under `Directive.data.plugin_directives` (nudges the coordinator surfaces). A plugin-less session is byte-identical — no key added.
- **gates** — `{core_gate: fn(state, bag) -> [blocker]}`. A plugin folds extra blockers into an existing core gate (`resolution` / `plan_approval`) via `plugin_gate_blockers`; it never adds a new gate.
- a per-session **bag** — `state.plugins[name]`, opaque to the core, seeded by `state_factory`.
- a **lifecycle** — `scope` `task` | `phase` and an optional `terminal(state, event)` predicate that auto-retires the plugin mid-task (bag archived into `state.plugins_archive`).

**Registration vs activation** are distinct: registration (import-time, into `REGISTRY`) means the engine *knows* the plugin; activation (`agentctl plugin-activate --plugin <name>`, presence of the name in `state.plugins`) means it *participates in this session*. The owning skill activates on invocation; `plugin-deactivate` is the manual escape hatch, `plugin-record` marks a publish-style phase done. **State is per-session** (keyed by `session_id`), so subtasks share the session's active set while spawned/parallel agents are isolated.

**Engine auto-activation for skill-less plugins.** A plugin need not have an owning skill to activate it. Each `Plugin` may carry an optional `auto_activate(state) -> bool` predicate; `auto_activate_for(state)` — called by `cmd_classify` once the route resolves to SUBSTANTIVE — activates every predicate-true plugin not already active or archived. It is idempotent and **name-free** (no plugin name hardcoded in `cli`), so a universal obligation rides the engine via a general predicate rather than a special case. `dummy`/`tracker` leave `auto_activate=None` (unchanged); a chat/small session activates nothing.

**Scope fence (deliberate):** a plugin introduces **no new `Node`** and **no new gate** — it only reacts to existing transitions and extends existing gates. New control-flow primitives belong in the core spine via `self-improvement`, not in a plugin. This keeps the lifecycle the core can reason about finite.

Two plugins ship in-tree: a built-in **`dummy`** (proves the framework end-to-end with zero core edits; phase-scoped, exercises auto-retire) and **`tracker`** ([`plugins_tracker.py`](plugins_tracker.py)) — the first real consumer. `tracker` observes `submit_plan`/`record_result`/`replan`/`resolve` to surface `publish_*` directives and gates `resolution` until the mandatory ticket publications (plan + result + the ticket status transition) are recorded; the `tracker-management` skill owns the comment content and transport (engine owns *when*, skill owns *what*). It also observes `partition`/`partition_units` to **materialize delivery units** (non-blocking nudges, never gates): on a `recommended` verdict it proposes the delivery structure (subtickets vs several PRs) citing the fired markers, and for every recorded unit with `mode == subtask` and no `ref` it nudges to create a subticket and re-record the unit with the key as `ref` (the nudge converges silent once `ref` is present). `verify-agentctl.py` asserts both register and that the tracker gate is wired.

A third plugin, **`experience`** ([`plugins_experience.py`](plugins_experience.py)), is the **skill-less** consumer and the first user of `auto_activate`: its predicate is `state.weight_class == SUBSTANTIVE`, so every substantive task gets it without any owning skill or manual `plugin-activate`. It observes `resolve` to surface a `record_experience` nudge (the `search → extend-vs-new → write` flow) and gates `resolution` until the leaf flow is recorded — bag complete when `searched` **and** (`recorded` **or** `skipped`). A skip must carry a reason: `plugin-record --plugin experience --phase skipped` requires `--note`, so declining a leaf below the quality bar is a conscious, recorded act rather than an inescapable block. `plugin-record --phase <searched|recorded|skipped>` marks each phase; the plugin auto-retires on the passing `resolve`. `verify-agentctl.py` asserts it registers and wires the `resolution` gate. This closes the deferred experience-search backlog item — the search-before-record step is now engine-gated, not prose-only.

**Plugins vs hooks — two determinization surfaces.** A *plugin* hangs off the coordination spine: it fires on engine commands and folds gate blockers, so it fits obligations that live *on* the task lifecycle (publish-on-resolve, record-on-resolve). Obligations that fire *off* the spine — at tool-write time or prompt time — are **hooks** instead, because there is no coordination command to observe. Two ship for memory work, both **non-blocking** (exit 0 always, preserving memory's gate-exempt status):

- [`hook-memory-consistency.py`](../hook-memory-consistency.py) (`PreToolUse` Write/Edit) classifies the target as a memory leaf in any of the **three** scopes (instruction-repo `memory-global/leaves/`, project `.claude/agent-memory/`, personal `~/.claude-agent/projects/*/memory/` — the last is invisible to `verify-leaf-structure.py`) and surfaces missing/malformed frontmatter (`name`/`description`/`type`) plus an index-pointer reminder. It only informs; it never denies.
- [`hook-experience-record-reminder.py`](../hook-experience-record-reminder.py) (`UserPromptSubmit`) reads the `experience` plugin bag and nudges to record before close — loudest at `node == RESOLUTION` (naming the exact missing phase and the `record-experience.py search …` → `plugin-record` commands), a soft nudge otherwise, silent when the flow is complete or the plugin is inactive. It mirrors `hook-tracker-publish-reminder.py`.

## Loop primitives: the turn-end gate, the acceptance judge, and composition with `/goal`

Three primitives adopted from Claude Code's loop taxonomy, plus one deliberately deferred. The first two are **landed mechanisms**; the third is a recorded non-decision.

### The turn-end Stop gate (the exit gate)

[`hook-turn-end-gate.py`](../hook-turn-end-gate.py) runs on the harness **Stop** event and is the **exit** counterpart to `hook-state-gate.py`'s **entry** gate on production edits. Its architecture mirrors `gates.py` one level up: an impure shell does all I/O and freezes a `TurnContext`; a module-level `TURN_GUARDIANS` registry maps a guardian name to a **pure** `(TurnContext) -> list[str]` function. Two guardians ship — `self_improvement_blockers` (fires on a corrective-feedback turn where neither `self-improvement` nor `overcome-difficulty` was invoked) and `resolution_turn_blockers` (fires only on a SUBSTANTIVE session whose stages are all PASSED, the resolution gate is unpassed, and the ending turn shows no closure being sought — neither an `AskUserQuestion` nor an armed `sleep >= 2` background deferral timer, the latter detected via the shared `timer_arm_detect.py` so the gate and `hook-ask-defer-timer.py` cannot drift).

Three loop-safety properties live **once** in the shell, not per obligation: a `stop_hook_active` short-circuit, a durable per-message marker keyed on `sha256(session + triggering user text)` (so the same message blocks at most once), and fail-open on any malformed/empty/unreadable input. Every fired guardian is **aggregated into ONE block** whose numbered reason names each unmet obligation, so N obligations cost exactly one extra model turn. The trade this buys is stated in the hook: aggregation gives up per-obligation *enforcement* (if the model addresses only obligation 1 of 2, the second stop is allowed) in exchange for turn-boundedness — accepted because a Stop hook that can block a session more than once per message has unbounded blast radius, and because the block text names every obligation. When `self_improvement` and `resolution` co-fire, resolution is named **last** and is enforced only when it is the sole fired guardian; `hook-resolution-reminder.py` stays registered on `UserPromptSubmit` as the complementary advisory for the residual co-firing hole. `verify-agentctl.py`'s gate-to-hook check maps the resolution gate to this hook.

### The acceptance-review judge

For a `criterion_type = acceptance_review` stage, `record-result --status passed` invokes an external cheap judge (a fail-open `claude -p` on the small-fast tier via `advisor.subprocess_runner`) that reads the stage's expected image plus the executor's `--observation` and returns a yes/no verdict. The verdict is recorded on `SessionState` bound to `sha256(observation)` by `cmd_stage_review` (a `StageReview` dataclass), exactly as `plan_review` binds a plan review to the reviewed plan's bytes — defeating "get a pass, then rewrite the observation".

The gate itself is a **pure** function, `gates.acceptance_review_blockers(state, stage)`, that reads only the recorded review — no subprocess, no LLM, no network. It refuses PASSED when the stage is `acceptance_review` and no matching review exists, the `observation_sha256` does not match, or the verdict is `revise`/unknown; a `verdict=override` clears only with a non-empty reviewer **and** note. Like `plan_review_blockers` it is an **internal command precondition and deliberately NOT a member of `gates.GUARDIANS`**, so `verify-agentctl.py`'s check needs no new hook and `gates.py` still imports no subprocess. Scoped to SUBSTANTIVE sessions with its own kill switch `AGENTCTL_STAGE_REVIEW` (not routed through the advisor's cost knob, because a mandatory gate's off switch must not be the advisor's optional one). Because the judge is fail-open (a timeout records no verdict) the **gate is fail-closed** (no verdict blocks) — an infra failure never silently passes a stage; the override escape means it can never wedge the user. That fail-open-judge / fail-closed-gate asymmetry is written into `gates.py`'s module docstring as reasoning, not merely enacted.

Honest limit: unlike Anthropic's `/goal` evaluator (which the *harness* invokes and the agent cannot reach), our judge is **agent-invoked**, so `AGENTCTL_STAGE_REVIEW=0` restores "do not lie to exit" one env var away. We make the bypass impossible to take *silently* rather than impossible to take: every bypass (kill switch or override) is recorded on `SessionState.judge_bypassed`, never cleared by a later passing review, and `verify-final` refuses a clean bill without printing it — so it reaches the user's resolution confirmation instead of dying in a log.

### Composition with `/goal`, and the four places this engine stays stronger

`/goal` and `agentctl` are **orthogonal** and compose. `/goal` is a session-scoped prompt-based Stop hook: a small fast model evaluates a natural-language condition each turn and returns yes/no + a reason. It keeps the *session looping*; `agentctl` decides *what is legal inside that loop*. A `/goal` evaluator naturally sits as a **reader** of our `[[final_check]]` commands — it can only judge what the agent surfaced in the conversation and cannot call tools, whereas the engine **executes** those commands and checks `expected_exit`.

Four properties this system must not regress toward a prompt-based loop:

| Property | agentctl | prompt-based `/goal` loop |
|---|---|---|
| Criterion | execution-verified — `verify_command` / `final_check` with `expected_exit` | reads a *report* of a test run it cannot run |
| Numeric bounds | checked by code (`max-recursion-depth`, `_MAX_PLAN_STACK`, budget tiers) | a turn cap living in the condition *text*, model-judged |
| Production edits | an **entry** gate (`hook-state-gate.py`) **and** an **exit** gate (the turn-end gate) | none |
| Divergence | a typed difficulty object (FAILED → DIAGNOSING → declare/investigate/critique → replan with a coverage gate) — the loop *learns* | repeats the same prompt |

### Deferred: the time-axis scheduler primitive

`/loop`'s local interval and `/schedule`'s cloud routine are **explicitly deferred, not built**. Rationale: (1) we already drive long jobs with `ScheduleWakeup` plus detached zero-token pollers per [`memory-global/leaves/long-job-monitoring.md`](../../memory-global/leaves/long-job-monitoring.md); (2) a routine that survives with no open session is a harness capability we do not currently need; (3) a scheduler would duplicate the harness rather than determinize anything of ours. Tracked so it is revisitable rather than forgotten: <!-- DEFERRAL-ISSUE --> _(pending: Core `backlog` issue on `sthe0/claude-agent-instructions`)_. If a concrete recurring need for a session-independent scheduled routine appears (a cadence `ScheduleWakeup` cannot serve), that item is where the evidence should land and reopen the decision.

## Cost tracking

`record-result` attributes spawn-stage cost from `~/.local/log/claude-spawn-costs.jsonl` (written by `spawn-specialist.py`) and stamps `cost_usd / duration_ms / spawn_count` on each spawn stage's `Outcome`. In-thread and main-session tokens are **not** split per stage — use `scripts/cost-report.py` for the whole-session estimate.

`verify-final` aggregates per-stage attributed costs into `SessionState.cost` (a `CostRollup`). `resolve` includes the `CostRollup` in `Directive.data` so the manager can fill the experience-leaf `## Cost` section from real figures rather than a TODO placeholder.

## State location

Session state is JSON at `~/.claude-agent/agentctl/state/<session_id>.json` (the durable machine-written record, kept separate from the human/LLM-authored TOML plan).

## Keeping this doc current

This README is a **registered concept doc** in [`../doc-bindings.json`](../doc-bindings.json) (concept `coordination-state-machine`): changing engine code under `scripts/agentctl/` should review this file in the same change. [`verify-doc-concepts.py`](../verify-doc-concepts.py) asserts the `## State machine` heading exists and the `Node` anchor still resolves; the commit-time reminder names this doc when engine code changes without it.
