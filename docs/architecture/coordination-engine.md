# The coordination engine and its state machine

> How a substantive task is driven deterministically: `agentctl` (Layer 3) owns the control-flow, while prose supplies the cognition at each step. The canon — code = deterministic control-flow, prose = cognition.

`agentctl` is a code state machine. It owns the **deterministic control-flow** of a substantive task — the order of the steps, which gates block, and which cognitive leaf runs next — so that the spine is executed reliably rather than re-derived as prose each turn. The manager (the main dialog) supplies the **cognition** at each step: the classification judgment, the plan content, the handling of each specialist's return marker.

```bash
cd scripts && PYTHONPATH=scripts python3 -m agentctl <cmd>
# start → classify → plan → submit-plan → approve → next-stage → dispatch → record-result → verify-final → resolve
```

## The state machine

A substantive task moves through these states:

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
                                                                            │
                                                            ──■RESOLUTION GATE■──→ RESOLVED
```

## The two gates

Two gates (marked `■`) are **non-skippable**, enforced by guardian hooks:

- The **approval gate** holds at `PLAN_READY`: production edits are hard-denied until the engine reaches an execution node, so nothing touches production code (including the agent's own config) before the user approves the plan.
- The **resolution gate** holds before `RESOLVED`: it requires explicit user confirmation that the task is resolved, never inferred from silence or thanks.

State for a session lives at `~/.claude-agent/agentctl/state/<session_id>.json`. The spine is **pluggable** — a skill can attach a per-session sub-state-machine.

Distinct from those two hook-enforced gates, the `DIAGNOSING`-closure path carries a family of **internal `replan` preconditions** — pure guardians deliberately absent from `GUARDIANS` (no hook), each blocking difficulty closure until its artifact is recorded: `difficulty_blockers` (the declare→investigate→critique cycle is complete), `normalization_blockers` (the reproducible factor is re-normed), and `failure_address_blockers` (the goal-failure is routed to a content-fault `сущее`, a form-fault `должное`, or explicit `not_applicable`) — see the engine README.

## Where the detail lives

This page is the architectural overview. Two surfaces carry the binding detail and are the canonical homes for it — they are linked here, not restated:

- The engine has its own README — [scripts/agentctl/README.md](../../scripts/agentctl/README.md) — the canon for the **engine internals**: the full command sequence, the `gates.py` purity invariant, the state file format, and the node-aware gate / plugin mechanism.
- The typed **plan model** the engine builds — the eight-element activity ontology each substantive plan must cover — is documented in [plan-activity-ontology.md](../../memory-global/leaves/plan-activity-ontology.md).

If the engine is unavailable, the manager walks the same steps by hand in the same order — the engine automates the spine, it does not replace the cognition.

## See also

- [The seven-layer model](layers.md) — the engine is Layer 3.
- [The consensus architecture](consensus-architecture.md) — how an evolving Core is distributed across a team.
