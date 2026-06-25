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
        small change ──┘                                              DECOMPOSED
                       │                                                    │
                       └──────────────────────────────→  EXECUTING  ⇄  VERIFYING
                                                              │             │
                                  (difficulty → BLOCKED → replan → PLANNING)│
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
| `DECOMPOSED` | M1–M4 decomposition verdict recorded (ship as one PR or several). |
| `EXECUTING` | Running the active stage — in-thread or via a dispatched specialist. |
| `VERIFYING` | A stage result was recorded; checking it, then choosing next stage vs final. |
| `RESOLUTION` | All stages PASSED; **held at the resolution gate** awaiting user confirmation. |
| `RESOLVED` | User confirmed the task is done (terminal). |
| `BLOCKED` | A difficulty interrupted the flow; `unblock` / `replan` returns to the prior node. |

## Commands

The happy-path spine, in order:

```text
start → classify → plan → submit-plan → approve → decompose → next-stage
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
| `decompose` | Record the M1–M4 decomposition verdict (`APPROVED → DECOMPOSED`). |
| `next-stage` | Select the next ready stage and enter execution (`→ EXECUTING`). |
| `dispatch` | At `EXECUTING`, route the active stage to its actor (`in_thread` / `spawn:<specialization>`) and return the cognitive leaf + return-marker handling; no node change. |
| `record-result` | Record a stage's actual result + status (PASSED/FAILED) (`EXECUTING → VERIFYING`). |
| `verify-final` | Pass the final-verification gate: all stages PASSED + the plan's *Final verification* (`VERIFYING → RESOLUTION`). |
| `resolve` | Pass the resolution gate; `--by` names the confirmer (`RESOLUTION → RESOLVED`). |
| `replan` | On a difficulty, re-arm planning at `PLAN_READY` with a revised `--plan`. |
| `block` / `unblock` | Mark a difficulty (`any → BLOCKED`) and return to the prior node. |
| `resolve-permission` | Record the decision on a specialist's `PERMISSION-REQUEST`. |
| `status` | Inspect the current node + directive; no transition. |

## Modules

`classify`, `config`, `state`, `store`, `machine`, `gates`, `directive`, `cli`, `dispatch`, `decompose`, `permissions`, `plan`, `continuations`.

Two modules carry the load-bearing invariants:

- **`gates.py` is pure** — every guardian is `(state: SessionState) -> list[str]` (a list of blocker strings). No subprocess, no I/O, no changeset. `verify-agentctl.py` guards this purity and the `GUARDIANS → GATE_TO_HOOK → install-reminder-hooks` consistency.
- **`state.py` + `plan.py` are the canonical plan model** — see below.

## The plan model

Plan structure is defined **primarily by typed code**, not prose. `plan.py`'s `parse_plan` builds grouped dataclasses (`Subject`, `Means`, `Actor`, `Criterion`, `Principle`, `Supply`, `Outcome`) from the flat author TOML and validates the 8-element activity ontology for substantive plans. The canonical description of the 8 elements and where each lives in the schema is [`memory-global/leaves/plan-activity-ontology.md`](../../memory-global/leaves/plan-activity-ontology.md); [`verify-plan-file.py`](../verify-plan-file.py) is a prose mirror. On any divergence, the code wins.

## State location

Session state is JSON at `~/.claude/agentctl/state/<session_id>.json` (the durable machine-written record, kept separate from the human/LLM-authored TOML plan).

## Keeping this doc current

This README is a **registered concept doc** in [`../doc-bindings.json`](../doc-bindings.json) (concept `coordination-state-machine`): changing engine code under `scripts/agentctl/` should review this file in the same change. [`verify-doc-concepts.py`](../verify-doc-concepts.py) asserts the `## State machine` heading exists and the `Node` anchor still resolves; the commit-time reminder names this doc when engine code changes without it.
