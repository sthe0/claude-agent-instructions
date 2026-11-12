---
name: review-gap-from-human-remarks
description: An accepted human PR remark that our own code-reviewer missed is a reviewer-gap signal — diagnose why and propose a reviewer brief/skill edit unprompted, in the same turn as planning the fix; non-blocking review findings must be dispositioned; a marker-less reviewer reply is re-run, not accepted
type: feedback
created: 2026-11-12
last_verified: 2026-11-12
---

**Difficulty removed:** a human reviewer finds in a PR what our independent code-reviewer did not; the fix lands, the reviewer stays as blind as before, and the same class of miss reaches the next PR. Observed twice in one project (two consecutive ticket PRs, 2026-10 and 2026-11); the second time the user had to ask "why don't you propose fixing the review process?".

## Rule — to keep the reviewer learning from human remarks, do this

1. **Missed remark → reviewer-gap analysis, unprompted.** When you accept a human PR remark, check whether our code-reviewer pass on that diff found it. If it did not, in the same turn you plan the fix also state *why it was missed* (axis outside the reviewer's role, brief omitted the input class, read-only pass with no execution, budget) and propose — via `self-improvement` — the reviewer skill or brief edit that would have caught it. Do not wait for the user to ask.
2. **Non-blocking findings are dispositioned, not dropped.** An ACCEPT / `approve-with-nits` verdict closes the review stage, not its findings: before asking about PR creation, each should-fix / nit is either fixed or stated in the PR description with its reason. A finding about divergence from a ported prototype is never silently dropped.
3. **A reviewer reply without a return marker is re-run.** A MALFORMED reply (no `COMPLETED:` / …) is not a verdict; re-dispatch with the same brief instead of reading a verdict out of the prose.

Candidate mechanization if rule 2 recurs despite this leaf: `agentctl code-review` requires a disposition per non-blocking finding before recording an approving verdict.

## Precedent (internal ticket PR, 2026-11-12)

Go port of a Python Jev reply parser. The human reviewer found (a) `Decode` ignored trailing data after the JSON object, turning a fail-open path into `passed=false`; (b) `KeyError` where Python raises `TypeError`. Our reviewer missed (a): its role excluded correctness, it only read code (2 min), and the brief never named malformed external replies. It found (b) as non-blocking; the root dropped it after ACCEPT and had accepted a MALFORMED reply. Remedy for the role gap: the code-reviewer's *prototype parity* axis (`skills/specializations/code-reviewer/SKILL.md`).
