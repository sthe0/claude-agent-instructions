---
name: overcome-difficulty
description: TRIGGER when the actual result of the plan or one of its stages diverges from the expected result — verification failed, blocker, repeated error, surprising output, plan mismatch, two or more process corrections in a row, long shell loops without progress. Work through declaration → investigation → critique to localize the moment of divergence and derive a concrete replanning task that the root coordinator then applies to fix the plan. SKIP when work is progressing as expected, or a one-off retry will obviously succeed without further analysis.
---

# Overcome difficulty

You are invoking this skill because the **actual** outcome of the plan (or one of its stages) diverged from the **expected** outcome. Work through three phases in order: **declaration**, **investigation**, **critique**. At the end, hand a concrete replanning task back to yourself — you are the root coordinator — and resume the original user task on the corrected plan.

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
