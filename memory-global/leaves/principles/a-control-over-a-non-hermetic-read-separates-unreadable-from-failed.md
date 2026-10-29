---
name: a-control-over-a-non-hermetic-read-separates-unreadable-from-failed
description: A stage control that consumes a network read, a live API or a flaky local run needs a typed third outcome that asserts nothing about the artifact, bounded retries on pinned keys, and a failure digest that varies with the cause; otherwise one transient failure is scored as a defect and costs a full difficulty cycle.
type: reference
schema: principle/v1
generality: 1
grounded_in: [regularity:a-non-hermetic-read-fails-independently-of-the-artifact, regularity:a-two-valued-judge-absorbs-could-not-look]
domain: development
created: 2026-10-12
last_verified: 2026-10-12
---

# A control over a non-hermetic read separates 'could not read' from 'failed'

## Principle

Because a non-hermetic read fails independently of the artifact ([[a-non-hermetic-read-fails-independently-of-the-artifact]]) and a two-valued judge absorbs "could not look" ([[a-two-valued-judge-absorbs-could-not-look]]), for the goal of controls whose red can be acted on, give every control that reads a non-hermetic input three outcomes, pass, fail and could-not-read; resolve the read by a pinned key under a timeout with bounded attempts, let could-not-read assert nothing about the artifact, and digest the failure by its observed cause so a repeat can be told from a new failure.

## Goal class

Controls over non-hermetic inputs whose red result can be acted on as a defect of the artifact, and whose repeated failures can be told apart as the same cause or a new one.

## Generality

Level 1 — a candidate principle grounded in three contexts across two environments (a remote-service read and a local long-running suite); it ranges over machine-evaluated checks whose input is not reproducible from the repository alone: network reads, live service state, long suites with ambient-load sensitivity. It is deliberately not claimed for hermetic controls (a grep over a committed file), where a non-zero exit is always a finding, and the three-outcome shape is not yet shown to be right for cases where "could not read" must itself block (a security-relevant check that may not be skipped).

## Grounded in

- [[a-non-hermetic-read-fails-independently-of-the-artifact]] — A control that reads an input it does not hold — a network service, a shared tree, a nested process, a moving branch — can fail because the read failed while the artifact under judgement is fine; the read's failure and the artifact's defect are independent events.
- [[a-two-valued-judge-absorbs-could-not-look]] — A judge that can only answer pass or fail still meets cases where it could not observe; those cases do not disappear but are silently reported as one of the two answers, so an unrun check reads as passed or a lost signal reads as failed.

## Refutation

The principle fails its goal if three-outcome controls still produce fail verdicts on correct artifacts, or if their cause digests fail to tell a repeated read failure from a new one.

## See also

- [[verdict-covers-the-evidence-domain-it-claims]] — "could not look" as the third answer a verdict must be able to give; this principle applies it to a control's read.
- [[a-judging-artefact-is-executed-before-it-judges]] — running the control once, repeatedly, is how a non-reproducing exit is found before it costs a cycle.
- [[a-rerunnable-control-asserts-a-phase-independent-property]] — a control re-run at the closing gate meets a read the authoring phase never made again.
