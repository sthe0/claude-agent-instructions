---
name: 2026-06-24-prose-to-code-migration-consumer-and-superset
description: Difficulty — moving a prose coordination process into agentctl code fails two ways: the new code has no runtime consumer (advisory dead code, e.g. a pure function nothing calls), or it is narrower than the prose it 'replaces' (information loss). Fix: (a) give the function a real consumer today, ideally an existing duplicated implementation it can delegate to; (b) before collapsing prose to a code pointer, check whether the prose is a richer superset — if so cross-reference, don't delete.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "Принять и запушить"
refs: [2026-05-26-agent-system-plan-vs-reality-drift]
---

# Porting a prose process to code: secure a real consumer, don't collapse a prose superset

## Difficulty
Two distinct failure modes when codifying a prose process. (1) Dead-consumer: the natural code form (a pure verdict function) has no caller in the current system, so it is advisory data until some future wiring (the same 'no consumer until auto-start' caveat that dogged Phase 3). (2) Superset-collapse: the roadmap assumes the prose duplicates the code and says 'trim prose to a pointer', but the prose is often a richer human-judgment superset of the conservative machine subset; collapsing it deletes decision-useful hints (e.g. verb heuristics the code returns 'unknown' for).

## Order & criterion
When moving a prose process into code: (1) find a consumer that calls the new code TODAY — if none, look for an existing duplicated implementation the new code can become the single source for and delegate to it (turns dead advisory code into a live, drift-removing refactor); else defer until the consumer lands. (2) Before trimming prose, diff prose-coverage vs code-coverage: collapse to a pointer only if the code is a true superset; otherwise keep the richer prose and add a keep-in-sync cross-reference to the single code source.

**Acceptance check:** measurable for the code (new function has >=1 caller exercised by tests/verify-all; behaviour-preserving — base.json verdicts byte-identical); acceptance-review for the prose (richer judgment hints survive, with a pointer to the single code source)

## Contexts

### 2026-06-24 — Phase 4 — classify_action / verb taxonomy
- Where it arose: agentctl prose->code roadmap, Phase 4: extracting the side-effect-free verb taxonomy
- Working plan: Reframed Phase 4 from 'write advisory classify_action' to 'extract the verb taxonomy already duplicated ad-hoc in lint-settings-base.py into classify_action, and make the linter delegate' — real consumer (the linter, run by verify-all) today. Step 2 prose: cross-referenced classify_action as canonical from CLAUDE.md/acting-without-asking.md WITHOUT deleting the richer verb heuristics (prose is a superset).

## Cost
grounding Explore (sonnet) ~$0.5; developer (sonnet, code) $0.94; manager prose edits in-thread. 2 commits 2be4afd+bd33469.

## Self-critique of the agent system
The approved plan literally said 'collapse prose to a pointer'; I almost executed that verbatim, which would have deleted the §3 verb heuristics. Caught it only by reading the trim targets before editing. Lesson: read the prose-trim target and compare its coverage to the code BEFORE assuming redundancy.
