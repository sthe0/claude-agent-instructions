---
name: a-non-hermetic-read-fails-independently-of-the-artifact
description: A control that reads an input it does not hold — a network service, a shared tree, a nested process, a moving branch — can fail because the read failed while the artifact under judgement is fine; the read's failure and the artifact's defect are independent events.
type: reference
schema: regularity/v1
generality: 1
domain: development
induced_from: [2026-10-12-stage-control-liveness-nondeterminism, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan, 2026-07-16-benchmark-measurement-contamination-env-resource-artifact]
created: 2026-10-27
last_verified: 2026-10-27
---
# A control that reads an input it does not hold

## Regularity

A control that reads an input it does not hold — a network service, a shared working tree, a nested process, a branch others advance, a timing-dependent output — can fail because the read itself failed while the artifact it judges is correct, and the same artifact can pass or fail on two runs with no change in between. The read's failure and the artifact's defect are independent events with independent causes; a single red result does not say which of the two occurred, and repeating the run does not tell a repeat of the same read failure from a new one unless the cause was recorded.

## Generality

Level 1 — ranges over controls whose verdict depends on an input outside the control's own process and the artifact's content. It is not claimed for a hermetic control that reads only pinned, local, immutable inputs.

## Refutation

A non-hermetic control observed across many runs whose red results always coincided with a real defect of the artifact — no red with an unchanged, correct artifact — would show that for that input the read does not fail independently.

## Induced from

- [[2026-10-12-stage-control-liveness-nondeterminism]] — three contexts: a network read whose failure was scored as an absent artifact (repaired by a pinned-key read, bounded attempts and a distinct unreadable outcome), a single non-reproducing exit from a live-API final check that every manual re-run contradicted, and a local suite block that exited non-zero once and cleanly five times after, together with the finding that the loop guard digests the command's static text and so cannot tell a repeat from a new failure.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the original difficulty whose re-run-everything-at-verify-final design is what exposes a non-hermetic control to a second, unscheduled read.
