---
name: regex-not-for-semantic-classification
description: A regex that classifies free-text MEANING to drive a hard block determinizes a perception task at the wrong structural level and false-positives on paraphrase/meta-text; demote it to a high-recall prefilter and let a fail-open model judge decide, mirroring agentctl/advisor.py::judge_binary_ask.
type: reference
schema: leaf/v1
created: 2026-07-22
last_verified: 2026-07-22
---

## Difficulty

A hard-enforcement gate (a Stop-hook block, a PreToolUse deny) that decides whether a piece of free text carries a given natural-language MEANING — "is this agent-behavior feedback?", "is this an un-diagnosed outage escalation?" — by matching a regex/keyword lexicon against the raw text is determinizing a **perception** task at the wrong structural level. A regex can recognize a token co-occurrence; it cannot tell a genuine correction from an analytical discussion that merely *mentions* the same words, or a live escalation from a meta-description of how the escalation gate works. Two such gates false-positived on the agent's own read-only analytical prose about `CLAUDE.md`: a self-improvement turn-guardian fired because its feedback-signal regex matched a corrective-sounding phrase co-occurring with a second-person pronoun in a passage that was analyzing, not receiving, feedback; an escalation-diagnosis guardian fired because its two-regex conjunction (failure words + a question frame) matched a **meta-description of how the hook itself works**, not a real outage escalation. Both blocks were false positives on paraphrase/meta-text — exactly the failure mode a regex cannot avoid because meaning, not shape, is what the classification actually turns on.

This is a sharpening of the system's own root principle ("separate rule from perception; determinize the rule at its proper structural level" — `CLAUDE.md` preamble): a regex classifying free-text meaning and driving a hard block violates the very principle it is meant to serve, because the *perception* half (does this text mean X?) has been left inside the *rule* half (regex match → block).

## Guidance

**The rule: semantic/content classification is cognition, not pattern-matching.** When a hard block's decision depends on the MEANING of natural-language text (a correction vs. an analytical mention, a live incident vs. a description of one), do not let a regex decide the block directly. Demote the regex to a **high-recall PREFILTER** (cheap, precision-first — it only needs to catch every candidate, false positives are fine here because it does not block anything by itself) and add a **fail-open model judge** that makes the actual semantic call behind the prefilter. The hard block fires only when *both* the prefilter matches *and* the judge confirms.

**The worked template already in this repo:** `agentctl/advisor.py::judge_binary_ask` (the function backing `prose_binary_ask` in `hook-turn-end-gate.py`) already implements exactly this shape for one path — "is this assistant turn a binary confirm-style question?" — behind a cheap, language-independent punctuation prefilter, with the following fail-open contract: not enabled / no runner / empty text → `False`; runner exits non-zero / prints nothing / prints something unparseable / raises → `False`; only an explicit `YES` first line → `True`. Its own docstring cites the CLAUDE.md rule-vs-perception paragraph. Any new semantic classifier that must drive a hard block should mirror this function's body (model, timeout, YES/NO protocol, exception → `False`) rather than inventing a new contract, per the self-improvement tie-breaker "extend the existing mechanism that already implements the rule for one path".

**Why fail-open is the safe direction here specifically.** The failure being removed in both exemplar cases was a FALSE hard block — the gate fired when it should not have. A judge that errs toward `False` (no block) on any ambiguity, timeout, or infra hiccup cannot re-introduce that false positive; it can only under-block. Whether that is acceptable depends on whether an independent recall backstop exists for the same signal outside the hard-block path — state this explicitly per classifier, don't assume it. (For the self-improvement feedback signal, the advisory `hook-self-improvement-reminder.py` — regex-driven, never blocks, only prints a nudge — remains the recall backstop if the Stop-hook judge fails open. For the outage-escalation signal there is no independent backstop once both its consumers share one judge; a judge `NO` on a genuine un-diagnosed outage is accepted recall loss, justified only by an explicit user preference for fail-open over disruptive false blocks and the low base rate of the scenario — this is a stated trade-off, not a free lunch.)

### The structural-vs-semantic boundary

Not all regex use in a hard-enforcement gate is the anti-pattern. The boundary:

- **STRUCTURAL (legitimate regex)** — the regex reads TOOL-INVOCATION SHAPE, COMMAND SYNTAX, or a FILE PATH: does this Bash command match a known long-running-job pattern, does this tool call look like a timer-arm request, is this path inside a protected mount. These are decidable from the input's *structure*, not its meaning — a regex is the right determinization level and needs no judge behind it.
- **SEMANTIC (the anti-pattern)** — the regex reads NATURAL-LANGUAGE MEANING to decide whether a hard block should fire: is this text a behavioral correction, is this text an outage escalation, is this text a request to do something risky. These require the prefilter → fail-open judge → block shape above.

### Structural-vs-semantic audit of the hook suite

**PENDING — filled in by the audit stage of the task that produced this leaf.** The full enumeration of every detector-driven hard-enforcement site in the hook suite is classified against the boundary above, confirming whether the semantic-hard-block set is exactly the two sites fixed alongside this leaf (the self-improvement feedback signal and the outage-escalation signal, both re-wired to a prefilter-AND-judge pair mirroring `judge_binary_ask`) — or naming any further semantic hard-block found, with its file and either its fix or the reason it was deferred.

## See also

- [[determinize-required-specialist-dispatch]] — a sibling application of the same root principle: proactively pairing a trigger with an existing reactive gate rather than leaving a deterministically-decidable obligation to prose-guided memory.
- `agentctl/advisor.py::judge_binary_ask` — the in-repo fail-open prefilter→model→YES/NO template this leaf's recipe mirrors.
- `CLAUDE.md` preamble, "Separate rule from perception; determinize the rule at its proper structural level" — the root principle this leaf sharpens for the semantic/content case.
- `skills/self-improvement/SKILL.md` § "Structural form before prose" — the tie-breaker rule (extend an existing mechanism rather than invent prose) this leaf's recipe follows.
