---
name: a-generic-failure-symptom-is-traced-to-its-cause-before-a-remedy-is-applied
description: Errors such as 'file not found', 'hangs', 'password authentication unsupported' or 'keys fall off' name the symptom's surface, not its cause, and invite a remedy on the wrong axis that is sometimes destructive.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-09-27-claude-code-cli-2-1-197-cpu-hang-pin-2-0-30, 2026-07-01-non-interactive-external-deploy-from-agent-bash, 2026-08-27-bash-background-cd-chain-cwd-reset, 2026-05-29-logical-pwd-self-locating-script, 2026-07-01-ssh-agent-forward-mosh-orphan-loop-flapping]
created: 2026-10-21
last_verified: 2026-10-21
---

# A generic failure symptom is traced to its cause before a remedy is applied

## Principle

Before acting on a generic failure message, state at least two causes on different axes (working directory, version, credential channel, process lifetime), find the observation that separates them without changing state, and choose the least destructive remedy that addresses the confirmed one; a remedy that makes the symptom worse is a signal that the cause was misjudged.

## Generality

Level 2 - a rule for diagnosing any failure whose message does not mention the broken thing.

## Induced from

- [[2026-09-27-claude-code-cli-2-1-197-cpu-hang-pin-2-0-30]] - a hang read as a network or auth failure was an unpinned CLI release spinning the CPU.
- [[2026-07-01-non-interactive-external-deploy-from-agent-bash]] - empty variable, 'password not supported' and 'project not found' each invited a destructive or repeated fix; the causes were a process boundary, a host-keyed credential and a missing pre-created resource.
- [[2026-08-27-bash-background-cd-chain-cwd-reset]] - a 'file not found' was a working-directory reset.
- [[2026-05-29-logical-pwd-self-locating-script]] - a symlink-loop error came from a logical path a script resolved back onto itself.
- [[2026-07-01-ssh-agent-forward-mosh-orphan-loop-flapping]] - successive fixes accumulated orphan loops and made the flapping worse.

## Refutation

Refuted by failures of this kind where acting on the surface message resolved the cause at once and no cheaper axis was missed, so that the extra hypothesis step was pure overhead across a sample of such cases.
