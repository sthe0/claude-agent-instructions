---
name: question-provenance-gate
description: The premise plugin gates plan_approval on question provenance — every planning question dispositioned and an enumeration cross-check run — the plan-time twin of the ledger's resolution-time claim gate; carries the honest ceiling INCLUDING the silent-advisor fail-open hole and the designed-in incentive gradient.
schema: leaf/v1
type: reference
created: 2026-07-20
last_verified: 2026-07-20
---

## Difficulty

An ungrounded planning leap — "the reporter says it reproduces", "the goal is agreed", "that path exists" — dissolves in confident prose and is never addressable by a reviewer: there is no NAMED record, bound to a NAMED plan element, carrying a NAMED source and an asserted inference. The ledger closes this on the *resolution* axis (every load-bearing claim in the deliverable grounded), but a plan is approved and executed long before resolution; a false premise baked in at plan-approval time has already cost the execution before any claim ledger runs. The plan_approval boundary needs its own provenance gate, and — like enumeration for the ledger — *which questions exist* is perception the engine must not regex-extract, while *whether the recorded set is structurally dispositioned* is a decidable form the engine owns.

## Guidance

**The mechanism.** The `premise` plugin is the first plugin keying the `plan_approval` core gate (the ledger/experience/tracker plugins key `resolution`). Its `auto_activate` predicate is `weight_class == SUBSTANTIVE` **alone** — unlike the ledger, it does *not* require `deliverable_kind in {reasoning, mixed}`, because a false premise harms a code plan exactly as much as a reasoning one, so every substantive plan is armed the moment `classify` routes it SUBSTANTIVE, with nobody hand-arming it. The `AGENTCTL_PREMISE` env seam force-on (`1`) / force-off (`0`) exists for tests and escape.

**Rule vs perception — the boundary, made explicit:**

| Perception (the coordinator's cognition, never mechanized) | Rule (the engine's, decidable from the bag) |
|---|---|
| *Which* questions a plan raises; which are load-bearing | Every recorded question is dispositioned (`open` never survives approve) |
| Whether a `--reason` / `basis` / `own_research` is honest or a decoy | An `escalated` question carries non-empty `own_research`; a `researched` one carries source+derivation; an `assumed` one carries basis+risk |
| Whether the derivation actually *follows* | A substantive Principle's derivation is not a restatement of the claim |
| Whether the advisor's read was real | The `enumerated` flag is set (the cross-check was RUN against current plan content) |

**One source, generated views.** Questions live once in the plugin bag; `question-list --format md` and `plan-render` are *generated read views*, never a second store to keep in sync. The thinker plan-review reads `question-list --format md` — that is where the perception layer catches what the structural gate cannot.

**Two-directional control norm.** The gate must both REFUSE (an open question / an escalated-with-empty-own_research / an un-run enumeration cross-check each block approve, surfaced as `[premise] …` blockers) AND ALLOW (once every question is dispositioned and the cross-check has run against the current plan digest). A gate that only ever refuses is untested on its allow path; the e2e proves both directions on a session nobody hand-armed.

## Honest ceiling — what is NOT guaranteed

Overselling this mechanism is the likeliest way for it to do harm. Guaranteed (fail-closed, decidable): over the questions ACTUALLY RECORDED, no `open` one survives approve; no `escalated` one has empty own_research; no stage-bound question is silently carried across a change to THAT stage's definition; every substantive Principle carries a non-restatement derivation; no plan is submitted whose control names a path nothing produces. NOT guaranteed:

- **RECALL < 100%.** A question neither the planner nor the advisor pass surfaces still escapes. The advisor *widens* recall; it does not close it. Inherited verbatim from the ledger's honest residual.
- **FABRICATION.** `own_research: "searched X, found nothing"` and a plausible-but-false `derivation` both clear the gate. The derivation field raises the COST and FALSIFIABILITY of the lie — a fabricated derivation from a REAL source tears on the first cross-check, and a reviewer can test the inference rather than only the citation's existence — but it does not detect it.
- **DECOY.** Recording three trivial questions while the load-bearing one goes unasked passes every structural check. Structure counts questions; it does not weigh them.
- **JUNK RETIRE / DISMISS.** `--reason` is free text and cannot be content-checked.
- **A SYSTEMATICALLY FAILING ADVISOR SILENTLY DISCHARGES THE CROSS-CHECK (F3b) — load-bearing.** The advisor is **fail-open** by module contract: a timeout, a missing binary, or a broken prompt all return `[]`. Under the chosen design the `enumerated` flag flips regardless of the count returned, so an advisor that fails on EVERY call still flips the flag on every call, the gate clears, and the mandatory second reading degrades into ceremony nobody notices has stopped happening. This is the price of fail-open, paid knowingly (the alternative wedges approve on an infra failure). MITIGATED, NOT CLOSED: `enumerated_runner_ok` and `enumerated_count` are recorded per run and surfaced non-blockingly, so a run of zeroes is VISIBLE to a reader — nothing forces anyone to read it. If this mechanism is ever audited, "did the advisor actually return anything, ever" is the first question to ask. This hole is load-bearing because the argument that this is not a second `External research:` token gate rests on the agent NOT controlling the question set (via `question-enumerate`) — and that argument is exactly as strong as the advisor is healthy.
- **PATH REACHABILITY, NOT green-reachability (stage 8).** The lint proves a control command names only existing paths; it cannot prove the command can exit 0 (be green). An aggregate runner exits 1 on any pre-existing red in its scope, most of which the stage does not own — exit-code reachability needs the command RUN, and submit-plan runs at approve time, before the artifacts exist. The exit-code half stays with the control author's perception.
- **REACHABILITY ≠ CONTROL-VALIDITY.** Proving a check's inputs exist proves it CAN run and CAN pass; it does not prove passing means anything. A check that greps for a string it also writes is reachable on both axes and worthless.

So gap 1 is NARROWED, not closed: an ungrounded leap previously dissolved in confident prose is now a NAMED record, bound to a NAMED element, carrying a NAMED source and an asserted inference — addressable by the thinker review that already runs. The perception layer remains the coordinator's own discipline.

## Incentive gradient (F7 — a designed-in gradient, not a perception ceiling)

`plan.goal` and `plan.done_criterion` targets are EXEMPT from the rebind check (by design: they bind to the order, not to plan bytes), and `question-raise` is deliberately permissive about targets. So an agent minimizing its own friction binds every question to `plan.goal` and never pays a rebind — a goal-bound question is well-formed, dispositionable, and cheap forever, and the gate cannot tell a question that genuinely concerns the order from one parked there to dodge maintenance. This is not a perception ceiling — it is a gradient the design itself creates. Knowingly accepted: (1) the alternative — inferring a question's TRUE target from its text — is exactly the perception the rule/perception table refuses to mechanize, and a wrong inference would misfile the record; (2) the F6 scoped-key fix SHRINKS it materially — under a whole-plan sha ANY edit invalidated EVERY stage-bound question, so the pressure to park on `plan.goal` was constant; scoped to the bound stage's own definition, a rebind is due only when that stage actually changed, so the gradient is a slope rather than a cliff. The countermeasure is the thinker review reading `question-list --format md` — perception, the right layer for it.

## See also

- [[formalization-ladder-l1-l3]] — the L1-L3 ladder and the ledger's resolution-axis claim gate; this premise gate is its plan-approval-axis twin, and the RECALL residual is inherited verbatim.
- `scripts/agentctl/README.md` § Plugins — the premise plugin registration, the seven `question-*` verbs, and `plan-render`.
