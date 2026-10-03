---
name: regex-not-for-semantic-classification
description: Any lexical matcher (regex, keyword list, token overlap, similarity ratio) that decides a question of free-text MEANING (a hard block, a classification, a dedup or clustering join) determinizes a perception task at the wrong structural level and false-positives on paraphrase/meta-text; demote it to a high-recall candidate generator and let a fail-open model judge decide, mirroring agentctl/advisor.py::judge_binary_ask.
type: reference
schema: leaf/v1
created: 2026-07-22
last_verified: 2026-10-01
---

## Difficulty

The norm covers every decision of meaning, not only a hard block: a classification, a "same difficulty?" dedup, a clustering join. A word-overlap score decided that "zebra the of and quantum" matched an experience leaf (it scored 139); a regex cannot tell a paraphrase from a coincidence of vocabulary, and any overlap threshold is a regex in disguise. The sections below were written for the hard-block case and apply unchanged to the rest, with "hard block" read as "decision".

A hard-enforcement gate (a Stop-hook block, a PreToolUse deny) that decides whether a piece of free text carries a given natural-language MEANING — "is this agent-behavior feedback?", "is this an un-diagnosed outage escalation?" — by matching a regex/keyword lexicon against the raw text is determinizing a **perception** task at the wrong structural level. A regex can recognize a token co-occurrence; it cannot tell a genuine correction from an analytical discussion that merely *mentions* the same words, or a live escalation from a meta-description of how the escalation gate works. Two such gates false-positived on the agent's own read-only analytical prose about `CLAUDE.md`: a self-improvement turn-guardian fired because its feedback-signal regex matched a corrective-sounding phrase co-occurring with a second-person pronoun in a passage that was analyzing, not receiving, feedback; an escalation-diagnosis guardian fired because its two-regex conjunction (failure words + a question frame) matched a **meta-description of how the hook itself works**, not a real outage escalation. Both blocks were false positives on paraphrase/meta-text — exactly the failure mode a regex cannot avoid because meaning, not shape, is what the classification actually turns on.

This is a sharpening of the system's own root principle ("separate rule from perception; determinize the rule at its proper structural level" — `CLAUDE.md` preamble): a regex classifying free-text meaning and driving a hard block violates the very principle it is meant to serve, because the *perception* half (does this text mean X?) has been left inside the *rule* half (regex match → block).

## Guidance

**The rule: semantic/content classification is cognition, not pattern-matching.** When a hard block's decision depends on the MEANING of natural-language text (a correction vs. an analytical mention, a live incident vs. a description of one), do not let a regex decide the block directly. Demote the regex to a **high-recall PREFILTER** (cheap, precision-first — it only needs to catch every candidate, false positives are fine here because it does not block anything by itself) and add a **fail-open model judge** that makes the actual semantic call behind the prefilter. The hard block fires only when *both* the prefilter matches *and* the judge confirms.

**The worked template already in this repo:** `agentctl/advisor.py::judge_binary_ask` (the function backing `prose_binary_ask` in `hook-turn-end-gate.py`) already implements exactly this shape for one path — "is this assistant turn a binary confirm-style question?" — behind a cheap, language-independent punctuation prefilter, with the following fail-open contract: not enabled / no runner / empty text → `False`; runner exits non-zero / prints nothing / prints something unparseable / raises → `False`; only an explicit `YES` first line → `True`. Its own docstring cites the CLAUDE.md rule-vs-perception paragraph. Any new semantic classifier that must drive a hard block should mirror this function's body (model, timeout, YES/NO protocol, exception → `False`) rather than inventing a new contract, per the self-improvement tie-breaker "extend the existing mechanism that already implements the rule for one path".

**Why fail-open is the safe direction here specifically.** The failure being removed in both exemplar cases was a FALSE hard block — the gate fired when it should not have. A judge that errs toward `False` (no block) on any ambiguity, timeout, or infra hiccup cannot re-introduce that false positive; it can only under-block. Whether that is acceptable depends on whether an independent recall backstop exists for the same signal outside the hard-block path — state this explicitly per classifier, don't assume it. (For the self-improvement feedback signal, the advisory `hook-self-improvement-reminder.py` — regex-driven, never blocks, only prints a nudge — remains the recall backstop if the Stop-hook judge fails open. For the outage-escalation signal there is no independent backstop once both its consumers share one judge; a judge `NO` on a genuine un-diagnosed outage is accepted recall loss, justified only by an explicit user preference for fail-open over disruptive false blocks and the low base rate of the scenario — this is a stated trade-off, not a free lunch.)

### Joins and dedup

