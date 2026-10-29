---
name: delivered-identity-is-established-by-content-or-ancestry
description: A check that a delivery landed, or that an artifact is the one it names, must bind to content or ancestry (containment in trunk, a content anchor, this task's own trailer), not a positional coordinate (a sha equality, a commit range off the original base, a path:line pin, a tip-commit proxy); trunk advances, squash merges and line shifts falsify coordinates while the content is intact.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:a-coordinate-shifts-while-the-content-it-addressed-stays, regularity:a-shared-resource-moves-under-others-action]
domain: development
created: 2026-10-12
last_verified: 2026-10-12
---

# Delivered identity is established by content or ancestry, not by a coordinate

## Principle

Because a coordinate shifts while the content it addressed stays ([[a-coordinate-shifts-while-the-content-it-addressed-stays]]) and a shared resource moves under others' action ([[a-shared-resource-moves-under-others-action]]), for the goal of deciding landed-ness and identity correctly, express landed-ness as monotone containment of the delivered head in the target, express artifact identity by a content anchor, and scope any sweep over a moving history to this task's own marker; never assert equality of, or position within, a coordinate that unrelated movement of the shared resource can change.

## Goal class

Checks that decide "did this delivery land" or "is this the artifact I named" correctly against a shared, moving resource — no false red on intact content, no false green on changed content.

## Generality

Level 2 — ranges over any check that must decide "did this delivery land" or "is this the artifact I named" against a shared, moving resource: a trunk branch other people advance, a registry other tasks append to, a file whose lines shift under unrelated edits. The class is "identity or position asserted through an address of a thing that others keep moving". It is not claimed for a resource the task owns exclusively and that nothing else moves (a private scratch tree), where a coordinate is stable and cheaper than a content anchor, and it does not say that content comparison is always affordable: for very large artifacts a cheaper anchor must still be content-derived, not positional.

## Grounded in

- [[a-coordinate-shifts-while-the-content-it-addressed-stays]] — A positional address of an artifact — a commit sha, a range off a base, a path:line pin, a branch tip — changes under squash merges, trunk advances and unrelated edits while the content it addressed is intact, and can stay fixed while the content changes.
- [[a-shared-resource-moves-under-others-action]] — A resource other sessions or tasks also act on — a working tree, a trunk branch, a test suite's population, a session-state schema — changes between two observations by one task for reasons unrelated to that task.

## Refutation

The principle fails its goal if containment- and content-anchored checks still go red on a delivery whose content is intact on the target, or green on one whose content was changed.

## See also

- [[a-control-names-a-venue-that-outlives-its-last-rerun]] — the "where" of the re-run; this principle says what a landed-state check may bind to once the delivery venue is gone.
- [[a-rerunnable-control-asserts-a-phase-independent-property]] — containment is monotone, hence phase-independent; equality of a head is not.
- [[shared-resource-health-asserted-relative-to-a-pinned-base]] — the same hostage-to-others'-movement failure on the health axis rather than the identity axis.
