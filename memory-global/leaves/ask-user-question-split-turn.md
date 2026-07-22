---
name: ask-user-question-split-turn
description: The plan-presentation delivery gate — the essence rendering must land as a completed turn's final message bound to plan_sha256 before cmd_approve; hook-plan-delivery-gate.py enforcement, its two-prover split, and two residuals. (The former universal AskUserQuestion turn-split is retired — the client render bug it worked around is fixed on 2.1.216.)
schema: leaf/v1
type: feedback
created: 2026-07-14
last_verified: 2026-07-21
---

## Difficulty

Plan approval must prove an *act of presentation* actually occurred: the plan's essence rendering has to reach the user as a completed turn's **final** message, bound to the exact plan version, before `cmd_approve` may pass. Delivery is observable ONLY by a hook — agentctl never sees a transcript — so the approval gate needs a hook to PRODUCE the proof and `cmd_approve` to CONSUME it, or a plan can be "approved" with nothing shown.

> **Historical note (2026-07-21).** This leaf formerly also carried a *universal* AskUserQuestion turn-split: on clients up to v2.1.211, same-turn text preceding a tool call could be dropped from render and transcript, so every confirmation ask had to be deferred to its own turn behind a `sleep 2` timer, enforced by dedicated turn-boundary and answer-delivery hooks. That client behavior was **re-measured false on client 2.1.216** (2026-07-21) — text preceding a same-turn tool call now renders and persists — so the universal split and those enforcement hooks were **retired**; see [claude-code-drops-pre-tool-call-text.md](system-knowledge/claude-code-drops-pre-tool-call-text.md) for the forensic history. An ask may now share its turn with preceding text and tool calls. What remains live — and what this leaf now documents — is the **plan-presentation delivery gate**, which is grounded on receipt-binding to the plan version, not on any client render behavior.

## Guidance

**The plan-presentation two-turn flow is still required** — but for a structural reason, not a render bug. The delivery gate can only observe the essence as a terminal block of a *completed* turn, so the approval ask must fall in a separate, later turn: register the essence (`present-plan --kind essence`), arm a `sleep 2` timer in that same turn, emit the essence as that turn's **final** message, and open the next turn directly with the approval `AskUserQuestion`. Receipt-binding to `plan_sha256` is what forces the split.

### Machine enforcement — what the delivery gate actually checks (and what it does not)

- **Delivery check** (`hook-plan-delivery-gate.py` + the `PlanPresentation` receipt): at `PLAN_READY`, the registered essence rendering must have landed as a COMPLETED turn's **FINAL** assistant message — terminal at BLOCK granularity, strictly after the receipt that registered it — bound to the current `plan_sha256`. On positive verification, and **only** there, the hook stamps a `source=hook` delivery receipt; `cmd_approve` requires that stamp. This is a DELIVERY guarantee, grounded on the receipt binding to the plan version, not on any client render behavior.
- **NOT machine-checked (perception):** that the rendering faithfully renders the plan, and that the essence is genuinely self-contained. tech-writer authors these; the thinker's plan-review checks them. Describing either as enforced is a defect — do not repeat it.

**What the two-prover split DOES close** (least obvious, easiest to dismantle by accident): delivery is observable ONLY by the hook — only hooks receive a `transcript_path`; agentctl never sees a transcript. So the hook PRODUCES the proof (a stamp bound to the plan version + rendering) and `cmd_approve` CONSUMES it (`plan_presentation_blockers` requires both a bound receipt and a bound stamp). Therefore `present-plan` → never emit → `approve --by user` FAILS: no ask fires, so the hook never runs, so nothing stamps, so approve refuses. Two properties hold it up, both easy to erode: (i) **ALLOW != VERIFIED** — the hook stamps only on positive verification, never on a fail-open ALLOW, or it would manufacture the proof it exists to demand; (ii) the only remaining path to approval-without-delivery is the explicit, audit-logged, per-plan-version `confirm-delivery --by --note` escape (it exists because a refusal with no reachable exit trains bypasses — experience leaf `2026-07-09-gate-must-execute-what-it-attests`). An escape used routinely rather than exceptionally is the signal to fix the hook, never to widen the escape.

**Two residuals the mechanism does NOT close — real limits, but not silent ones:**
1. **The essence has no lower bound.** `full` renderings get anchor-completeness checking; `essence` gets none, correctly — a summary is a summary. So registering "план готов" as the essence, emitting it, and asking for approval passes every machine check. The gate proves that AN ACT OF PRESENTATION OCCURRED and those exact bytes landed, bound to this plan version — **not** that anything meaningful was shown. It raises the floor from "nothing" to "whatever you registered"; that is a real gain and is not "the user saw the plan". Adequacy of the essence is perception (tech-writer authors, thinker plan-review checks).
2. **Delivery is not reading.** Terminal position in a completed turn proves the harness rendered the bytes, not that the user read them. That is the ceiling of any mechanical check available here.

## See also

- `CLAUDE.md` § Escalation to the user — the `AskUserQuestion`-mandatory mandate and the "unanswered question survives the turn" norm.
- [acting-without-asking.md](acting-without-asking.md) § Approved plan — the canonical two-acts plan-approval definition this gate enforces.
- [outcome-format.md](outcome-format.md) — the main-first shape of a final-message artifact.