A join over free-text grounds ("is this the same difficulty as that leaf?") is a decision of meaning like any other. The lexical half may only nominate: `record-experience.py search` ranks leaves by term overlap and its top hits are the *candidates*, taken in rank order with no score threshold, ratio or token-count cut-off; the model judge (`advisor.judge_same_difficulty`, the `judge_binary_ask` contract) alone decides each candidate. A judge that cannot answer (killswitch, timeout, unparseable, raised) is not a NO and not a YES: the item is kept and flagged `judge-unavailable`, because a duplicate costs a glance and a dropped finding costs the difficulty. The first instance is `improvement-scan.py telemetry --grounds` (outcomes `no-match` / `dedup-match` / `board-match` / `search-failed` / `judge-unavailable`, counted on its `dedup outcomes:` summary line); a ground that overlaps a leaf word for word is still `no-match` when the judge says the difficulty differs.

### The structural-vs-semantic boundary

Not all regex use in a hard-enforcement gate is the anti-pattern. The boundary:

- **STRUCTURAL (legitimate regex)** — the regex reads TOOL-INVOCATION SHAPE, COMMAND SYNTAX, or a FILE PATH: does this Bash command match a known long-running-job pattern, does this tool call look like a timer-arm request, is this path inside a protected mount. These are decidable from the input's *structure*, not its meaning — a regex is the right determinization level and needs no judge behind it.
- **SEMANTIC (the anti-pattern)** — the regex reads NATURAL-LANGUAGE MEANING to decide whether a hard block should fire: is this text a behavioral correction, is this text an outage escalation, is this text a request to do something risky. These require the prefilter → fail-open judge → block shape above.

### A third case: SCOPE/EFFECT — grounded in the tool's contract, not its invocation spelling

