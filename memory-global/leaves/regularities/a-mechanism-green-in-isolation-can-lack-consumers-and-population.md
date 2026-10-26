---
name: a-mechanism-green-in-isolation-can-lack-consumers-and-population
description: A new or tightened mechanism whose own tests pass can still have no production caller, no entry point or onboarding path, and newly enforced violations across the corpus it was never run on.
type: reference
schema: regularity/v1
generality: 2
induced_from: [2026-06-24-prose-to-code-migration-consumer-and-superset, 2026-06-27-stub-mechanism-not-deployable-until-caller-cli-onboarding, 2026-06-30-validator-tightening-newly-enforces-latent-violations, 2026-06-29-cost-tracking-three-levels-attribution]
created: 2026-10-27
last_verified: 2026-10-27
---
# A new or tightened mechanism whose own tests pass can still have no production caller, no entry point or onboarding path, and newly enforced violations across the corpus it was never run on

## Regularity

A new or tightened mechanism can be green in isolation — its own tests pass on the file that was changed — while it has no production caller, no entry point, no onboarding step and no configuration seam through which anything reaches it; while the corpus it now applies to holds violations it has never been run over; and while the behaviour it replaces did more than it does (a richer prose superset, a manual capture). None of these show in the mechanism's own tests.

## Generality

Level 2 — ranges over mechanisms introduced into or tightened within a system that has callers and a population. It is not claimed for a standalone tool with no callers by design and no existing population.

## Refutation

Mechanisms observed shipped green on their own tests that, in later use, never turned out to lack a consumer, a population sweep or a replaced behaviour would show the gap does not arise for that kind of mechanism.

## Induced from

- [[2026-06-24-prose-to-code-migration-consumer-and-superset]] - the new code had no runtime consumer and was narrower than the prose it replaced.
- [[2026-06-27-stub-mechanism-not-deployable-until-caller-cli-onboarding]] - green unit tests hid five gaps: no production caller, a nonexistent queue, no CLI, no per-machine selection, no onboarding.
- [[2026-06-30-validator-tightening-newly-enforces-latent-violations]] - a stricter shared validator flagged pre-existing violations across other authors' files.
- [[2026-06-29-cost-tracking-three-levels-attribution]] - a declared multi-level metric was captured manually and its verifier checked only the section's presence.
