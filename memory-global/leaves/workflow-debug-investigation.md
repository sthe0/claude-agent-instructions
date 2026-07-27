---
name: workflow-debug-investigation
description: Investigation checklist when a long-running orchestrated workflow fails — baseline run, block topology, code delta, then infra logs
type: reference
created: 2026-06-03
last_verified: 2026-07-09
---

# Workflow debug investigation

Use during **overcome-difficulty § Investigation** when the failure is in an orchestrated pipeline: workflow-orchestration-platform runs, CI launches, reaction-pipeline runs, migration processes, multi-stage build graphs.

**Order matters.** Do not jump to infra logs (YT stderr, pod logs, Monium) until the first three passes are done — anchoring on the loudest symptom (timeout, OOM, cancel) without topology is a recurring failure mode.

## Checklist (in order)

### 1. Reference baseline

| Step | Action |
|------|--------|
| Find known-good run | Same workflow / flow id, comparable parameters (checkpoint, baskets, env). Project memory may name one; else search recent successful WIs or CI launches. |
| Compare at block level | Which blocks succeeded/failed in baseline vs failing run? Same block names and progress points? |
| Terminal status in context | `cancel` on a child WI may be **normal cleanup** (e.g. stop launcher after eval) — compare whether baseline had the same pattern at a similar progress. |

**Falsifier:** If baseline shows the same terminal status at the same graph position and baseline succeeded, the status alone is not the root cause.

### 2. Topology / causality

| Step | Action |
|------|--------|
| Block completion order | List blocks that reached `success` / `failure` / `cancel` **before** the reported failure block. |
| Dependencies | Which block outputs feed the failing block? Shared workflow instance ids between blocks? |
| Failure block ≠ root cause | Meta graph may fail because a **child** WI failed; child may fail because an **earlier sibling** (Stop vs Start) changed shared state. |

**Falsifier:** If block A succeeded and block B failed, but B depends on state that A's successor C modified — root cause is orchestration order, not B's infra.

### 3. Code delta

| Step | Action |
|------|--------|
| Ticket scope | If debugging follows a branch/ticket — a VCS diff (or PR diff) on code paths for the failing block names **before** deep infra dives. |
| Behavior change | Did a refactor change which terminal statuses raise (e.g. treat `cancel` as failure when trunk only raised on `failure`)? |
| Branch freshness | Before writing a fix to match the branch's **current** state, diff the failing artifact against trunk via the VCS (fetch trunk, then show trunk's copy of the path). A branch forked from / not yet rebased onto current trunk may be **missing** what trunk already has (a baked model, a config, a dep). "X expected but missing on the branch" → first hypothesis is *branch is stale*, not *X's consumer is wrong*. |

**Falsifier:** If diff explains the mismatch between baseline and failing behavior, fix code/graph — not GPU quota. If trunk's version of the artifact already contains the missing piece, the fix is a rebase / cherry-pick of trunk — not a code change aligning the consumer to the branch's stale state.

### 4. Infra logs (last)

Only after 1–3: data-warehouse job stderr, orchestration-platform block logs, cluster ops, launcher health, backend metrics — scoped to the **localized** failing step from investigation, not the whole chain.

#### Reading a failed orchestration-platform block's stderr

Prefer the platform's own log-fetch tool/skill over hand-rolling a client library — check whether a composite block returns a **nested** workflow-instance id and use that, not the top-level one, before pulling logs. If the platform's dedicated tools aren't connected this session, don't grind a low-level API client whose log-fetch call returns an empty result for failed blocks — use the platform's raw log HTTP endpoint directly instead. Project-specific tool names, skill names, and endpoint URLs for this recipe: see project `overcome-difficulty-signals-pipelines.md` / project memory when present.

> verified by: a past pipeline debugging incident — repeated low-level API client calls returned empty/non-serializable results and produced two wrong "infra" conclusions; the platform's raw log endpoint returned the full traceback in one call, and the root cause was on stderr line 1.

## Hypothesis portfolio (required)

**Scope: any failure investigation** — this section is not confined to orchestrated pipelines. It applies equally to local processes, network faults, and client hangs. (The ordered checklist above is pipeline-specific; this rule is not.)

Maintain **at least two** competing hypotheses until one is falsified. For each:

- **Hypothesis** — one sentence.
- **Would confirm** — observation that supports it.
- **Would falsify** — observation that kills it (must be checkable in ≤3 tool calls when possible).

Example (a past ticket):

| Hypothesis | Falsifier |
|------------|-----------|
| Model failed to start (health timeout) | Eval blocks never reached; balancer never registered |
| Stop block cancelled launcher while Start still polls same WI | Stop LLM success + Start LLM failure; success baseline also had launcher cancel after eval |

## When to read this leaf

- overcome-difficulty Investigation table row **Reference baseline** / **Topology** / **Code delta** points here.
- Project-specific pipeline signals: see project `overcome-difficulty-signals-pipelines.md` when present.

> verified by: a past post-mortem (2026-06-03) — cancel on launcher was normal in success run; root cause was a Stop→Start race on the shared launcher workflow instance.
