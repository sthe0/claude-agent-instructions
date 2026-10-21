---
name: an-assumed-ambient-facility-is-declared-with-a-generic-default
description: A tool that works on its author's host because of an ambient facility (a kernel filesystem, an internal service, a detected backend, a shared config file) breaks silently elsewhere; the facility must be a declared, opt-in dependency with a generic default.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-07-01-2026-07-01-engine-spawn-proc-tree-macos-and-rm-empty-var, 2026-06-29-org-portable-core-internal-coupling-opt-in, 2026-07-31-second-agent-cli-launcher-seam, 2026-06-27-stub-mechanism-not-deployable-until-caller-cli-onboarding]
created: 2026-10-21
last_verified: 2026-10-21
---

# An assumed ambient facility is declared with a generic default

## Principle

For every dependency that the author's environment supplies without being asked, either test the tool where the facility is absent or make the dependency explicit: a generic default in the shared layer, the environment-specific choice in a per-machine file that is not shared, and a dividing line drawn by reachability from outside the boundary rather than by what looks local.

## Generality

Level 2 - a rule for any tool distributed beyond the host it was written on.

## Induced from

- [[2026-07-01-2026-07-01-engine-spawn-proc-tree-macos-and-rm-empty-var]] - the spawn cleanup read a Linux-only process filesystem and failed every spawn elsewhere.
- [[2026-06-29-org-portable-core-internal-coupling-opt-in]] - internal-only facilities were coupled in; the fix was a generic default with internal opt-in.
- [[2026-07-31-second-agent-cli-launcher-seam]] - ambient workspace detection picked the wrong backend outside its home tree.
- [[2026-06-27-stub-mechanism-not-deployable-until-caller-cli-onboarding]] - a 'per-machine' flag lived in a git-shared file and so varied by nothing.

## Refutation

Refuted by a tool that ran on a second host with the author's ambient facilities removed and failed in a way unrelated to any undeclared dependency.
