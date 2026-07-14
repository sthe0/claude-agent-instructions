---
name: overcome-difficulty
description: TRIGGER when the actual result of the plan or one of its stages diverges from the expected result — verification failed, blocker, repeated error, surprising output, plan mismatch, two or more process corrections in a row, long shell loops without progress, same root-cause narrative repeated without new evidence, or the first failure in an external orchestrated job (Nirvana WI, CI launch, Reactor) before chaotic retries. Work through declaration → investigation → critique to localize the moment of divergence and derive a concrete replanning task that the root coordinator then applies to fix the plan. SKIP when work is progressing as expected, or a one-off retry will obviously succeed without further analysis.
---

# Overcome difficulty

## What counts as a difficulty

A **difficulty** is a divergence between reality and the plan. Two forms qualify:

1. **Result mismatch (canonical):** the actual result of a step does not match the result image the plan declared for that step.
2. **Verification gap:** you cannot perform that check at all — no observable, no signal, no measurement, no way to compare actual against expected. Inability to analyze conformance is itself a difficulty.

Surface signals (verification failed, blocker, repeated error, surprising output, plan mismatch, two or more process corrections in a row, long shell loops without progress, "I don't even know how to tell if this worked", **same root-cause narrative repeated across investigation iterations without new evidence**, **retrying an external workflow / VCS / mount / CLI after failure without a fresh declaration**, **first external job failure in an orchestrated pipeline** — child WI `failure`/`cancel`, CI launch red, unexpected block status) are all manifestations of one of these two underlying forms.

**The plan can be the task plan or the agent system itself.** Instructions (`CLAUDE.md`, memory, skills, hooks) are the persistent multi-session plan you work by; the same Expected/Actual/Mismatch frame applies when behavior across sessions diverges from what the instructions intended. See `memory-global/leaves/systemic-pattern-scan.md` for the resolution-time entry point that surfaces these.

When that plan is the **agent system** and the fault is `нормативное` (a norm — instruction / gate / habit — is what failed), the specialization that owns this cycle is **`self-improvement`**: its two beats map onto declaration → investigation → critique → normalization, closing with a re-norming edit to the instructions. This skill is the general case it specializes.

## Proactive self-diagnosis (standing obligation)

Invoking this skill is not gated on a user-visible failure. Material = the norm itself (see `reflexive-exit-is-base-activity-figure.md`): the agent's own memory and instructions are a plan, so a mechanically-decidable divergence in them — an oversized memory index, a dangling memory pointer, an instruction file near its ceiling — is already a Declaration waiting to be read, not something to wait for the user to notice. `scripts/self-diagnose.py` determinizes exactly that decidable half; `hook-self-diagnose-due.py` runs it once per session (throttled, fail-open) and prints any worklist. A non-empty worklist is worked through the same declare → investigate → critique cycle as any other difficulty, with the resulting re-norming edit authored through `self-improvement`.

## What this skill does

You are invoking this skill because either of the above happened. There are three phases — **declaration**, **investigation**, **critique** — each ending in a concrete replanning task you (the root coordinator) then apply, resuming the original user task on the corrected plan.

**The engine owns the sequencing; this skill owns the cognition.** When `agentctl` is driving the session, a failed stage (`record-result --status failed`) routes to the `DIAGNOSING` node and the engine enforces the order and that each phase recorded its artifact: `declare` → `investigate` → `critique`, with `replan` machine-blocked until all three are present (`gates.difficulty_blockers`). So you do not police the ordering yourself — the engine does. What this skill supplies is the *content* of each phase: what the divergence actually is, the ≥2 competing hypotheses, the functional-ground critique, and the recursive escape when the thread won't converge. Each phase below names the command that records its artifact; if the engine is not driving this session, produce the same three artifacts in-thread.

## 1. Declaration

State, here in the conversation, three things — at the **global** level, the way the plan as a whole framed them:

- **Expected:** the image of done the plan (or the current stage) aimed at — artifact, observation, status, measurement.
- **Actual:** what was actually produced — output, error, partial result, missing artifact.
- **Mismatch:** one or two sentences naming the gap between expected and actual.

