---
name: overcome-difficulty
description: TRIGGER when the actual result of the plan or one of its stages diverges from the expected result — verification failed, blocker, repeated error, surprising output, plan mismatch, two or more process corrections in a row, long shell loops without progress. Work through declaration → investigation → critique to localize the moment of divergence and derive a concrete replanning task that the root coordinator then applies to fix the plan. SKIP when work is progressing as expected, or a one-off retry will obviously succeed without further analysis.
---

# Overcome difficulty

## What counts as a difficulty

A **difficulty** is a divergence between reality and the plan. Two forms qualify:

1. **Result mismatch (canonical):** the actual result of a step does not match the result image the plan declared for that step.
2. **Verification gap:** you cannot perform that check at all — no observable, no signal, no measurement, no way to compare actual against expected. Inability to analyze conformance is itself a difficulty.

Surface signals (verification failed, blocker, repeated error, surprising output, plan mismatch, two or more process corrections in a row, long shell loops without progress, "I don't even know how to tell if this worked") are all manifestations of one of these two underlying forms.

**The plan can be the task plan or the agent system itself.** Instructions (`CLAUDE.md`, memory, skills, hooks) are the persistent multi-session plan you work by; the same Expected/Actual/Mismatch frame applies when behavior across sessions diverges from what the instructions intended. See `memory-global/leaves/systemic-pattern-scan.md` for the resolution-time entry point that surfaces these.

## What this skill does

You are invoking this skill because either of the above happened. Work through three phases in order: **declaration**, **investigation**, **critique**. At the end, hand a concrete replanning task back to yourself — you are the root coordinator — and resume the original user task on the corrected plan.

Do not start the next round of execution until the corrected plan addresses the localized gap.

## 1. Declaration

State, here in the conversation, three things — at the **global** level, the way the plan as a whole framed them:

- **Expected:** the image of done the plan (or the current stage) aimed at — artifact, observation, status, measurement.
- **Actual:** what was actually produced — output, error, partial result, missing artifact.
- **Mismatch:** one or two sentences naming the gap between expected and actual.

Do not yet try to explain the cause. Cause is what investigation will find. Declaration only fixes that there **is** a divergence and frames it.

## 2. Investigation

Goal: localize the moment of divergence as precisely as possible. The moment can sit **in the middle of a stage**, not at a stage boundary. You are looking for the **most local** expectation that did not match the **most local** actual result.

Compare in order:

| What to compare | Question |
|---|---|
| **Process** | Which steps ran, in what order, what was skipped, what was added? At which point did execution stop matching the plan? |
| **Means** | Which agents / skills / commands / MCP were used versus what the plan named? Did any substitution coincide with the divergence? |
| **Results** | Stage outputs vs expected artifacts. Which intermediate output was the first one to fail expectation? |
| **Prior sessions** | What was already attempted on the same task — read the relevant session transcript (`~/.claude/projects/<cwd-hash>/<uuid>.jsonl`) for launch parameters, working commands, branches already rejected. Avoid blind repeats. |

If the reasoning gets non-trivial — `Task → thinker` with the localized fact + expectation to verify logic, not to replace investigation.

Output of this phase: a localized expectation and a localized actual result, in the most narrow form you can produce. If you cannot localize beyond "the whole stage was wrong", the investigation is not done — keep narrowing.

You may legitimately conclude: the **plan itself** was the problem — incomplete, wrong dependencies, wrong executor, unrealistic estimate. Name this explicitly; do not pin the failure only on "bad execution" when the means were misassigned.

## 3. Critique

Formulate the **essence** of the local mismatch — split it cleanly into two parts:

| | |
|---|---|
| **What matched expectation** | Inside the local result, what did happen as planned |
| **What did not match** | What did not happen, happened differently, or arrived with unacceptable quality/timeline |

State concretely as `local fact → local expectation → local gap`.

Then derive a **replanning task** for the root coordinator. The task must name:

- Which stages of the current plan are affected (refine / rebuild / drop / merge / insert).
- New done criteria for each affected stage.
- Updated means and resources per affected stage — ready / obtain via task / ask user — and new executors if means change.

End with one explicit sentence: **"Replanning task for the root: …"**.

## Handoff back to the root

End the skill here. The replanning task you just wrote is now the input for the root coordinator (you, in the main thread). The root takes that task, produces a corrected plan that **replaces** the prior plan, and continues solving the **original** user task on the new plan.

Verification after the next round of action loops back to declaration if a new divergence appears.

## Escape to a fresh context (recursive)

Use this when working through declaration → investigation → critique in the current thread is **not converging**: the same difficulty keeps re-appearing in different costumes, plan-rework iterations chase their tail, you can feel the parent thread is anchored on a wrong frame, or accumulated context noise is more burden than help.

Mechanism: spawn a **fresh manager** in a separate Claude Code process via `claude -p` — **no `--agent` flag** and **no `--append-system-prompt-file`**. That keeps the full Claude Code built-in system prompt, the same `CLAUDE.md`, the same memory, the same skills, the same subagent infrastructure; only the conversation history is empty.

