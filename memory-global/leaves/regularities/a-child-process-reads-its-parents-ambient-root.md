---
name: a-child-process-reads-its-parents-ambient-root
description: A child process launched by an agent or installed beside a personal setup reads the parent's ambient configuration root (settings, hooks, credentials) unless relocated; relocated, it loses whatever that root held, including its login.
type: reference
schema: regularity/v1
generality: 2
induced_from: [2026-08-25-unisolated-judge-subprocess-recursion, 2026-07-01-isolate-tool-config-root-per-root-auth]
created: 2026-10-27
last_verified: 2026-10-27
---
# A child process launched by an agent or installed beside a personal setup reads the parent's ambient configuration root (settings, hooks, credentials) unless relocated

## Regularity

A child process launched by an agent, or a tool installed beside a personal setup, reads its configuration from the same ambient root as its parent — the same settings, hooks and credential store — unless its root is relocated: an unisolated judge subprocess runs the parent's hooks and recurses into itself, an unisolated second installation clobbers the first. Relocated, it starts from an empty root and silently loses everything the old root held, its login included. Both outcomes are invisible to a caller that tolerates the child's failure.

## Generality

Level 2 — ranges over child processes of tools that resolve a configuration root from the environment. It is not claimed for a child that takes its whole configuration on its command line.

## Refutation

Unisolated child processes observed over many launches that never read or wrote the parent's root, or relocated ones that kept their credentials without any re-supply, would show the coupling does not hold for that tool.

## Induced from

- [[2026-08-25-unisolated-judge-subprocess-recursion]] - single-turn judge children loaded the fleet's own hooks and recursed; isolating the config root then severed their file-carried login.
- [[2026-07-01-isolate-tool-config-root-per-root-auth]] - an install into the shared config dir clobbered a personal config; the fix was a separate root, with authentication being per-root.
