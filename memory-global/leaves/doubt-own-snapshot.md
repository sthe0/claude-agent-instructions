---
name: doubt-own-snapshot
description: When a user's stated requirement appears to contradict what you observe, first suspect your OWN source is stale or incomplete and refresh it before doubting the requirement or asking a clarifying question built on a false premise.
type: feedback
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-08-24
---

# Before you doubt a requirement, doubt your own snapshot

The short rule lives in CLAUDE.md § Escalation to the user; this leaf carries the full narrative.

## Difficulty

Challenging a **correct** requirement from an out-of-date local view wastes the user's attention and erodes trust; the apparent contradiction is far more often your staleness than the user's error.

## Guidance

When a user's stated requirement appears to contradict what you observe (a command / file / flag the user says exists that you don't see), first suspect your OWN source is stale or incomplete — `pull` / `fetch` / re-read the authoritative source (fresh state may live on another branch or machine) **before** doubting the requirement or asking a clarifying question built on a false premise ("X doesn't exist"). A stale local snapshot is not ground truth.

Critically evaluate every clarified requirement for adequacy **and** non-contradiction, but resolve a perceived contradiction to root — self-staleness included — before escalating.

The snapshot to refresh is not only your view of the stated order but the functional place behind it — a stale order can be literally accurate yet already fill the wrong position in its organizedness; see [[function-place-difficulty]] for reconstructing the function an order serves before optimizing it at face value.

### The planning direction: before planning potentially-already-done work, refresh the authoritative source

The same stale-snapshot difficulty has a *planning-side* twin. Before you plan work that another session, machine, or collaborator **could already have produced** — a merge, a hook deploy, a refactor, a migration — refresh the authoritative source (`git fetch origin` + re-read the live state / branch tips / deployed config) **before** committing the plan. Planning against a stale local view silently re-plans already-done work; the wasted effort surfaces only when the plan is executed or, worse, when someone checks the live state you should have checked first.

Concrete instance (2026-07-11): a whole plan plus **four** thinker-review rounds were spent against a `main` snapshot **19 commits stale**, on work (a "loops" feature) that had *already been merged and deployed* — discovered only when the user asked "сверься со свежим main". Personal memory was independently wrong too (it claimed the primary checkout sat on a feature branch when it was on `main`). Two separate stale snapshots, one avoidable `fetch` away from being caught at plan time. So: for any task whose result is the kind of thing that gets done once and shared, a fetch-and-re-read is a **plan-time precondition**, not a nicety.

### The outage direction: doubt your own probe before declaring a service down

The same difficulty has an *external-failure* twin. When a service appears to fail, your bare probe is the stale snapshot — a `curl`/one-shot call can fail for a dozen reasons that are not "the service is down" (wrong client, missing ambient context, expired token, a transient, the wrong endpoint). Before you declare it down or escalate the outage to the user, reproduce the failure with the **real client** the working path uses, and actively seek a **counter-example** (open the UI, try a second access path) — a genuine outage survives both, a false premise does not. Never launder the unverified premise into a sub-agent question ("the endpoint is down — how do I get access?"): the sub-agent inherits the premise and **circularly confirms** it. Route it through overcome-difficulty (reproduce → ≥2 hypotheses, each with a cheap falsifier) instead. Enforced pre-emptively by `hook-escalation-diagnosis-gate.py` (denies the un-diagnosed AskUserQuestion) and, as a Stop backstop, by the `escalation_without_diagnosis` turn guardian.

### The authoring direction: a plan's own claims about code/engine mechanics are a snapshot too

A plan stage that asserts a code-behavior fact, an engine-mechanic fact (which venue a control resolves against, how a gate discharges), or a real-world timing feasibility (a done_criterion resting on calendar time) is taking a **snapshot** of the live system at authoring time — and that snapshot goes stale (or was simply never taken) exactly like a stale `main` or a stale probe. The planner's existing "numbers and deadlines without a source" rule already forces this for bare numeric claims; the same trace-to-source discipline generalizes to code-behavior and engine-mechanic claims: before a stage's text commits to "X already does Y" or "the venue resolves to Z", grep/read the actual source (or, for a timing claim, derive the calendar math from data already in hand) rather than asserting it from memory or plausibility.

