# Bash sandbox feasibility on this machine

Claude Code's Bash sandbox on Linux runs commands under `bwrap` (bubblewrap) for filesystem isolation and routes
network traffic through `socat` proxies. Enabling it would stop Bash from writing outside the allowed directories
without asking, which overlaps with part of what the resource-grant hook does. This note records whether the sandbox
could be turned on here. It is an investigation only: nothing was installed and no setting was changed.

Facts measured on 2026-10-27, Ubuntu 24.04.2 LTS, Claude Code 2.1.310:

bwrap: absent
socat: absent
sudo -n: yes (passwordless sudo is available to this user)
userns: user.max_user_namespaces=204686; `kernel.unprivileged_userns_clone` and `kernel.apparmor_restrict_unprivileged_userns` do not exist on this kernel, and an unprivileged `unshare --user --map-root-user true` succeeds
packages: apt (`/usr/bin/apt-get`); `bubblewrap` provides bwrap and `socat` provides socat. The local apt lists are empty, so `apt-cache show` finds neither package until `sudo apt-get update` runs.
covers: filesystem and network isolation of Bash commands only
does not cover: Read/Edit/Write tool calls, MCP tool calls, and remote effects of VCS commands (a `git push` or an `arc` operation reaches the server through whatever network the sandbox allows) — the resource-grant hook stays needed for all of these

> verified by: `command -v bwrap` and `command -v socat` print nothing (exit 1) on 2026-10-27.
> verified by: `sudo -n true` exits 0 without a password prompt.
> verified by: `cat /proc/sys/user/max_user_namespaces` prints `204686`; `unshare --user --map-root-user true` exits 0 as the unprivileged user.
> verified by: `apt-cache show bubblewrap` and `apt-cache show socat` print `E: No packages found`; `/var/lib/apt/lists` holds 3 entries (no package indexes).
> verified by: `/etc/os-release` reports `Ubuntu 24.04.2 LTS`; `claude --version` prints `2.1.310 (Claude Code)`.

The kernel allows unprivileged user namespaces, so bwrap would run without a setuid bit once installed. What is
missing is the two binaries, and installing them needs root — available here through passwordless sudo, after an
`apt-get update`.

Verdict: feasible with sudo

Even when enabled, the sandbox only narrows Bash. The approval stops this plan removes also come from Read and
Edit/Write outside the working directories (see `resource-grant-hook-harness-probe.md`, Q3), which the sandbox does
not touch, so it complements the resource-grant hook rather than replacing it.
