---
name: a-gate-on-form-is-checked-against-the-property-it-stands-for
description: A gate that matches the form of an output (marker on the first line, byte-exact text) rejects correct work when the form drifts without the property changing.
type: reference
schema: principle/v1
generality: 1
grounded_in: [regularity:a-format-varies-while-the-property-it-stands-for-holds]
created: 2026-10-21
last_verified: 2026-10-21
---

# A gate on form is checked against the property it stands for

## Principle

Because a format varies while the property it stands for holds ([[a-format-varies-while-the-property-it-stands-for-holds]]), for the goal of a gate that admits exactly the work with the property it guards, when a gate tests format as a proxy for a property, either test the property, or normalize the drift classes that do not change it and say which are tolerated; the work around a false rejection (manual re-verify, unblock) is a cost to count when choosing the gate.

## Goal class

Gates that admit exactly the work that has the property they guard, without false rejections whose manual workaround costs more than the gate saves.

## Generality

Level 1 - holds for gates over agent output with a recoverable proxy; two instances only.

## Grounded in

- [[a-format-varies-while-the-property-it-stands-for-holds]] — The format a gate tests as a proxy for a property drifts — a marker moves off line 1, whitespace or rendering bytes change — while the property the format stands for stays intact, so a format test rejects work that satisfies the property.

## Refutation

The principle fails its goal if gates built on the property or on normalized form keep falsely rejecting work that has the property, or start admitting work that lacks it.
