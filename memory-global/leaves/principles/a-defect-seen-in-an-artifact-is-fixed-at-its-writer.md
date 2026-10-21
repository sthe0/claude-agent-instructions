---
name: a-defect-seen-in-an-artifact-is-fixed-at-its-writer
description: When a file or value is wrong, find what writes it before editing it: a generated or emitted artifact is rewritten by its generator, and the same defect reaches every other consumer of that generator.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-07-31-generated-config-fix-the-generator-not-the-artifact, 2026-07-01-additive-settings-merge-cannot-prune-install-time-invariant, 2026-07-28-agent-emitted-value-leaks-org-key-into-public-history]
created: 2026-10-21
last_verified: 2026-10-21
---

# A defect seen in an artifact is fixed at its writer

## Principle

On finding a defect in a config file, settings value or emitted field, first locate the writer (generator, installer, emitter) and decide whether the defect lives there; fix the writer and, where the writer is additive or lossy, make it prune or pin the invariant, then add a guard so the next edit cannot reintroduce the drift. A hand edit of the artifact is only a stopgap and is named as one.

## Generality

Level 2 - a rule for any artifact that a program produces, whether config, settings, trailers or generated code.

## Induced from

- [[2026-07-31-generated-config-fix-the-generator-not-the-artifact]] - a hand patch to a generated config would have been reverted by the next sync run, and the generator also emitted rules the consumer cannot match.
- [[2026-07-01-additive-settings-merge-cannot-prune-install-time-invariant]] - an additive installer merge could not remove deprecated keys, so every machine needed a hand fix until the installer pruned and pinned.
- [[2026-07-28-agent-emitted-value-leaks-org-key-into-public-history]] - the trailer helper emitted the raw value; the downstream gate blocked it and the old workaround dropped a harmless sibling field.

## Refutation

Refuted by a defect in a generated artifact for which fixing only the artifact was durable and complete (the writer never runs again, or serves a single consumer), or by a writer fix that failed to remove the defect from the artifact because the defect was independent of it.
