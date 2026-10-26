---
name: a-child-process-shares-its-parent-ambient-root-until-isolated-explicitly
description: A child process or installed tool that runs with the ambient environment of its parent re-triggers the parent's machinery or collides with the parent's config; isolating by relocating its root works only if what the old root carried in files (credentials, model selection) is supplied to the new one.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:a-child-process-reads-its-parents-ambient-root]
created: 2026-10-21
last_verified: 2026-10-21
---

# A child process shares its parent ambient root until isolated explicitly

## Principle

Because a child process reads its parent's ambient root ([[a-child-process-reads-its-parents-ambient-root]]), for the goal of children that neither recurse nor clobber and keep their login, when a process is launched by an agent or installed beside a personal setup, decide its root explicitly: relocate it with the tool's own root variable from one single-source seam, then enumerate what the old root held in files and re-supply it; otherwise the unisolated child recurses or clobbers, and the isolated one silently loses its login. Pair the isolation with an alarm, because a caller that tolerates failure shows neither outcome.
## Goal class

Child processes and side-by-side installations that neither recurse into nor clobber their parent's setup and keep the credentials they need, with any failure of either kind visible.

## Generality

Level 2 - a rule for any child process or co-installed tool that reads configuration from an ambient location.

## Grounded in

- [[a-child-process-reads-its-parents-ambient-root]] — A child process launched by an agent or installed beside a personal setup reads the parent's ambient configuration root (settings, hooks, credentials) unless relocated; relocated, it loses whatever that root held, including its login.

## Refutation

The principle fails its goal if children relocated from one seam with their root's files re-supplied still recurse, clobber or lose their credentials, or if such a failure stays silent despite the alarm.
