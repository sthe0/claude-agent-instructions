---
name: entity-claim-verified-against-the-entity-itself
description: A load-bearing claim about a specific named entity (which platform it runs on, which convention its own directory documents, which layer of a chain actually binds, what a cited source says) is confirmed by a direct check on THAT entity, never by resemblance to a similar one or by the mere existence of a source.
type: reference
schema: principle/v1
generality: 2
domain: planning
induced_from: [doubt-own-snapshot]
created: 2026-08-22
last_verified: 2026-08-22
---

## Principle

To achieve confidence in a claim about a **specific named entity** — which platform it runs on,
which directory's convention governs an edit, which layer of a multi-layer limit actually binds,
what a cited source actually contains, which unit a complaint was measured in — do not accept
resemblance to a similar entity, or the bare existence of *a* source, as confirmation. Spend the
cheapest available **direct check against that specific entity** (grep the actual tree, re-read
the actual cited ticket, read the target directory's own README, trace the real end-to-end call
chain) before the claim is allowed into a plan, a report, or a dispatched instruction. Plausibility
by proximity is not evidence, however cheap it is to reach for.

## Generality

Level 2 — a task class: any planning or reporting act that commits a specific-entity claim to an
artifact someone else will rely on (a plan stage, a ticket comment, a dispatch's constraints, a
done-criterion). It ranges over at least four different kinds of object sharing one failure shape:

- **domain/platform attribution** — does agent X actually run on platform Y;
- **delivery convention** — does directory X actually follow convention Y;
- **binding-constraint identification** — does layer X actually bind, among several plausible layers;
- **source attribution / unit** — does cited source X actually say Y, in the unit the reader meant.

In each case a plausible-but-unverified inference reached the artifact instead of a direct check
on the named entity — the shared functional ground, not the surface topic, is what recurs.

## Induced from

Four instances inside one project ticket's planning lifecycle (a single multi-day session on one
task) — none individually met `principle-promotion-threshold`'s Σ-occurrence bar, because the
recurring element is the *shape*, not the object, so induction happens directly from the session
rather than through `record-experience.py promote-scan`:

- misattributed a dataset's source ticket to a topically-similar sibling ticket, before re-reading
  the actual cited ticket and finding a different one;
- reached for a memory leaf about a different internal platform on the surface match "both are
  forks of the same upstream agent framework," before checking which platform the specific agent
  under investigation actually runs on — caught by the user, not self-caught; see [[doubt-own-snapshot]]
  for the parallel case of a stale *own* snapshot, which this instance is adjacent to but not identical
  to (here the wrong source was never one's own — it was a plausible neighbour never checked at all);
- drafted a dispatch instruction for editing a vendored third-party fork without first reading that
  fork's own documented delivery convention;
- kept a plan stage guarding a correctly-sourced client-SDK timeout without tracing whether that
  specific layer was ever the operative constraint in the real request chain — it was not; an
  infrastructure timeout bound first, one hop earlier in the same chain.

## Refutation

Refuted, or driven broader, by a case where the direct-check cost genuinely exceeds the expected
value of catching the error it would surface — e.g. an entity nested so deep that "just check it"
would itself require the very investigation this principle exists to short-circuit. At that point
the principle needs an explicit proportionality clause (check when direct-check cost is low
relative to the claim's blast radius) rather than an unconditional "always check the entity."

## See also

- [[doubt-own-snapshot]] — the after-a-mismatch-is-noticed companion: once a contradiction between
  a requirement and your own observation is *felt*, refresh your own source before doubting the
  requirement. This principle is the gate one step earlier: it governs claims that never triggered
  a felt contradiction at all, because the unchecked, proximate answer resembled the true one
  closely enough to pass without friction.
- [[verdict-covers-the-evidence-domain-it-claims]] — related in spirit (honest evidence coverage)
  but a different subject: that principle is about a *mechanism's* verdict covering the evidence
  domain it claims; this one is about a *model's own authoring habit*, before any mechanism runs.
- planner `SKILL.md` § "Framework-capability discovery" — the pre-existing instance of this same
  principle, scoped to one claim type ("a platform capability is absent is an evidence-bearing
  claim, not a default"). This leaf generalizes that sentence's shape to entity-claims at large.
- planner `SKILL.md` § "Numbers and deadlines without a source" — the same shape applied to numeric
  claims specifically.
