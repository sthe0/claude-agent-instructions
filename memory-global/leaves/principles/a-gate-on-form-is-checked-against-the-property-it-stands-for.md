---
name: a-gate-on-form-is-checked-against-the-property-it-stands-for
description: A gate that matches the form of an output (marker on the first line, byte-exact text) rejects correct work when the form drifts without the property changing.
type: reference
schema: principle/v1
generality: 1
induced_from: [2026-06-24-developer-marker-not-on-line-1-false-block, 2026-08-26-plan-delivery-gate-exact-content-match]
created: 2026-10-21
last_verified: 2026-10-21
---

# A gate on form is checked against the property it stands for

## Principle

When a gate tests format as a proxy for a property, either test the property, or normalize the drift classes that do not change it and say which are tolerated; the work around a false rejection (manual re-verify, unblock) is a cost to count when choosing the gate.

## Generality

Level 1 - holds for gates over agent output with a recoverable proxy; two instances only.

## Induced from

- [[2026-06-24-developer-marker-not-on-line-1-false-block]] - a correct specialist result was blocked because the marker was not on the first line; the fix extracted the marker with a closed-set model pass.
- [[2026-08-26-plan-delivery-gate-exact-content-match]] - a hand-retyped plan message with added backticks failed the exact-content gate and denied the approval ask.

## Refutation

Refuted by a form gate that never rejected correct work over a sample of cases, or by cases where normalizing the form admitted incorrect work that the strict gate had caught.
