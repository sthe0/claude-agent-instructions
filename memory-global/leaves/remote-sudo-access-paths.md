---
name: Remote sudo fast-paths for Claude sessions
description: Cheapest ways to get sudo access on a remote host through Claude when you can ssh as one user but the target work is in another user's space — pick by task duration.
type: reference
created: 2026-05-29
last_verified: 2026-05-29
---

# Remote sudo fast-paths

When the task touches another user's space on a remote host (e.g. `the0` ssh'es to `the0.fun`, but the files live under `/home/nick`), you need elevated access. Claude's `!`-spawned shell does NOT allocate a TTY, so interactive `sudo` prompts fail (`Pseudo-terminal will not be allocated`). And `sudo`'s `tty_tickets` default binds the credential cache to a specific pts — so even if the user `sudo -v`'s in *their* terminal, a separate Claude-side ssh session won't see the cache.

Three workable paths, pick by duration and trust:

| Path | When to use | Setup | Trade-offs |
|---|---|---|---|
| **A. NOPASSWD narrow scope** — `the0 ALL=(nick) NOPASSWD: ALL` in `/etc/sudoers.d/99-the0-as-nick` | Multi-step or open-ended work on another user's files. **Default recommendation.** | User runs in their own terminal: `echo 'the0 ALL=(nick) NOPASSWD: ALL' \| sudo tee /etc/sudoers.d/99-the0-as-nick && sudo chmod 440 /etc/sudoers.d/99-the0-as-nick` | Permanent until removed. Grants full sudo *as that user only*, NOT as root. Means: `sudo -u nick …` works passwordless, but `sudo …` (root) still needs password. Some operations like `chown -g root` won't work; work around with metadata preservation via `cp -a` of backups. |
| **B. Global timestamp + sudo -v** — `Defaults timestamp_type=global` in sudoers, then user runs `sudo -v` once | One-off task, < 15 min. Avoids permanent policy change. | User in their own terminal: `echo "Defaults timestamp_type=global" \| sudo tee /etc/sudoers.d/99-global-sudo-timestamp && sudo chmod 440 /etc/sudoers.d/99-global-sudo-timestamp && sudo -v` | 15-min window. After, all Claude-side `ssh host "sudo -n …"` go through. Cleanup: `sudo rm /etc/sudoers.d/99-global-sudo-timestamp`. |
| **C. Direct ssh as target user** | When the target user has a separate ssh alias / authorized_keys set up | None — just `ssh nick@host` | Cleanest if available, but usually requires pre-existing key. |

## Decision rule

Pick **A** unless the user explicitly wants a temporary grant (B) or already has a direct-ssh path (C). Narrow-scope NOPASSWD has the lowest per-call friction (zero), and the "as this user only, not root" limit is a good safety boundary.

## After granting

- Always start with `sudo -n -u <target> whoami` to verify path works without password.
- Use `sudo -n -u <target> cat /path` instead of `sudo cat /path` — the latter elevates to root, which the narrow scope doesn't allow.
- For metadata-preserving file replacement (owner / mode of existing files): `cp -a /home/<target>/file /home/<target>/file.bak` then `sudo -n -u <target> cp /tmp/new /home/<target>/file` — `cp` to existing target preserves dest metadata in many cases; verify with `ls -la` after.
- Don't forget cleanup at end of session if the user wanted temporary: `sudo rm /etc/sudoers.d/<file>` (this `rm` requires regular root sudo, not the narrow-scope one).

> verified by: 2026-05-29 nick-scripts DeepSeek migration task on the0.fun. Path A used; ~10 sudo -u nick calls passwordless, no friction.
