---
name: a-generic-failure-message-names-a-surface-shared-by-many-causes
description: A generic failure message (hang, auth failure, file not found, connection reset) names a surface reached by several unrelated causes on different axes; the message alone does not select among them, and a remedy aimed at the wrong one can deepen the failure.
type: reference
schema: regularity/v1
generality: 2
induced_from: [2026-09-27-claude-code-cli-2-1-197-cpu-hang-pin-2-0-30, 2026-07-01-non-interactive-external-deploy-from-agent-bash, 2026-08-27-bash-background-cd-chain-cwd-reset, 2026-05-29-logical-pwd-self-locating-script, 2026-07-01-ssh-agent-forward-mosh-orphan-loop-flapping]
created: 2026-10-27
last_verified: 2026-10-27
---
# A generic failure message (hang, auth failure, file not found, connection reset) names a surface reached by several unrelated causes on different axes

## Regularity

A generic failure message — a hang, an authentication failure, a "not found", a reset connection — names the surface where the failure became visible, and that surface is reached by several unrelated causes on different axes: the working directory, the tool version, the credential channel, the lifetime of a process. The message alone does not select among them, and a remedy aimed at the wrong cause can leave the symptom in place or make it worse.

## Generality

Level 2 — ranges over failure symptoms whose message does not carry its cause. It is not claimed for a diagnostic that names its cause precisely (a typed error with the failing argument).

## Refutation

A generic failure message observed over many occurrences that always had the same single cause would show that for that message the surface does select the cause.

## Induced from

- [[2026-09-27-claude-code-cli-2-1-197-cpu-hang-pin-2-0-30]] - a hang read as a network or auth failure was an unpinned CLI release spinning the CPU.
- [[2026-07-01-non-interactive-external-deploy-from-agent-bash]] - empty variable, 'password not supported' and 'project not found' each invited a destructive or repeated fix; the causes were a process boundary, a host-keyed credential and a missing pre-created resource.
- [[2026-08-27-bash-background-cd-chain-cwd-reset]] - a 'file not found' was a working-directory reset.
- [[2026-05-29-logical-pwd-self-locating-script]] - a symlink-loop error came from a logical path a script resolved back onto itself.
- [[2026-07-01-ssh-agent-forward-mosh-orphan-loop-flapping]] - successive fixes accumulated orphan loops and made the flapping worse.
