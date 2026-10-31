---
name: an-instruction-edit-degrades-task-quality-without-error-and-an-unstamped-quality-record-cannot-be-traced-to-it
description: An instruction or code edit can lower task quality silently, and a quality record without the instructions revision cannot be matched to the causing commits
type: reference
schema: regularity/v1
generality: 1
induced_from: [quality-regression-investigation]
rule_derived: "lifted from quality-regression-investigation.md (## Difficulty); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# an-instruction-edit-degrades-task-quality-without-error-and-an-unstamped-quality-record-cannot-be-traced-to-it

## Regularity

An edit to an agent's instructions or code (slimming CLAUDE.md, mechanizing a rule, changing a skill or hook) can lower task-solving quality without any error at the edit: the prose that carried salience is gone, a mechanized gate misses the perception half, or a rule lost its functional ground. The loss shows only as later worse tasks, and a quality record that does not carry the instructions revision it was produced under cannot be matched to the commit range that caused it, so the cause is guessed at.

## Generality

Level 1 — ranges over edits to an agent's instructions, skills, hooks and engine code, and the per-task quality records written afterwards. It is deliberately not claimed for edits to ordinary product code, whose regressions raise errors or fail tests.

## Induced from

- [[quality-regression-investigation]] — contributes the observed practice behind the statement: slimming and mechanization edits silently degraded quality, and only a per-task series stamped with the instructions revision made the commit range attributable.

## Refutation

An instruction edit that removed salience-bearing prose or moved a rule into a gate, followed by a measurable quality drop that the unstamped per-task series still attributed to that exact commit range without any revision stamp, would show the stamp is not needed for attribution.
