---
name: delivered-identity-is-established-by-content-or-ancestry
description: A check that a delivery landed, or that an artifact is the one it names, must bind to content or ancestry (containment in trunk, a content anchor, this task's own trailer), not a positional coordinate (a sha equality, a commit range off the original base, a path:line pin, a tip-commit proxy); trunk advances, squash merges and line shifts falsify coordinates while the content is intact.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-10-12-landed-state-identity-by-coordinate, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]
domain: development
created: 2026-10-12
last_verified: 2026-10-12
---

# Delivered identity is established by content or ancestry, not by a coordinate

## Principle

Express landed-ness as monotone containment of the delivered head in the target, express artifact identity by a content anchor, and scope any sweep over a moving history to this task's own marker; never assert equality of, or position within, a coordinate that unrelated movement of the shared resource can change.

## Generality

Level 2 — ranges over any check that must decide "did this delivery land" or "is this the artifact I named" against a shared, moving resource: a trunk branch other people advance, a registry other tasks append to, a file whose lines shift under unrelated edits. The class is "identity or position asserted through an address of a thing that others keep moving". It is not claimed for a resource the task owns exclusively and that nothing else moves (a private scratch tree), where a coordinate is stable and cheaper than a content anchor, and it does not say that content comparison is always affordable: for very large artifacts a cheaper anchor must still be content-derived, not positional.

## Induced from

- [[2026-10-12-landed-state-identity-by-coordinate]] — seven contexts in which landed-ness or identity was checked by head-sha equality, a range off a base, a `path:line` pin, a tip-commit proxy or a trailer match, and went red after trunk advanced or a squash merge orphaned the frozen head while the delivered content was byte-identical on trunk; the same movement could equally hide a genuine edit behind an unchanged coordinate, so the fragility runs both ways.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the original difficulty from which the landed-state check kind was introduced in the engine (containment of the delivered head in the target rather than equality), and the kept contexts that fix what "landed" must mean once the delivery worktree no longer exists.

## Refutation

A shared resource on which a positional coordinate (a sha, a range, a line number) stayed valid through a squash merge, a trunk advance and an unrelated edit, with content-bound and coordinate-bound checks disagreeing in the coordinate's favour, would show the principle over-reaches for that resource. A case where the content anchor itself is unstable, because the artifact is regenerated on every landing (a derived index, a stamped timestamp), would force a broader form: anchor to the invariant sub-content, not the whole artifact.

## See also

- [[a-control-names-a-venue-that-outlives-its-last-rerun]] — the "where" of the re-run; this principle says what a landed-state check may bind to once the delivery venue is gone.
- [[a-rerunnable-control-asserts-a-phase-independent-property]] — containment is monotone, hence phase-independent; equality of a head is not.
- [[shared-resource-health-asserted-relative-to-a-pinned-base]] — the same hostage-to-others'-movement failure on the health axis rather than the identity axis.
