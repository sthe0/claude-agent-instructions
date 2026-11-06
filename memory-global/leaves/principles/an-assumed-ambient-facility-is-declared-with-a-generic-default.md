---
name: an-assumed-ambient-facility-is-declared-with-a-generic-default
description: A tool that works on its author's host because of an ambient facility (a kernel filesystem, an internal service, a detected backend, a shared config file) breaks silently elsewhere; the facility must be a declared, opt-in dependency with a generic default.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:a-tool-relies-on-facilities-its-authors-host-supplies-unasked]
grounding_judged: d85be6ae83635dbbb5abf36c3b9c68a9933026f0bf458b54f8d8e247c079cff1
grounding_override: Stamp: the leaf is not edited in this conversion and its grounded_in is fixed as the leaf states it; the judge finds the listed grounds support the central rule but not every clause, which a later curator split of the leaf can address.
created: 2026-10-21
last_verified: 2026-10-21
---

# An assumed ambient facility is declared with a generic default

## Principle

Because a tool relies on facilities its authors' host supplies unasked ([[a-tool-relies-on-facilities-its-authors-host-supplies-unasked]]), for the goal of tools that run on other hosts, for every dependency that the author's environment supplies without being asked, either test the tool where the facility is absent or make the dependency explicit: a generic default in the shared layer, the environment-specific choice in a per-machine file that is not shared, and a dividing line drawn by reachability from outside the boundary rather than by what looks local.

## Goal class

Tools that run on hosts other than their authors', failing — when a facility is missing — with a named dependency rather than an unexplained error.

## Generality

Level 2 - a rule for any tool distributed beyond the host it was written on.

## Grounded in

- [[a-tool-relies-on-facilities-its-authors-host-supplies-unasked]] — A tool built on one machine relies on facilities that machine supplies without being asked — a GNU coreutil, a pre-installed launcher, an org-internal service — and fails on a host that lacks them, with nothing in the tool naming the dependency.

## Refutation

The principle fails its goal if tools whose ambient dependencies were declared with generic defaults still fail on a foreign host with an undeclared dependency.
