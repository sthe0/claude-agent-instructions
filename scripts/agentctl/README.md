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

## Command sequence

```text
start → classify → plan → submit-plan → approve → decompose → next-stage
      → dispatch → record-result → verify-final → resolve
```

Plus the side-channels: `replan` (difficulty → re-arm at `PLAN_READY`), `block` / `unblock`, `reset` (re-arm for a new task), `status` (inspect).

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