Do not yet try to explain the cause. Cause is what investigation will find. Declaration only fixes that there **is** a divergence and frames it.

Record it: `agentctl declare --session <id> --expected … --actual … --mismatch …` (the engine refuses `investigate`/`critique`/`replan` until this exists).

## 2. Investigation

Goal: localize the moment of divergence as precisely as possible. The moment can sit **in the middle of a stage**, not at a stage boundary. You are looking for the **most local** expectation that did not match the **most local** actual result.

Compare in order:

| What to compare | Question |
|---|---|
| **Process** | Which steps ran, in what order, what was skipped, what was added? At which point did execution stop matching the plan? |
| **Means** | Which agents / skills / commands / MCP were used versus what the plan named? Did any substitution coincide with the divergence? |
| **Results** | Stage outputs vs expected artifacts. Which intermediate output was the first one to fail expectation? |
| **Reference baseline** | Is there a known-good run (same workflow / flow, comparable params)? At **block order**, what differs from the failing run? (See `memory-global/leaves/workflow-debug-investigation.md` § Reference baseline.) |
| **Topology / causality** | Parent/child WI, block completion order, Stop/Start/shared instance ids — is the reported failure block the **root** cause or downstream of orchestration? |
| **Code delta** | For ticket-scoped work — `arc diff` or PR diff on code paths for failing block names **before** deep infra logs. |
| **Prior sessions** | What was already attempted on the same task — read the relevant session transcript (`~/.claude/projects/<cwd-hash>/<uuid>.jsonl` or Cursor `agent-transcripts/`) for launch parameters, working commands, branches already rejected. Avoid blind repeats. |


For orchestrated pipelines (Nirvana, Arcadia CI, Reactor, multi-stage Sandbox), follow the ordered checklist in **`memory-global/leaves/workflow-debug-investigation.md`** — baseline → topology → code delta → infra logs last. Project-specific pipeline signals may live in `<project_cwd>/.claude/agent-memory/leaves/overcome-difficulty-signals-pipelines.md`.

### Hypothesis portfolio

Before committing to a fix, maintain **at least two** competing hypotheses. For each:

- **Hypothesis** — one sentence.
- **Would confirm** — observation that supports it.
- **Would falsify** — checkable observation that kills it (prefer ≤3 tool calls).

Do not anchor on the loudest symptom (timeout, OOM, `cancel`, "model never started") until baseline and topology passes falsify simpler orchestration/code explanations. Example table shape: `workflow-debug-investigation.md` § Hypothesis portfolio.

If the reasoning gets non-trivial — `Task → thinker` with the localized fact + expectation to verify logic, not to replace investigation.

Output of this phase: a localized expectation and a localized actual result, in the most narrow form you can produce. If you cannot localize beyond "the whole stage was wrong", the investigation is not done — keep narrowing.

Record it: `agentctl investigate --session <id> --localized-expectation … --localized-actual …`.

You may legitimately conclude: the **plan itself** was the problem — incomplete, wrong dependencies, wrong executor, unrealistic estimate. Name this explicitly; do not pin the failure only on "bad execution" when the means were misassigned.

## 3. Critique

Formulate the **essence** of the local mismatch — split it cleanly into two parts:

| | structured as | role on replan |
|---|---|---|
| **What matched expectation** | `--invariant-to-preserve` (repeatable) | each must reappear as a stage **condition/invariant** in the corrected plan |
| **What did not match** | `--difference-to-remove` (repeatable) | their presence requires a **changed means/method** in the corrected plan |

State concretely as `local fact → local expectation → local gap`. The split is recorded **structurally** (not just prose): the engine verifies *coverage* on `replan` — every similarity you name must be carried into some stage's conditions/invariants, and naming any difference forces a means/method to actually change. It does **not** author the item→stage mapping (that is your cognition); it only checks the dataflow lands.

Before concluding a difficulty is intractable or that the only options are costly, restate the **functional ground** (the underlying desired-vs-actual, stripped of any framed mechanism) and enumerate the **simplest primitive** that removes it — especially one already demonstrated in code/artifacts you've read. Mechanism-fixation (solving "can API X pin the path?" instead of "how do I get real data to a controlled path?") is a common way investigation declares a false dead-end.

