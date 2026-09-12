# ADR-0006: Premise invalidation scope

- Status: Accepted 2026-08-18
- Plan: `premise-loop-determinize-r2.toml` (stages 2–9)

## Context

Plan `premise-loop-determinize` set out to make the pre-approval premise machinery converge: a
single edit to a single plan element should invalidate only what it actually changed, a raised
question should name the control its answer could flip, a review should bind to the scope it
actually read, an accepted risk should discharge without an edit, the pre-approval phase should
carry an effort limiter the way every other phase already does, and the delivery gate's escape
channel should be reachable without weakening the case it protects. Stages 2–8 landed the seven
independent changes this required. This ADR is stage 9's record of four invariants the resulting
code states only in a local comment at its own site: it gathers them where a reader touching one
digest function can see the other three, records for each the failure it prevents — since a rule
stated without its failure is indistinguishable from an arbitrary choice — and carries the
alternatives weighed and rejected while making them, which the code does not.

Three of the four invariants come from the same migration: the premise/enumeration machinery moved
from one whole-plan digest to per-part digests (meta + one per stage) without breaking any state a
session had already written under the old shape. The fourth is unrelated in mechanism but shares
the same underlying discipline — a fail-open judgement is safe only because the irreversible act it
guards is fail-closed one layer further in.

## Decision

### Invariant 1 — absent-field digest identity (introduced in `1bf44b2`, pre-branch; preserved by stage 2, toml line ~201)

`plan.stage_question_key(stage, element=None)` only ever contributes `verify_venue_at_final` to its
hash payload when the stage actually declares it:

```python
*((_normalize_string(stage.criterion.verify_venue_at_final),) if stage.criterion.verify_venue_at_final else ())
```

never as `... or ""`. `Question.disposed_at_key` persists this digest and compares it across
processes at the `plan_approval` gate. A plan authored before `verify_venue_at_final` existed has
no such field to contribute either way — the failure this invariant prevents is that contributing
`""` in its place would change the digest EVERY such plan produces, which would flip every already
-dispositioned question of every live session, on every plan predating the field, to a spurious
"stage definition changed" blocker the moment the engine started reading a field that plan never
had. The rule generalizes to every field added to the hash payload after the digest scheme was
first fixed: contribute only when declared, never coerce absence to an empty contribution.

