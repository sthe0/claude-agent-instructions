---
name: a-mechanism-is-done-when-its-consumers-and-its-population-are-covered
description: A mechanism that passes its own tests can still be unfinished: judge it against who calls it, who inherits it, and what it replaces.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-06-24-prose-to-code-migration-consumer-and-superset, 2026-06-27-stub-mechanism-not-deployable-until-caller-cli-onboarding, 2026-06-30-validator-tightening-newly-enforces-latent-violations, 2026-06-29-cost-tracking-three-levels-attribution]
created: 2026-10-21
last_verified: 2026-10-21
---

# A mechanism is done when its consumers and its population are covered

## Principle

Before calling a new or tightened mechanism done, enumerate its consumers (a production caller, an entry point, onboarding, the configuration seam), the population it applies to (the whole corpus, not the file changed), and the behavior it replaces (a richer prose superset, a manual capture); run the new check corpus-wide before shipping it and grandfather legacy violations by mode rather than by surprise.

## Generality

Level 2 - a rule for any mechanization, validator tightening or prose-to-code migration.

## Induced from

- [[2026-06-24-prose-to-code-migration-consumer-and-superset]] - the new code had no runtime consumer and was narrower than the prose it replaced.
- [[2026-06-27-stub-mechanism-not-deployable-until-caller-cli-onboarding]] - green unit tests hid five gaps: no production caller, a nonexistent queue, no CLI, no per-machine selection, no onboarding.
- [[2026-06-30-validator-tightening-newly-enforces-latent-violations]] - a stricter shared validator flagged pre-existing violations across other authors' files.
- [[2026-06-29-cost-tracking-three-levels-attribution]] - a declared multi-level metric was captured manually and its verifier checked only the section's presence.

## Refutation

Refuted by a mechanism shipped with no named consumer, population check or replaced-behavior comparison that later proved complete, or by a case where the coverage enumeration itself cost more than the gap it would have caught.
