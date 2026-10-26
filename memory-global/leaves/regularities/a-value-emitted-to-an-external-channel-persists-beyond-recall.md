---
name: a-value-emitted-to-an-external-channel-persists-beyond-recall
description: A value written to an external channel — a process argument list, a public commit history, a published ticket — is copied, logged or indexed beyond the writer's reach as it lands; later removal at the sink does not recall the copies.
type: reference
schema: regularity/v1
generality: 2
induced_from: [2026-07-08-secret-on-argv-curl-config-stdin, 2026-07-28-agent-emitted-value-leaks-org-key-into-public-history]
created: 2026-10-27
last_verified: 2026-10-27
---
# A value written to an external channel

## Regularity

A value written into an external channel — a process argument list visible to every local user, a commit pushed into public history, a field published to a ticket or a log — is copied, logged or indexed beyond the writer's reach the moment it lands. Removing it afterwards at the sink does not recall those copies, and a gate at the sink that refuses one field leaves the sibling fields of the same emission in place.

## Generality

Level 2 — ranges over channels the writer does not control after emission. It is not claimed for a private, writer-owned store that keeps no copies.

## Refutation

An external channel observed to keep no copy of an emitted value after its removal at the sink — no log, index, mirror or argv trace retaining it — would show the persistence does not hold for that channel.

## Induced from

- [[2026-07-08-secret-on-argv-curl-config-stdin]] - a token on a command line landed in endpoint logs and a ticket; the emitter was hardened, and the already-logged token needed rotation.
- [[2026-07-28-agent-emitted-value-leaks-org-key-into-public-history]] - a trailer carried an internal key toward public history; the gate blocked it and the old workaround dropped a harmless sibling value.