<!-- Language exception: ресурсное/нормативное/обеспечение/знание-о-материале/целеполагание are the settled SMD source-ontology terms named here; preserved verbatim for traceability. -->
**Route the fault — `--failure-address` (which обеспечение was inadequate).** A затруднение is overcome by fixing its **обеспечение**, and the critique must decide which kind proved inadequate: **ресурсное обеспечение** (`ресурсное`) — the материал/средство, your model of what you were transforming, was wrong; or **нормативное обеспечение** (`нормативное`) — the норма/способ, the целеполагание itself, was wrong (the goal, or the norm you set to reach it, was inadequate). «Норма — тоже ресурс»: these are two special cases of ONE act, both reducing reflexively to знание — NOT an is/ought (`сущее`/`должное`) tag (that v3 typing was rejected, ADR-0004 §R2). Where routing genuinely does not apply, say so explicitly with `not_applicable`; do not leave it unset, as the closure gate (`gates.failure_address_blockers`) blocks `replan` on a bare omission so the routing is DECIDED rather than silently skipped.

Then derive a **replanning task** for the root coordinator. The task must name:

- Which stages of the current plan are affected (refine / rebuild / drop / merge / insert).
- New done criteria for each affected stage.
- Updated means and resources per affected stage — ready / obtain via task / ask user — and new executors if means change.

End with one explicit sentence: **"Replanning task for the root: …"**.

Record it: `agentctl critique --session <id> --functional-ground … --replanning-task … [--invariant-to-preserve …]… [--difference-to-remove …]… [--failure-address ресурсное|нормативное|not_applicable]`. This completes the diagnosis; one closure act remains before `replan`.

## 4. Normalization — re-norming the reproducible factor

A difficulty is a **norm-failure**: a governing norm — an instruction, a gate, a habit, a plan assumption — failed to hold. That failure is a **SIGNAL**, not the fix. Because an activity is constituted by its *reproduction*, a **reproducible** factor left un-normed simply re-fails on the next run. So the closure **ACT** is **re-norming**: update the norm so the same factor cannot recur — tighten an instruction, add/adjust a gate, change a habit, or at minimum record a deliberate note. In SMD/MMK terms, mapped onto the cycle above:

<!-- Language exception: SMD/MMK source terms name the signal→act pair precisely. -->
- **провал нормы (norm-failure) = SIGNAL** — surfaced by declaration → investigation → critique;
- **перенормирование (renorming) = ACT** — this phase, which the replanning task then carries into the corrected plan on `replan`.

The **ACT is mandatory whenever the factor is reproducible**; the recording **LEVEL** (in-head note / memory leaf / principle) is payoff-gated by `rediscovery-threshold-min` — that split is the [[recording-experience]] `normalize-if-reproducible` rule. A factor that is genuinely one-off (will not recur) is the only case that skips the record.

Record it: `agentctl normalize --session <id> --factor <the reproducible cause> [--level note|leaf|principle]`. The engine blocks `replan` at DIAGNOSING closure until this exists, **or** you take the explicit one-off escape `agentctl replan … --normalization-waiver <reason>` (`gates.normalization_blockers`). This unblocks `replan`.

## Handoff back to the root

The replanning task you just wrote is now the input for the root coordinator (you, in the main thread). **Before calling `agentctl replan`, a thinker review is mandatory for every replan — refinement or substantive, no exceptions.** Spawn `Task → thinker` with the critique's causal reasoning (functional ground, invariants-to-preserve, differences-to-remove) and the corrected plan, to check both the critique's reasoning and the corrected plan's adequacy/internal-consistency. Record the verdict with `agentctl plan-review --verdict pass|revise|override --reviewer thinker --target <corrected-plan> [--concern …]… [--note …]` — `replan` is engine-blocked (`gates.plan_review_blockers`) until this exists bound to the exact corrected-plan path; only an explicit user-authored `override` bypasses a `revise` verdict.

