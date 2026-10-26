---
name: a-generic-failure-symptom-is-traced-to-its-cause-before-a-remedy-is-applied
description: Errors such as 'file not found', 'hangs', 'password authentication unsupported' or 'keys fall off' name the symptom's surface, not its cause, and invite a remedy on the wrong axis that is sometimes destructive.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:a-generic-failure-message-names-a-surface-shared-by-many-causes]
created: 2026-10-21
last_verified: 2026-10-21
---

# A generic failure symptom is traced to its cause before a remedy is applied

## Principle

Because a generic failure message names a surface shared by many causes ([[a-generic-failure-message-names-a-surface-shared-by-many-causes]]), for the goal of a first remedy that removes the failure, before acting on a generic failure message, state at least two causes on different axes (working directory, version, credential channel, process lifetime), find the observation that separates them without changing state, and choose the least destructive remedy that addresses the confirmed one; a remedy that makes the symptom worse is a signal that the cause was misjudged.
## Goal class

Remedies for generic failures that remove the failure on the first applied remedy, without a destructive step aimed at the wrong cause.

## Generality

Level 2 - a rule for diagnosing any failure whose message does not mention the broken thing.

## Grounded in

- [[a-generic-failure-message-names-a-surface-shared-by-many-causes]] — A generic failure message (hang, auth failure, file not found, connection reset) names a surface reached by several unrelated causes on different axes; the message alone does not select among them, and a remedy aimed at the wrong one can deepen the failure.

## Refutation

The principle fails its goal if remedies chosen after a separating observation still miss the cause or make the symptom worse at a rate no lower than remedies applied directly.
