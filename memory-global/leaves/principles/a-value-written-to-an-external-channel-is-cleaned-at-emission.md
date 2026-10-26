---
name: a-value-written-to-an-external-channel-is-cleaned-at-emission
description: A secret or internal identifier that leaves the agent through an argument list, a log or a public artifact stays leaked even when the sink's gate blocks it or the emitter is hardened later.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:a-value-emitted-to-an-external-channel-persists-beyond-recall]
created: 2026-10-21
last_verified: 2026-10-21
---

# A value written to an external channel is cleaned at emission

## Principle

Because a value emitted to an external channel persists beyond recall ([[a-value-emitted-to-an-external-channel-persists-beyond-recall]]), for the goal of keeping secrets and non-public identifiers out of copies the writer cannot reach, emit a value into any external channel only after it has been reduced to what the channel may carry (feed secrets through a stream, not an argument; neutralize identifiers before they reach public history); treat the sink's gate as a backstop, keep the sibling fields the gate did not object to, and rotate any secret already logged.
## Goal class

No secret or non-public identifier reaching a copy outside the writer's control through an external channel.

## Generality

Level 2 - a rule for every value an agent writes outside its own session.

## Grounded in

- [[a-value-emitted-to-an-external-channel-persists-beyond-recall]] — A value written to an external channel — a process argument list, a public commit history, a published ticket — is copied, logged or indexed beyond the writer's reach as it lands; later removal at the sink does not recall the copies.

## Refutation

The principle fails its goal if values cleaned at emission still turn up, in their uncleaned form, in a log, an argv trace or public history.
