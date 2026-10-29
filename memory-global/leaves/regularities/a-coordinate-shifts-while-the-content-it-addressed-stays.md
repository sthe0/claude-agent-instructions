---
name: a-coordinate-shifts-while-the-content-it-addressed-stays
description: A positional address of an artifact — a commit sha, a range off a base, a path:line pin, a branch tip — changes under squash merges, trunk advances and unrelated edits while the content it addressed is intact, and can stay fixed while the content changes.
type: reference
schema: regularity/v1
generality: 2
domain: development
induced_from: [2026-10-12-landed-state-identity-by-coordinate, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan, pushed-to-personal-branch-is-not-landed, 2026-08-11-sync-stash-identity-not-position]
created: 2026-10-27
last_verified: 2026-10-27
---
# A positional address of an artifact

## Regularity

A positional coordinate of an artifact — a head sha, a commit range off an original base, a `path:line` pin, a tip commit, a stash index — changes under movements that leave the addressed content intact: a squash merge orphans the delivered head while its bytes sit on trunk, an advance of trunk moves the range, an unrelated edit shifts the lines. The same movements can leave a coordinate pointing at something that is no longer the content it once addressed. Content and coordinate vary independently once the resource moves.

## Generality

Level 2 — ranges over artifacts addressed inside a resource others move — a branch, a registry, a file, a stack. It is not claimed for a resource nothing else moves, where a coordinate stays bound to its content.

## Refutation

A moving shared resource on which a positional coordinate stayed bound to the same content through squash merges, trunk advances and unrelated edits — content-bound and coordinate-bound checks never disagreeing — would show the independence does not hold for that resource.

## Induced from

- [[2026-10-12-landed-state-identity-by-coordinate]] — seven contexts in which landed-ness or identity was checked by head-sha equality, a range off a base, a `path:line` pin, a tip-commit proxy or a trailer match, and went red after trunk advanced or a squash merge orphaned the frozen head while the delivered content was byte-identical on trunk; the same movement could equally hide a genuine edit behind an unchanged coordinate, so the fragility runs both ways.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the original difficulty from which the landed-state check kind was introduced in the engine (containment of the delivered head in the target rather than equality), and the kept contexts that fix what "landed" must mean once the delivery worktree no longer exists.
