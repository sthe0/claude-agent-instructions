---
name: a-value-written-to-an-external-channel-is-cleaned-at-emission
description: A secret or internal identifier that leaves the agent through an argument list, a log or a public artifact stays leaked even when the sink's gate blocks it or the emitter is hardened later.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-07-08-secret-on-argv-curl-config-stdin, 2026-07-28-agent-emitted-value-leaks-org-key-into-public-history]
created: 2026-10-21
last_verified: 2026-10-21
---

# A value written to an external channel is cleaned at emission

## Principle

Emit a value into any external channel only after it has been reduced to what the channel may carry (feed secrets through a stream, not an argument; neutralize identifiers before they reach public history); treat the sink's gate as a backstop, keep the sibling fields the gate did not object to, and rotate any secret already logged.

## Generality

Level 2 - a rule for every value an agent writes outside its own session.

## Induced from

- [[2026-07-08-secret-on-argv-curl-config-stdin]] - a token on a command line landed in endpoint logs and a ticket; the emitter was hardened, and the already-logged token needed rotation.
- [[2026-07-28-agent-emitted-value-leaks-org-key-into-public-history]] - a trailer carried an internal key toward public history; the gate blocked it and the old workaround dropped a harmless sibling value.

## Refutation

Refuted by a channel whose later cleanup (hardening or redaction downstream) removed a leaked value completely with no rotation or loss, or by a sink gate that reliably preserved the clean fields. Two sources only.
