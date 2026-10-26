---
name: a-tool-relies-on-facilities-its-authors-host-supplies-unasked
description: A tool built on one machine relies on facilities that machine supplies without being asked — a GNU coreutil, a pre-installed launcher, an org-internal service — and fails on a host that lacks them, with nothing in the tool naming the dependency.
type: reference
schema: regularity/v1
generality: 2
induced_from: [2026-07-01-2026-07-01-engine-spawn-proc-tree-macos-and-rm-empty-var, 2026-06-29-org-portable-core-internal-coupling-opt-in, 2026-07-31-second-agent-cli-launcher-seam, 2026-06-27-stub-mechanism-not-deployable-until-caller-cli-onboarding]
created: 2026-10-27
last_verified: 2026-10-27
---
# A tool built on one machine relies on facilities that machine supplies without being asked

## Regularity

A tool built and tested on its authors' machine relies on facilities that machine supplies without being asked — a GNU flavour of a core utility, a process-tree command, a pre-installed agent launcher, an organization-internal service reached as if it were local. On a host without the facility the tool fails, and nothing in the tool names the dependency, because on the authors' host it was never something to name.

## Generality

Level 2 — ranges over tools that run on hosts other than the ones they were built on. It is not claimed for a tool shipped only to one fixed, identical environment.

## Refutation

Tools observed moved to hosts lacking their authors' ambient facilities that ran unchanged with no undeclared dependency surfacing would show the reliance does not arise for that kind of tool.

## Induced from

- [[2026-07-01-2026-07-01-engine-spawn-proc-tree-macos-and-rm-empty-var]] - the spawn cleanup read a Linux-only process filesystem and failed every spawn elsewhere.
- [[2026-06-29-org-portable-core-internal-coupling-opt-in]] - internal-only facilities were coupled in; the fix was a generic default with internal opt-in.
- [[2026-07-31-second-agent-cli-launcher-seam]] - ambient workspace detection picked the wrong backend outside its home tree.
- [[2026-06-27-stub-mechanism-not-deployable-until-caller-cli-onboarding]] - a 'per-machine' flag lived in a git-shared file and so varied by nothing.