Concrete instance (2026-08-21, `spawn-outcome-typing` plan, session `fa80b9a9`): 3 replans fired against one plan between approvals — a plan clause asserting unverified existing-code behavior, a done_criterion binding a multi-week real-world statistical measurement to one delivery session, and a stage assuming `verify_venue` would resolve to the delivery worktree when it is engine-hardcoded to the canonical checkout. All three were checkable from already-existing source/data *at planning time* (the code was readable, the engine mechanic was readable in `agentctl` source, the spawn-rate-derived timeline was computable from data already in hand) — none depended on runtime-only state. This tripped the engine's own effort-divergence trigger (`effort-replan-absolute` = 3) as a "chosen norm visibly missing something essential" signal, which is exactly what it is for.

### The currency direction: an artifact checked against its own backup is not verified

Comparing a **mutable** artifact against its own copy or backup establishes **integrity** — the bytes are intact and the two agree — and says nothing whatever about **currency**, i.e. whether the artifact is still being written. The two properties are independent, so verifying a mutable artifact must include a currency check — the newest record's timestamp read against the calendar — and not only internal consistency. The backup is a second snapshot of the same source, so it inherits that source's staleness exactly; it is not an independent witness.

Concrete instance (2026-07-27): `~/.local/log/claude-policy-ledger.jsonl` had been frozen for 25 days — 184 rows, newest row dated 2026-07-03 — because the scanner that appends to it only ever ran by hand. It was checked during an unrelated incident by comparing it against its own backup: same row count, same sums, structurally valid JSONL. Every check it received passed **while it was broken**, because each one tested integrity. No integrity check could have caught this; only "the newest row is 25 days old" could.

The same failure appears one level up, in the guards a plan writes: a control that pins a base state by a **literal commit SHA** is a snapshot of that base and goes stale the moment the base moves. Name the base by a **derived** ref instead — `git merge-base HEAD origin/main`, the branch's fork point — which stays correct as the trunk advances. In this leaf's own delivery the plan-time SHA went stale twice before execution and a third time between approval and the first stage.

### The reporting direction: a closure claim in a reply is a snapshot too

Telling the user a task/ticket is "closed", "resolved", or "done" is itself a claim about live system state, not a summary of the conversation you remember. When an `agentctl`-tracked session exists, that claim has an authoritative source — `agentctl status --session <id>` and its `resolution_passed` + per-stage fields — and conversation memory is exactly the kind of snapshot this leaf warns about: it goes stale fastest right after a context-compaction resume, when only a summary of prior work survives and the boundary between "a narrow sub-thread finished" and "the whole tracked task finished" blurs. Before any sentence asserting closure/resolution, check `agentctl status` and match the claim to what it reports — never assert closure from memory alone.

Concrete instance (2026-08-24, session `7514dd40-b947-4cc5-84aa-983476c2515c`): right after a compaction resume, a landed instructions-repo edit — unrelated to the tracked task's actual deliverable — was described to the user as "the task was closed earlier in this session," conflating a narrow sub-thread this session had actually handled (attaching a comparison artifact, fixing a stale-path citation) with the task's real done criterion, a model migration still in progress. `agentctl status` showed `resolution_passed: false`, with three of six plan stages (deploy to a shared pre-production environment, measure, land to trunk) still `ACTIVE`/`PENDING`. The user caught it with one question, in effect: how is this closed when the migration itself never happened?

## See also

- `~/.claude-agent/CLAUDE.md` § Escalation to the user — the short pointer that loads this leaf.
- [[mirror-working-caller-before-bypass]] — the same "use the real working path, not a bypass" instinct on the ambient-context axis.
- [[capability-before-offload]] — the acting-side twin: doubt your own claim of *"can't"*, not the user's expectation that you can.
