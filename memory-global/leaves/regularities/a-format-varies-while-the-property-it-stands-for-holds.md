---
name: a-format-varies-while-the-property-it-stands-for-holds
description: The format a gate tests as a proxy for a property drifts — a marker moves off line 1, whitespace or rendering bytes change — while the property the format stands for stays intact, so a format test rejects work that satisfies the property.
type: reference
schema: regularity/v1
generality: 1
induced_from: [2026-06-24-developer-marker-not-on-line-1-false-block, 2026-08-26-plan-delivery-gate-exact-content-match]
created: 2026-10-27
last_verified: 2026-10-27
---
# The format a gate tests as a proxy for a property drifts

## Regularity

The surface form that stands for a property drifts independently of the property: a marker is written on line 3 instead of line 1, a delivered rendering differs from the registered one by whitespace or a client's re-encoding, and the property the form was chosen to indicate — the specialist did declare its outcome, the user did receive the plan — holds unchanged. A test of the form therefore rejects work that satisfies the property, and each such false rejection is paid for by a manual re-verification or an unblock.

## Generality

Level 1 — ranges over gates that test a textual or structural form as a proxy for a semantic property. It is not claimed for a gate whose form IS the property (a schema whose exact shape a parser consumes).

## Refutation

A form-testing gate observed over real traffic with no false rejections — every form mismatch it rejected also lacked the property — would show the drift does not occur for that form.

## Induced from

- [[2026-06-24-developer-marker-not-on-line-1-false-block]] - a correct specialist result was blocked because the marker was not on the first line; the fix extracted the marker with a closed-set model pass.
- [[2026-08-26-plan-delivery-gate-exact-content-match]] - a hand-retyped plan message with added backticks failed the exact-content gate and denied the approval ask.