**Rejected alternative:** a schema-version tag on the digest, bumped whenever a new field joins the
payload, with old-version questions grandfathered wholesale. Rejected because it discharges
EVERYTHING a pre-existing question ever bound to, not just the one field that changed shape — the
same over-wide invalidation element scoping (stage 2's own headline change) exists to remove.

### Invariant 2 — the two-branch migration acceptance rule (stage 3, toml line ~325)

`plugins_premise.stale_enumeration_parts(bag, doc)` branches on whether the bag carries per-part
enumeration digests at all:

- **No per-part baseline** (a bag written before the per-part split, or never enumerated): the only
  available signal is the single legacy composite digest, so the function falls back to comparing
  `bag.get("enumerated_at")` against the whole-plan `plan.plan_content_digest(doc)`. A **match**
  proves nothing in the plan moved — which is exactly why accepting it clears every part rather
  than clearing nothing: a composite match is total information, not partial information read as
  total.
- **Per-part baseline present**: `plan.changed_parts(doc, baseline)` does the granular comparison
  the rest of the machinery relies on.

The failure this invariant prevents sits on the branch a naive migration gets wrong: reading an
empty per-part map as "no part enumerated, therefore nothing is stale" — rather than as "this bag
predates the per-part split and must fall back to the composite test" — would flip every
already-discharged live session's `_ENUMERATE_STALE` state to false on its very next call, silently
re-approving a cross-check that was never actually re-run against the parts that changed.

**Rejected alternative:** treat every legacy (no-per-part-digest) bag as unconditionally stale on
first contact after the split, forcing one mandatory full re-enumeration per live session. Rejected
because it spends the exact re-run cost stage 3 exists to remove, on every in-flight session,
merely for having been open across a deploy — the composite digest already answers the question
correctly when it still matches.

### Invariant 3 — composite-digest compatibility of the escape binding (stage 3, toml line ~325)

`plan.plan_content_digest(doc)` keeps producing byte-for-byte the same payload it produced before
the per-part split — order still spliced in (`+ order_place(...)`) rather than occupying a tuple
slot, which is what keeps a plan predating the order field on the same value it always produced.
Enumeration escapes, launch-window counters and every already-persisted `enumerated_at` bind to
this one value; stage 3's own expected-result image states the escape machinery is "unchanged in
behaviour: escapes still bind to one composite plan digest plus the launch and pass counters." The
failure this invariant prevents: a changed composite payload would silently void every live
session's escape and re-arm a cross-check that a user already spent an override on, discovered only
when the next `approve` unexpectedly demands another enumeration pass. `test_enumeration_keying`
pins the composite value for a fixture plan so a future edit to the payload construction fails a
test rather than a live session.

**Rejected alternative:** version the composite digest itself and carry a translation table from
old escapes to new. Rejected as strictly more moving parts for the same guarantee invariant 1
already rejected a version tag for — the byte-compatible construction makes a translation table
unnecessary rather than solving a problem it would otherwise need to.

### Invariant 4 — fail-open direction of the delivery classifier (stage 8, toml line ~852)

`hook-plan-delivery-gate.py`'s `decide()` computes `advisor.judge_approval_ask` before calling
`gate_decision(..., is_approval_ask=...)`. `is_approval_ask=False` short-circuits to an unverified
ALLOW — the receipt/freshness/delivery/marker checks apply only to an ask the classifier identifies
as the plan-approval ask, never to every `AskUserQuestion` at node `PLAN_READY`. An absent, slow or
malformed classifier call can therefore only WIDEN what passes through, never deny a question the
user needed answered.

This is safe specifically because the irreversible act the gate protects — recording approval — is
fail-**closed** one layer further in: `main()` stamps a delivery receipt only for the receipt
`decide()` hands back, which is set only when `gate_decision` returned `delivery_verified=True`,
never on a fail-open ALLOW ("or it would manufacture the proof it
exists to demand"), and `cmd_approve` refuses without that stamp regardless of what the hook let
through. The classifier fails toward "not the approval ask" because the act it protects fails
closed one layer down — inverting the failure direction at the live-turn layer only works because
the act it gates does not inherit that inversion.

**Rejected alternative:** a second, coordinator-supplied "this is the approval ask" marker,
considered and rejected for the same reason a marker alone cannot serve this role at all — the
coordinator supplies it, so it proves only that the coordinator SAID this is the approval ask,
never that it IS one. The model judgement over the ask's own text was kept as the sole classifier
instead.

## Deliberately not done

1. **A migration cutover date** after which legacy (no-per-part) enumeration bags are rejected
   outright — not implemented; invariant 2's fallback branch stays live indefinitely rather than on
   a schedule, since nothing in the engine currently needs the per-part shape to be universal.
2. **Extending element-scoped digests to the `order`/`requirements`/`control` elements** — these
   three have no stage field of their own and still fall back to the rest-of-stage definition
   (stage 2's own scope); a finer split was not attempted.
3. **Splitting the pre-approval round budget from `effort-replan-absolute`** — stage 7 reused the
   existing Rule-of-Three config key rather than adding a dedicated one; stage 7's own refutation
   names the observable (the release firing on most substantive plans) that would justify the
   split, which does not yet exist.

## Consequences

- No engine behavior changes as a result of this stage; it is a documentation-only record.
- A later change to any of the three enumeration/premise digest functions (`stage_question_key`,
  `plan_content_digest`, `stale_enumeration_parts`) should re-check this ADR's invariants before
  landing: each site carries a local comment, but none carries the other two invariants' reasoning
  or the rejected alternatives.
- The fail-open/fail-closed pairing (invariant 4) is the pattern to reuse for any future advisory
  judge gating a `PLAN_READY`-adjacent hook: judge fails open on the cheap, retryable decision;
  the irreversible act one layer down stays fail-closed.

## See also

- [scripts/agentctl/README.md](../../scripts/agentctl/README.md) — command rows and prose sections
  for every mechanism this ADR documents.
- [question-provenance-gate.md](../../memory-global/leaves/question-provenance-gate.md) — the
  premise gate's own honest-ceiling record, updated alongside this ADR.
- [ask-user-question-split-turn.md](../../memory-global/leaves/ask-user-question-split-turn.md) —
  the delivery gate's record, updated for invariant 4's classifier.
