---
name: an-action-on-shared-state-names-its-object-and-leaves-others-untouched
description: An operation that assumes it owns the state it touches ('whatever stash is on top', 'pull now', 'the variable's directory') damages state that belongs to someone else when the assumption fails.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:a-name-from-position-or-an-empty-variable-designates-a-foreign-object, regularity:a-shared-resource-moves-under-others-action]
created: 2026-10-21
last_verified: 2026-10-21
---

# An action on shared state names its object and leaves others untouched

## Principle

Because a name from position or an empty variable designates a foreign object ([[a-name-from-position-or-an-empty-variable-designates-a-foreign-object]]) and a shared resource moves under others' action ([[a-shared-resource-moves-under-others-action]]), for the goal of acting on exactly the intended object, before a mutating act on a resource other sessions or tasks may share (a stash, a working tree, a branch, a directory built from a variable), name the exact object by identity, guard the conditions under which the name is empty or foreign, and prefer an offer to a user over a silent background mutation; recover by isolating onto a pinned clean base rather than editing in place.

## Goal class

Mutating actions on shared resources that change exactly their intended object and leave every other session's or task's objects untouched.

## Generality

Level 2 - a rule for any write to a resource with more than one potential owner.

## Grounded in

- [[a-name-from-position-or-an-empty-variable-designates-a-foreign-object]] — A name for the object of an action that is built from position (top of a stack, current branch) or from a variable that can be empty resolves, under shared use, to an object belonging to someone else or to a parent of the intended one.
- [[a-shared-resource-moves-under-others-action]] — A resource other sessions or tasks also act on — a working tree, a trunk branch, a test suite's population, a session-state schema — changes between two observations by one task for reasons unrelated to that task.

## Refutation

The principle fails its goal if actions that name their object by identity and guard empty or foreign names still mutate another session's object.