A command's **scope or effect** — does this specific invocation stay within a plan's declared boundary, does this ad-hoc script only touch what it claims to — is neither of the two cases above. It is not STRUCTURAL (the invocation's shape says nothing about what the tool actually *does*) and it is not SEMANTIC in the free-text-meaning sense (there is no natural-language claim to interpret). It is a property of the **tool's contract** — its documentation, its tests, its source — and a regex or keyword match against the command line cannot decide it any more than it could decide a paraphrase's meaning: a `git checkout` argument alone does not tell you whether the checkout also discards untracked files, and a python script's filename alone does not tell you what it writes.

**The fix is not a prefilter → judge pair (that shape is for free-text meaning); it is to read the contract.** When a command's in-scope-ness is unclear:

- **For a standard/typical tool** (git, a shell builtin, a well-known CLI), ground the judgment in its actual documented contract — `--help`, man page, or (when genuinely ambiguous) the source — and do this **proactively at plan-construction time** for every tool the plan's stages are expected to invoke, not reactively per invocation during execution (see [planner/SKILL.md](../../skills/specializations/planner/SKILL.md) § Plan format).
- **For an ad-hoc script** (no external contract exists), the contract is internal: read its own content — what it writes, deletes, or calls — against the plan's declared material/means, not its invocation surface (its name or arguments tell you nothing a stranger's paraphrase wouldn't).

This is the same root principle as the structural/semantic split, applied to a third axis: the rule ("is this in scope?") is decidable, but only once the perception step — reading the actual contract — has supplied the fact a regex cannot manufacture from the command's spelling. *Difficulty removed:* treating an unclear-scope command as a coin flip between "obviously fine" and a blanket `PERMISSION-REQUEST:` skips the one step that actually resolves it — reading the tool's own contract, which the agent already has every means to do.

**The mechanized instance of "read the contract."** `scripts/agentctl/tool_contracts.toml` is that contract, made machine-readable rather than left as a per-invocation judgment call: `tool_contracts.py`'s `resolve_command` resolves a command's literal text against the table to a typed resource, not a regex match on the command's own spelling. A command the table cannot pin to a resource resolves `unresolved` rather than being coin-flipped either way — the SCOPE/EFFECT judgment above is exactly what this resolution step automates for the covered cases, and exactly what still falls to the agent's own read of the contract for the rest.

### The live enumeration (supersedes the original hand audit)

The original audit here was a hand-built table: `grep` for three enforcement contracts (PreToolUse `"permissionDecision": "deny"`, a Stop `"decision": "block"`, `sys.exit(2)`) across the hook suite, each hit classified by hand. It closed with a UNIVERSAL claim — "no further semantic hard-block was found in the suite" — that its own bounded domain cannot support: a regex driving a hard *behaviour* (a routed/dispatched/suppressed/recorded decision) rather than a deny/block/exit uses none of the three contracts and so is invisible to a grep built around them. A universally-quantified claim is only discharged against a mechanically enumerated domain (`CLAUDE.md` § On task resolution); a hand-built list is existential evidence about the sites someone thought to grep for.

A follow-on task replaced the hand audit with a mechanical enumerator (`scripts/crutch-inventory.py`) over both this leaf's domain (regex-driven hard-outcome code sites, widened past the three-contract boundary) and a second domain never audited before — candidate decidable-rule statements left as prose. The classification is recorded as data in `scripts/crutch_registry.toml`, re-checked on every run by `scripts/verify-semantic-gates.py` so a regression cannot land silently. **That registry, not the table this section used to carry, is the live source of the current state of any site.** Full documentation of the mechanism — how a site gets a class, how to add one, and its honest limits — lives in [docs/operations/crutch-registry.md](../../docs/operations/crutch-registry.md).

As of the mechanical enumeration: **2042 sites total** (752 code + 166 code-file-rollup + 1124 prose — the prose count includes this leaf's own rewrite and its companion experience leaf, both self-registered by the loop that produced this text). **7 code sites are `semantic-guarded`** — a regex feeding a hard outcome with a fail-open judge on the same path, including the four sites that task classified semantic (three it fixed, one already judge-backed), now re-verified structurally rather than carried by memory — and **0 are `semantic-unguarded`**: the anti-pattern this leaf names does not currently exist anywhere in the enumerated domain, and the verifier fails the moment one is introduced. Three `decidable` prose rules in `CLAUDE.md` remain explicitly `defer`red, each with a named, dated reason (two blocked on the verifier's own landing, one judged not worth building yet relative to the size of the win) — the full deferral list is published in [docs/operations/crutch-registry.md](../../docs/operations/crutch-registry.md).

**What this corrected claim is honestly built on, and what it is not.** The registry closes exactly the gap the original table's grep missed (hard behaviour, not only deny/block/exit) and adds a domain the original table never attempted (prose). It does not see dynamic pattern construction, a crutch expressed without a regex or a modal keyword, or cross-file semantic duplicates of the same rule — the full limits list is in the docs page above, restated there rather than here so the claim and its boundary do not drift apart. For the prose domain specifically, the honest word is "routed" (a new statement is surfaced and must be dispositioned by a model pass) — never "verified" (no mechanical judgement is made about whether a statement is genuine perception).

### Open violations

Lexical "same difficulty?" decisions still to convert to candidate-generation plus a model judge (the crutch registry above does not see them: they feed a join, not a hard outcome):

- `scripts/record-experience.py` `cluster_by_ground` / `_similarity` — the word-overlap ratio (`JOIN_RATIO`) that clusters experience leaves for promotion.
- `scripts/improvement-scan.py` `classify_and_score` — its backlog clustering calls `cluster_by_ground`, so it inherits that join.
- `scripts/core-difficulty-digest.py` `cluster_records` — clusters difficulty records by the same ratio.
- `scripts/sigma-sentinel.py` `measure_condition_a` / `measure_cheap_c` — join an experience leaf to a principle (and count near-duplicate pairs) by the same ratio.

Lexical classifications of a user prompt's meaning, also still to convert (they feed counters, not a hard outcome):

- `scripts/cost-report.py` `CORRECTION_RE` (used by `parse_transcripts(classify=True)` for its "likely corrections (heuristic, approximate)" line) — a regex alone calls a prompt a correction, and it counts every non-tool-result entry, machine text included.
- `scripts/policy-scorecard.py` `QUESTION_RE` (any `?`) and `RESOLUTION_RE` — now applied to human prompts only (see below), but still lexical decisions of meaning.

Converted:

- improvement-scan telemetry-ground dedup (above).
- `scripts/policy-scorecard.py` correction count (`attention.corrections`, which `improvement-scan.py`'s attention-burn detector, the scorecard's correction-rate flag and `rule-salience-report.py`'s trigger proxy all read). Who spoke is structure: only entries stamped `origin.kind == "human"` are prompts (a 14-day sweep found 63% of the old "corrections" were machine text or regex noise). What the prompt means is the judge's: `si_feedback_detect.find_signals` on the injection-stripped text only nominates, `advisor.judge_feedback_signal` decides (the pair `hook-turn-end-gate.py` uses), and a nomination the judge did not answer is counted in `attention.corrections_unjudged`, never as a correction. Verdicts are cached by the hash of the stripped text. Accepted recall change: the prefilter is now `si_feedback_detect`'s, so a bare "that's wrong" with no reference to the agent no longer nominates.

## See also

- [[determinize-required-specialist-dispatch]] — a sibling application of the same root principle: proactively pairing a trigger with an existing reactive gate rather than leaving a deterministically-decidable obligation to prose-guided memory.
- `agentctl/advisor.py::judge_binary_ask` — the in-repo fail-open prefilter→model→YES/NO template this leaf's recipe mirrors.
- `CLAUDE.md` preamble, "Separate rule from perception; determinize the rule at its proper structural level" — the root principle this leaf sharpens for the semantic/content case.
- `skills/self-improvement/SKILL.md` § "Structural form before prose" — the tie-breaker rule (extend an existing mechanism rather than invent prose) this leaf's recipe follows.
