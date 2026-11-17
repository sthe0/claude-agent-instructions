---
name: a-dependencys-native-guarantees-are-established-before-compensation-is-built
description: Before writing a parser, normalizer, retry, fallback or local timeout around an external dependency, establish what the dependency guarantees itself in its current version; re-establish it when the dependency's version changes or when the same failure class needs a second patch.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:compensation-is-designed-from-the-observed-failure-not-from-the-dependency-guarantees]
created: 2026-11-17
last_verified: 2026-11-17
grounding_judged: d6eb88f39f1de48f5e194b55faef7e0090f134cb2862db9c4bdb473bd9f25c3f
---

# A dependency's native guarantees are established before compensation is built

## Principle

Because compensation is designed from the observed failure rather than from what the dependency guarantees, for the goal of minimal and reliable code around external dependencies, establish from the dependency's help, documentation or changelog what it guarantees natively in its current version before building compensation, prefer the native guarantee where it exists, and re-establish it for an existing compensation or template when the dependency's version changes or when the same failure class needs a second patch.

## Goal class

Code around external dependencies that carries no compensation the dependency already makes unnecessary, and whose templates follow the dependency as it gains guarantees.

## Generality

Level 2 — ranges over the design and the review of wrappers around a dependency the author does not control: a model CLI, a third-party API, a library, a platform service. The check is one lookup (`--help`, the documentation, the changelog) per failure class. Not claimed for the author's own code, which is fixed at the source, nor as a reason to drop a fail-open path for failures no guarantee covers (timeout, refusal, outage).

Two triggers apply it to existing code, not only to new code: a version change of the dependency, and a second patch for the same failure class at one component. On the second patch, re-check the approach before writing a third.

## Grounded in

- [[compensation-is-designed-from-the-observed-failure-not-from-the-dependency-guarantees]] — supplies the mechanism this norm interrupts: the remedy is shaped to the failure seen, the dependency is never asked, and a canonized template spreads the compensation unexamined.

## Refutation

The principle fails its goal if wrappers built after this check still carry compensation a native guarantee would have made unnecessary, or if the check keeps finding nothing while later reviews keep finding native guarantees that were missed.
