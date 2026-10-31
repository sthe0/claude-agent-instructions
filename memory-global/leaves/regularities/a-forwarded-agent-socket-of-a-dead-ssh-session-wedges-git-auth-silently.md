---
name: a-forwarded-agent-socket-of-a-dead-ssh-session-wedges-git-auth-silently
description: A forwarded ssh-agent socket left by a dead ssh session still accepts connections, so git over ssh hangs silently at the publickey step and looks like a network fault
type: reference
schema: regularity/v1
generality: 1
induced_from: [ssh-stale-forwarded-agent-sock-hang]
rule_derived: "lifted from ssh-stale-forwarded-agent-sock-hang; no experience leaf backs it (verified there on OpenSSH 9.6p1, Ubuntu, 2026-07-03)"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-forwarded-agent-socket-of-a-dead-ssh-session-wedges-git-auth-silently

## Regularity

When SSH_AUTH_SOCK points, via the login-refreshed symlink ~/.ssh/ssh_auth_sock, at the forwarded agent socket (/tmp/ssh-*/agent.*) of an ssh session that has died, the socket still accepts connections, so a git fetch or push to github.com completes the transport handshake and then blocks forever inside the agent query at the publickey step, printing nothing, while TCP to the remote is fine; `ssh -vv` stalls right after `Next authentication method: publickey` and `ssh-add -l` also hangs. The failure therefore looks like a network or proxy fault. With the identity held as an on-disk IdentityFile and `IdentitiesOnly yes`, the same operation authenticates when the agent is removed from the environment, and the symlink is rewritten by the user's login machinery on the next connection.

## Generality

Level 1 — ranges over git over ssh from a machine whose SSH_AUTH_SOCK is a login-refreshed link to a forwarded agent socket. It is deliberately not claimed for hosts with a local agent or without agent forwarding, nor for hangs that stall before the publickey step.

## Induced from

- [[ssh-stale-forwarded-agent-sock-hang]] — contributes the observed case behind the statement: a silent git fetch hang mid-task, isolated to a dead session's agent socket and cleared by removing the agent from the environment.

## Refutation

Observing a git fetch to github.com that hangs with SSH_AUTH_SOCK pointing at a dead forwarded agent socket yet does not stall at the publickey step, or one where `env -u SSH_AUTH_SOCK` with an on-disk IdentityFile and IdentitiesOnly yes still hangs, or `ssh-add -l` returning instantly against such a socket, would show the statement false.
