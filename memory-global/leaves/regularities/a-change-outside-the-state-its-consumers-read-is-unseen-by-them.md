---
name: a-change-outside-the-state-its-consumers-read-is-unseen-by-them
description: A change is seen by its consumers only in the state they read; elsewhere it decays unattended
type: reference
schema: regularity/v1
generality: 1
induced_from: [landing-discipline]
rule_derived: "written from a rule leaf, grounding-norm seed"
introduced_by: grounding-norm
created: 2026-10-29
last_verified: 2026-10-29
---

# a-change-outside-the-state-its-consumers-read-is-unseen-by-them

## Regularity

A change exists for its consumers only in the state they read from — trunk for code and instructions, the published surface for text. Anywhere else — a branch, a worktree, an open review, a local commit — it is not seen by them, and it decays there unattended: the branch falls behind trunk, the worktree is removed, the review is forgotten. A delivery report that names the intermediate state reads like one that names the read state.

## Generality

Level 1 — ranges over deliveries of code, instructions and text whose consumers read one designated state (trunk or a published surface). It is deliberately not claimed for work that has no designated read state, nor for the quality of a change, only for where it is visible.

## Induced from

- [[landing-discipline]] — contributes the observed practice behind the statement: trunk is the terminal state, and a branch, worktree or open review left short of it is not the delivery.

## Refutation

Refuted by a change that its consumers use while it lives only on a branch, in a worktree, in an open review or as a local commit; or by such an intermediate state that does not decay unattended even when left alone.
