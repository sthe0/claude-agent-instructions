---
name: a-control-over-a-non-hermetic-read-separates-unreadable-from-failed
description: A stage control that consumes a network read, a live API or a flaky local run needs a typed third outcome that asserts nothing about the artifact, bounded retries on pinned keys, and a failure digest that varies with the cause; otherwise one transient failure is scored as a defect and costs a full difficulty cycle.
type: reference
schema: principle/v1
generality: 1
induced_from: [2026-10-12-stage-control-liveness-nondeterminism, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]
domain: development
created: 2026-10-12
last_verified: 2026-10-12
---

# A control over a non-hermetic read separates 'could not read' from 'failed'

## Principle

Give every control that reads a non-hermetic input three outcomes, pass, fail and could-not-read; resolve the read by a pinned key under a timeout with bounded attempts, let could-not-read assert nothing about the artifact, and digest the failure by its observed cause so a repeat can be told from a new failure.

## Generality

Level 1 — a candidate principle induced from three contexts across two environments (a remote-service read and a local long-running suite); it ranges over machine-evaluated checks whose input is not reproducible from the repository alone: network reads, live service state, long suites with ambient-load sensitivity. It is deliberately not claimed for hermetic controls (a grep over a committed file), where a non-zero exit is always a finding, and the three-outcome shape is not yet shown to be right for cases where "could not read" must itself block (a security-relevant check that may not be skipped).

## Induced from

- [[2026-10-12-stage-control-liveness-nondeterminism]] — three contexts: a network read whose failure was scored as an absent artifact (repaired by a pinned-key read, bounded attempts and a distinct unreadable outcome), a single non-reproducing exit from a live-API final check that every manual re-run contradicted, and a local suite block that exited non-zero once and cleanly five times after, together with the finding that the loop guard digests the command's static text and so cannot tell a repeat from a new failure.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the original difficulty whose re-run-everything-at-verify-final design is what exposes a non-hermetic control to a second, unscheduled read.

## Refutation

A population of non-hermetic controls in which a typed could-not-read outcome was routinely abused (read as a pass that hid real defects) at a rate exceeding the transient failures it removed would refute the shape for that population and push toward bounded retries without a third outcome. Because generality is 1, a single further context where a non-hermetic control's transient failure was correctly treated as a finding, with retries masking a real intermittent defect, would also force a narrower form: separate infrastructure unreadability from a nondeterministic defect in the artifact itself.

## See also

- [[verdict-covers-the-evidence-domain-it-claims]] — "could not look" as the third answer a verdict must be able to give; this principle applies it to a control's read.
- [[a-judging-artefact-is-executed-before-it-judges]] — running the control once, repeatedly, is how a non-reproducing exit is found before it costs a cycle.
- [[a-rerunnable-control-asserts-a-phase-independent-property]] — a control re-run at the closing gate meets a read the authoring phase never made again.
