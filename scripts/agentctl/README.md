# `agentctl` — the coordination engine

`agentctl` is the **deterministic control-flow engine** for a substantive task. It owns the *spine* — classify → plan → approve → execute → verify → resolve — while **prose supplies the cognition** at each step (the classification judgment, the plan content, the marker handling). The canon: **code = deterministic control-flow, prose = cognition.** The engine never decides *what* the right answer is; it decides *which step is legal next* and *which gate blocks*.

Run it from the repo `scripts/` dir:

```bash
cd scripts && PYTHONPATH=scripts python3 -m agentctl <cmd>
```

Each command returns a **Directive** (JSON): the next node, which cognitive leaf to run, and whether a gate blocks.

## State machine

A substantive task moves through this node lifecycle (`agentctl.state.Node`):

```text
start → CLASSIFIED → ROUTED → PLANNING → PLAN_READY ──■APPROVAL GATE■──→ APPROVED
                       │                                                    │
        small change ──┘                                              PARTITIONED
                       │                                                    │
                       └──────────────────────────────→  EXECUTING  ⇄  VERIFYING
                                                              │             │
       (stage FAILED → DIAGNOSING: declare→investigate→critique → replan ──┘
                          → retry, or PLANNING on a substantive replan)      │
                                                                       RESOLUTION
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
| `RESOLUTION` | All stages PASSED; **held at the resolution gate** awaiting user confirmation. |
| `RESOLVED` | User confirmed the task is done (terminal). |
| `DIAGNOSING` | A stage FAILED: the **overcome-difficulty** sub-spine. The engine runs `declare → investigate → critique` (filling the `Difficulty` record) and **blocks `replan` until the record is complete** (`gates.difficulty_blockers`). `replan` is the sole exit — back to `VERIFYING` to retry, or to `PLAN_READY` for a substantive re-plan. The *cognition* of each phase lives in the `overcome-difficulty` skill. |
| `BLOCKED` | A structural blocker (spawn refusal, escalation) interrupted the flow; `unblock` / `replan` returns to the prior node. |

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
| `submit-plan` | Mark the plan authored and ready for approval (`PLANNING → PLAN_READY`). |
| `approve` | Pass the plan-approval gate; `--by` names the approver (`PLAN_READY → APPROVED`). |
| `partition` | Record the M1–M4 **delivery-partition** assessment — into how many independently-shippable, separately-reviewable units (PRs/tickets) the approved plan is cut. Delivery segmentation, **not** the planner's step-level decomposition (`APPROVED → PARTITIONED`). |
| `next-stage` | Select the next ready stage and enter execution (`→ EXECUTING`). |
| `dispatch` | At `EXECUTING`, route the active stage to its actor (`in_thread` / `spawn:<specialization>`) and return the cognitive leaf + return-marker handling; no node change. |
| `record-result` | Record a stage's actual result + status. PASSED → `VERIFYING`; FAILED → `DIAGNOSING` (enter the overcome-difficulty sub-spine). |
| `declare` / `investigate` / `critique` | In `DIAGNOSING`, fill the three sections of the `Difficulty` record **in order** (the engine refuses out-of-order calls). Each records one phase's artifact; the content is the `overcome-difficulty` skill's cognition. |
| `verify-final` | Pass the final-verification gate: all stages PASSED + the plan's *Final verification* (`VERIFYING → RESOLUTION`). |
| `resolve` | Pass the resolution gate; `--by` names the confirmer (`RESOLUTION → RESOLVED`). |
| `replan` | Apply a revised `--plan`. **Precondition-gated** in `DIAGNOSING`: refused until the `Difficulty` record is complete. A refinement retries the re-armed stage (`→ VERIFYING`); a substantive change re-arms the approval gate (`→ PLAN_READY`). |
| `block` / `unblock` | Mark a difficulty (`any → BLOCKED`) and return to the prior node. |
| `resolve-permission` | Record the decision on a specialist's `PERMISSION-REQUEST`. |
| `status` | Inspect the current node + directive; no transition. |

## Modules

`classify`, `config`, `state`, `store`, `machine`, `gates`, `directive`, `cli`, `dispatch`, `partition`, `permissions`, `plan`, `continuations`.

Two modules carry the load-bearing invariants:

- **`gates.py` is pure** — every guardian is `(state: SessionState) -> list[str]` (a list of blocker strings). No subprocess, no I/O, no changeset. `verify-agentctl.py` guards this purity and the `GUARDIANS → GATE_TO_HOOK → install-reminder-hooks` consistency.
- **`state.py` + `plan.py` are the canonical plan model** — see below.

## The plan model

Plan structure is defined **primarily by typed code**, not prose. `plan.py`'s `parse_plan` builds grouped dataclasses (`Subject`, `Means`, `Actor`, `Criterion`, `Principle`, `Supply`, `Outcome`) from the flat author TOML and validates the 8-element activity ontology for substantive plans. The canonical description of the 8 elements and where each lives in the schema is [`memory-global/leaves/plan-activity-ontology.md`](../../memory-global/leaves/plan-activity-ontology.md); [`verify-plan-file.py`](../verify-plan-file.py) is a prose mirror. On any divergence, the code wins.

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

Two plugins ship in-tree: a built-in **`dummy`** (proves the framework end-to-end with zero core edits; phase-scoped, exercises auto-retire) and **`tracker`** ([`plugins_tracker.py`](plugins_tracker.py)) — the first real consumer. `tracker` observes `submit_plan`/`record_result`/`replan`/`resolve` to surface `publish_*` directives and gates `resolution` until the mandatory ticket publications (plan + result) are recorded; the `tracker-management` skill owns the comment content and transport (engine owns *when*, skill owns *what*). `verify-agentctl.py` asserts both register and that the tracker gate is wired.

A third plugin, **`experience`** ([`plugins_experience.py`](plugins_experience.py)), is the **skill-less** consumer and the first user of `auto_activate`: its predicate is `state.weight_class == SUBSTANTIVE`, so every substantive task gets it without any owning skill or manual `plugin-activate`. It observes `resolve` to surface a `record_experience` nudge (the `search → extend-vs-new → write` flow) and gates `resolution` until the leaf flow is recorded — bag complete when `searched` **and** (`recorded` **or** `skipped`). A skip must carry a reason: `plugin-record --plugin experience --phase skipped` requires `--note`, so declining a leaf below the quality bar is a conscious, recorded act rather than an inescapable block. `plugin-record --phase <searched|recorded|skipped>` marks each phase; the plugin auto-retires on the passing `resolve`. `verify-agentctl.py` asserts it registers and wires the `resolution` gate. This closes the deferred experience-search backlog item — the search-before-record step is now engine-gated, not prose-only.

**Plugins vs hooks — two determinization surfaces.** A *plugin* hangs off the coordination spine: it fires on engine commands and folds gate blockers, so it fits obligations that live *on* the task lifecycle (publish-on-resolve, record-on-resolve). Obligations that fire *off* the spine — at tool-write time or prompt time — are **hooks** instead, because there is no coordination command to observe. Two ship for memory work, both **non-blocking** (exit 0 always, preserving memory's gate-exempt status):

- [`hook-memory-consistency.py`](../hook-memory-consistency.py) (`PreToolUse` Write/Edit) classifies the target as a memory leaf in any of the **three** scopes (instruction-repo `memory-global/leaves/`, project `.claude/agent-memory/`, personal `~/.claude/projects/*/memory/` — the last is invisible to `verify-leaf-structure.py`) and surfaces missing/malformed frontmatter (`name`/`description`/`type`) plus an index-pointer reminder. It only informs; it never denies.
- [`hook-experience-record-reminder.py`](../hook-experience-record-reminder.py) (`UserPromptSubmit`) reads the `experience` plugin bag and nudges to record before close — loudest at `node == RESOLUTION` (naming the exact missing phase and the `record-experience.py search …` → `plugin-record` commands), a soft nudge otherwise, silent when the flow is complete or the plugin is inactive. It mirrors `hook-tracker-publish-reminder.py`.

## State location

Session state is JSON at `~/.claude/agentctl/state/<session_id>.json` (the durable machine-written record, kept separate from the human/LLM-authored TOML plan).

## Keeping this doc current

This README is a **registered concept doc** in [`../doc-bindings.json`](../doc-bindings.json) (concept `coordination-state-machine`): changing engine code under `scripts/agentctl/` should review this file in the same change. [`verify-doc-concepts.py`](../verify-doc-concepts.py) asserts the `## State machine` heading exists and the `Node` anchor still resolves; the commit-time reminder names this doc when engine code changes without it.