The escape is **always a vanilla manager**, never a specialization-preloaded process — even when the difficulty arose inside a specialist (e.g. inside a developer `claude -p` process). The whole point of the escape is a fresh perspective without anchors. If the fresh manager determines it needs a specific specialist to resolve the sub-difficulty, it will spawn one itself via the standard spawn template (per its own freshly-built plan).

Each spawn can itself spawn another (overcome-difficulty escape can recurse) up to the depth cap defined below.

### Invocation

Before spawning, verify the would-be `AGENT_RECURSION_DEPTH` does not exceed `max-recursion-depth` (see `~/.claude/config.md`). If it would, follow § Safeguards § Hard depth cap and do not spawn.

Choose the budget tier per `CLAUDE.md` § Budget tier — `budget-medium-usd` is the default for overcome-difficulty escapes; use `budget-large-usd` only when the difficulty likely needs deep exploration.

```bash
# --max-budget-usd resolves to budget-medium-usd by default (~/.claude/config.md).
AGENT_RECURSION_DEPTH=$(( ${AGENT_RECURSION_DEPTH:-0} + 1 )) \
claude -p \
  --max-budget-usd 3.00 \
  --output-format text \
  "AGENT_RECURSION_DEPTH=$AGENT_RECURSION_DEPTH

You have been spawned as a fresh root coordinator to resolve a difficulty in isolation from any parent conversation. There is no prior history; treat the description below as a self-contained task.

Difficulty (in declaration form):
- Expected: <what the plan declared the result should be>
- Actual: <what actually happened>
- Mismatch: <one or two sentences naming the gap>

What has been tried so far (concise; do not retry blindly):
<bulleted list of approaches and what failed about each>

What you are asked to do:
1. Work through overcome-difficulty (declaration → investigation → critique).
2. Resolve the difficulty if you can.
3. If you yourself hit an unyielding sub-difficulty, escalate with the same mechanism (this prompt template, AGENT_RECURSION_DEPTH+1).

Reply with one of these exact markers on the first non-empty line of your final output:
- RESOLVED: <one paragraph resolution + concrete next action for the caller>
- INVESTIGATION: <findings + what you would try next, if you investigated but could not resolve>
- LOOP_DETECTED: <how this task mirrors an ancestor's task you noticed, if AGENT_RECURSION_DEPTH is at or above loop-sensitivity-depth (see ~/.claude/config.md) and the pattern repeats>"
```

The env-var line at the top of the bash command increments `AGENT_RECURSION_DEPTH` from the current process's env (default 0 if unset), then exports it to the spawned `claude` process. The same value is embedded as text in the prompt so the spawned model can see its depth directly without reading env.

### Reading the result

The child returns to stdout. The Bash tool result will start with one marker:

- **`RESOLVED:`** — apply the resolution. Continue the original work.
- **`INVESTIGATION:`** — incorporate findings. Decide whether to retry inline, escalate to the user, or accept partial.
- **`LOOP_DETECTED:`** — the recursion is not converging. Stop, summarize for the user, ask for direction. Do not spawn again on the same difficulty.

If the child hits its budget cap (`--max-budget-usd`) without emitting a marker, treat the output as `INVESTIGATION:` even without the prefix.

### Safeguards

- **Hard depth cap.** Before spawning, check `$AGENT_RECURSION_DEPTH`. If the spawn would push it **above** `max-recursion-depth` (see `~/.claude/config.md`), **do not spawn**. Instead: stop, summarize the chain for the user (original task, where the recursion is now, what the next spawn would have done), ask whether to continue manually, restart with a clean approach, or accept a partial result. This cap is shared with the global one in `CLAUDE.md` § Recursion cap.
- **Per-level budget** — `--max-budget-usd` (see `CLAUDE.md` § Budget tier — default `budget-medium-usd` for overcome-difficulty spawns) caps API spend at each level. Hitting the cap returns control to the caller.
- **Visible depth** — `AGENT_RECURSION_DEPTH` is in env and in the prompt; each level knows where it is in the stack.
- **Loop sensitivity at depth ≥ `loop-sensitivity-depth`** (see `~/.claude/config.md`) — the spawned level must self-check whether its task is a re-framing of an ancestor's task. If yes, return `LOOP_DETECTED:` early rather than recursing further.
- **Transcripts persist** — each spawned level leaves a session transcript at `~/.claude/projects/<cwd-hash>/<sid>.jsonl`. Useful for post-mortem if recursion was long.

### When NOT to escalate

- One more inline retry would obviously succeed (transient error, missed flag).
- The difficulty is a simple lookup — `grep`, `git log`, `ls` — no need for a fresh context.
- Asking the user a clarifying question would be cheaper than recursion.
- You are at the cap (`AGENT_RECURSION_DEPTH` equals `max-recursion-depth`) — escalate to the user instead.
- A previous level returned `LOOP_DETECTED:` — escalate to the user, do not re-spawn on the same difficulty.

Inline overcome-difficulty (the phases above) is the default. Spawn only when the current thread genuinely won't converge.
