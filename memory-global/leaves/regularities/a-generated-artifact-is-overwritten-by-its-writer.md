---
name: a-generated-artifact-is-overwritten-by-its-writer
description: A config file, settings value or emitted field that a generator, installer or emitter writes is rewritten by that writer on its next run, so a hand edit of the artifact is undone or, with an additive writer, kept alongside the drift it was meant to remove.
type: reference
schema: regularity/v1
generality: 2
induced_from: [2026-07-31-generated-config-fix-the-generator-not-the-artifact, 2026-07-01-additive-settings-merge-cannot-prune-install-time-invariant, 2026-07-28-agent-emitted-value-leaks-org-key-into-public-history]
created: 2026-10-27
last_verified: 2026-10-27
---
# A config file, settings value or emitted field that a generator, installer or emitter writes is rewritten by that writer on its next run, so a hand edit of the artifact is undone or, with an additive writer, kept alongside the drift it was meant to remove

## Regularity

An artifact that a writer produces — a generated config, a settings file an installer merges into, a field an emitter fills — is rewritten by that writer on its next run. A hand edit of the artifact is undone by a regenerating writer, or, with an additive writer that only ever adds keys, survives beside the very value it was meant to remove; and the next emission by the writer reintroduces any defect that lives in the writer itself.

## Generality

Level 2 — ranges over artifacts that have a writer which runs again. It is not claimed for an artifact written once by hand and never regenerated.

## Refutation

Hand edits of written artifacts observed across later runs of their writers that persisted and were never reverted or duplicated would show the writer does not overwrite for that kind of artifact.

## Induced from

- [[2026-07-31-generated-config-fix-the-generator-not-the-artifact]] - a hand patch to a generated config would have been reverted by the next sync run, and the generator also emitted rules the consumer cannot match.
- [[2026-07-01-additive-settings-merge-cannot-prune-install-time-invariant]] - an additive installer merge could not remove deprecated keys, so every machine needed a hand fix until the installer pruned and pinned.
- [[2026-07-28-agent-emitted-value-leaks-org-key-into-public-history]] - the trailer helper emitted the raw value; the downstream gate blocked it and the old workaround dropped a harmless sibling field.
