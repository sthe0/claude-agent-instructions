---
name: ssh-stale-forwarded-agent-sock-hang
description: git fetch/push to github silently hangs forever (no error) when SSH_AUTH_SOCK points through ~/.ssh/ssh_auth_sock to a forwarded agent socket of a dead ssh session — the handshake completes and auth wedges at the agent query; fix env -u SSH_AUTH_SOCK (key is on disk), heals on the user's next login.
type: reference
schema: leaf/v1
created: 2026-07-03
last_verified: 2026-07-03
---

# Stale forwarded SSH agent socket hangs git auth silently

## Difficulty
`git fetch`/`push` to github.com hangs indefinitely with **zero output** — not a timeout error, a silent wedge — even though TCP to the remote connects fine. Diagnosis is misleading: it looks like a network/proxy problem. The real cause: `SSH_AUTH_SOCK` points (via the login-refreshed symlink `~/.ssh/ssh_auth_sock`) to a **forwarded agent socket of an ssh session that has died** (`/tmp/ssh-*/agent.*`). The socket still accepts connections, so ssh completes the transport handshake and then blocks forever inside the agent query at the publickey step (`ssh -vv` stalls right after `Next authentication method: publickey`; `ssh-add -l` also hangs). Cost paid on 2026-07-03: ~10 min isolating this from a suspected git/proxy/wrapper issue mid-task.

## Guidance
- Signature check (fast): `timeout 5 ssh-add -l` — a hang (exit 124) rather than an instant listing/refusal confirms a wedged agent socket.
- Workaround when the identity is an on-disk key (`IdentityFile` in `~/.ssh/config`, e.g. `~/.ssh/id_ed25519_github`): run the operation with the agent removed from env — `env -u SSH_AUTH_SOCK git fetch/push`. With `IdentitiesOnly yes` the key file authenticates fine without the agent.
- If a plan's `verify_command`/`final_check` embeds a `git fetch`, do not rewrite the check — invoke the engine itself under `env -u SSH_AUTH_SOCK` so the embedded fetch inherits the sanitized env.
- Do NOT delete or repoint `~/.ssh/ssh_auth_sock` — the user's login machinery owns it and refreshes the symlink on the next connection, which is the durable heal.
- Verified by: OpenSSH 9.6p1 / Ubuntu, host the0 (Linux 5.4), 2026-07-03 — verbose log stall at publickey + `env -u` immediately succeeding.

## See also
- [[ssh-controlmaster-group-cache]] — the sibling stale-multiplexing trap (old group membership via a reused ControlMaster session).
