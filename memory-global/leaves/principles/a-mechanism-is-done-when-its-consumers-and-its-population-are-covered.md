---
name: a-mechanism-is-done-when-its-consumers-and-its-population-are-covered
description: A mechanism that passes its own tests can still be unfinished: judge it against who calls it, who inherits it, and what it replaces.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:a-mechanism-green-in-isolation-can-lack-consumers-and-population]
created: 2026-10-21
last_verified: 2026-10-21
---

# A mechanism is done when its consumers and its population are covered

## Principle

Because a mechanism green in isolation can lack consumers and population ([[a-mechanism-green-in-isolation-can-lack-consumers-and-population]]), for the goal of mechanisms that work in production on ship, before calling a new or tightened mechanism done, enumerate its consumers (a production caller, an entry point, onboarding, the configuration seam), the population it applies to (the whole corpus, not the file changed), and the behavior it replaces (a richer prose superset, a manual capture); run the new check corpus-wide before shipping it and grandfather legacy violations by mode rather than by surprise.

## Goal class

New or tightened mechanisms that work in production on ship — reached by their callers, applied across their whole population, at least as capable as what they replace.

## Generality

Level 2 - a rule for any mechanization, validator tightening or prose-to-code migration.

## Grounded in

- [[a-mechanism-green-in-isolation-can-lack-consumers-and-population]] — A new or tightened mechanism whose own tests pass can still have no production caller, no entry point or onboarding path, and newly enforced violations across the corpus it was never run on.

## Refutation

The principle fails its goal if mechanisms shipped after this enumeration and corpus-wide run still turn out unreachable, newly blocking on legacy violations, or poorer than what they replaced.