Apply the replan with `agentctl replan --session <id> --plan <corrected-plan>`: a *refinement* re-arms the failed stage and returns to execution; a *substantive* change re-arms the plan-approval hard gate (PLAN_READY) for re-approval. Either way the engine clears the difficulty record on exit, so a later failure starts a fresh cycle. The corrected plan **replaces** the prior plan, and you continue solving the **original** user task on it.

Verification after the next round of action loops back to declaration if a new divergence appears.

## Escape to a fresh context (recursive)

Use this when working through declaration → investigation → critique in the current thread is **not converging**: the same difficulty keeps re-appearing in different costumes, plan-rework iterations chase their tail, you can feel the parent thread is anchored on a wrong frame, or accumulated context noise is more burden than help.

Mechanism: spawn a **fresh manager** in a separate Claude Code process via `claude -p` — **no `--agent` flag** and **no `--append-system-prompt-file`**. That keeps the full Claude Code built-in system prompt, the same `CLAUDE.md`, the same memory, the same skills, the same subagent infrastructure; only the conversation history is empty.

The escape is **always a vanilla manager**, never a specialization-preloaded process — even when the difficulty arose inside a specialist (e.g. inside a developer `claude -p` process). The whole point of the escape is a fresh perspective without anchors. If the fresh manager determines it needs a specific specialist to resolve the sub-difficulty, it will spawn one itself via the standard spawn template (per its own freshly-built plan).

Each spawn can itself spawn another (overcome-difficulty escape can recurse) up to the depth cap defined below.

### Invocation

Before spawning, verify the would-be `AGENT_RECURSION_DEPTH` does not exceed `max-recursion-depth` (see `~/.claude-agent/config.md`) — if it would, follow the hard depth cap below and do not spawn. Choose the budget tier per `CLAUDE.md` § Budget tier — `budget-medium-usd` is the default for overcome-difficulty escapes; `budget-large-usd` only when the difficulty likely needs deep exploration. The `claude -p` bash template (with the `AGENT_RECURSION_DEPTH` increment + the self-contained escape prompt) and how it works: [policy.md](policy.md) § Invocation.

### Reading the result

The child returns one marker on stdout: **`RESOLVED:`** (apply + continue), **`INVESTIGATION:`** (incorporate findings; retry inline / escalate / accept partial), **`LOOP_DETECTED:`** (stop, summarize, ask the user — do not re-spawn on the same difficulty); a budget-cap exit without a marker is treated as `INVESTIGATION:`. Detail: [policy.md](policy.md) § Reading the result.

### Safeguards

- **Hard depth cap.** Before spawning, check `$AGENT_RECURSION_DEPTH`. If the spawn would push it **above** `max-recursion-depth` (see `~/.claude-agent/config.md`), **do not spawn**. Instead: stop, summarize the chain for the user (original task, where the recursion is now, what the next spawn would have done), ask whether to continue manually, restart with a clean approach, or accept a partial result. This cap is shared with the global one in `CLAUDE.md` § Recursion cap.
- Per-level budget, visible depth, loop sensitivity at depth ≥ `loop-sensitivity-depth`, persisted transcripts: [policy.md](policy.md) § Safeguards.

### When NOT to escalate

- One more inline retry would obviously succeed (transient error, missed flag).
- The difficulty is a simple lookup — `grep`, `git log`, `ls` — no need for a fresh context.
- Asking the user a clarifying question would be cheaper than recursion.
- You are at the cap (`AGENT_RECURSION_DEPTH` equals `max-recursion-depth`) — escalate to the user instead.
- A previous level returned `LOOP_DETECTED:` — escalate to the user, do not re-spawn on the same difficulty.

Inline overcome-difficulty (the phases above) is the default. Spawn only when the current thread genuinely won't converge.

### Cursor (use spawn-cursor-escape.py)

In **Cursor**, do **not** invoke the `claude` CLI for recursive escape (global hard gate) — use `~/claude-agent-instructions/scripts/spawn-cursor-escape.py` after **two** full inline cycles without convergence. The wrapper invocation, flags, and result handling: [policy.md](policy.md) § Cursor (use spawn-cursor-escape.py).
