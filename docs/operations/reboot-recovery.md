# Reboot recovery

After a virtual machine reboots, its mounts are gone and every tmux session with a running `claude` is closed. `claude-recover` brings the machine back: it re-establishes the mounts, frees disk space if it is short, and reopens each interrupted session with `claude --resume`.

## How it works

Recovery rests on one fact: a reboot destroys the very state needed to restore from. So the state is saved **before** the reboot, continuously.

- **Snapshot.** A `systemd --user` timer runs `claude-recover snapshot` every minute. The snapshot records each live `claude` session (id, working directory, arguments with the prompt stripped, a whitelist of environment variables) and the FUSE mounts. It is written under the state directory, tagged with the boot id.
- **Restore.** `claude-recover` (the default action is `restore`) picks the newest snapshot that belongs to a **different** boot, then runs five phases: `disk-pre`, `mounts`, `compose`, `disk-post`, `sessions`. A session is reopened only if it is not already running and its transcript still exists.
- **Boot trigger.** `claude-recover.service` runs `restore --auto` once per boot, after `ccgram.service` by default. `--auto` opens sessions and then waits for the user's command; it never answers a pending question.
- **No double restore.** A `done-<boot_id>` marker stops a second automatic run in the same boot. A restore that skipped sessions (no tmux session, deadline reached) writes no marker.
- **Second reboot during a restore.** While a restore is pending, the snapshot timer does not write snapshots of the new boot. Otherwise a partial snapshot would hide the full one.

## Commands

```bash
claude-recover                      # restore after a reboot
claude-recover --dry-run            # print the plan, change nothing
claude-recover status               # snapshot age, snapshot vs live sessions
claude-recover status --check-fresh 180   # exit 0 when the snapshot is fresh and matches live sessions
claude-recover restore --dry-run --any-boot --format json   # idempotency check on a live system
```

## Install

```bash
scripts/install-claude-recover-systemd.sh --dry-run   # see what would be written
scripts/install-claude-recover-systemd.sh             # units, ~/.local/bin/claude-recover
scripts/install-claude-recover-systemd.sh --uninstall
```

The installer enables the snapshot timer and **enables but does not start** the restore unit, so installing never runs a restore on a live system. User units start at boot only with linger on (`loginctl enable-linger $USER`).

## Disk space

The `disk-pre` and `disk-post` phases measure free space. Below the warn level (free under 15% or under 50 GiB) Core offers only safe cleanup: dangling Docker images and the builder cache. Below the hard minimum (10 GiB) restore mounts only the essential mounts and skips the rest. Cleanup that depends on an organization's tooling comes from hooks.

## Hooks

Executable files in `~/.config/claude/recover.d/<NN>-*.sh` run in every phase with `RECOVER_PHASE`, `RECOVER_PLAN`, `RECOVER_DISK_LEVEL` and `RECOVER_LOG` set. Exit code 10 means "nothing to do". A hook must honour `RECOVER_DRY_RUN=1`. Hook output goes to `hooks.log` in the state directory.

## Behaviour worth knowing

- **When tmux fails (`TmuxError`), snapshot exits 1.** The snapshot unit then shows as failed, no empty snapshot is written and the `last-ok` stamp is not refreshed, so `status --check-fresh` starts failing. This is deliberate: an empty snapshot would look newer than the real pre-crash one.
- **The liveness check walks `/proc` for every session it opens and for every redecide.** `alive_session_ids` scans every `/proc/<pid>` whenever tmux has panes, once per session restore opens and once per redecide, not once per run. On a machine with thousands of processes and dozens of sessions the cost is noticeable.

## What recovery never does

- starts or restarts the tmux server, or touches `ccgram`;
- answers a question a session was waiting on, or sends a prompt;
- stores secrets: only whitelisted environment variables enter a snapshot;
- runs a destructive cleanup without a confirmation step.

## Tests

`scripts/tests/test_claude_recover.py` and `claude_recover_mutants.py` (each invariant has a mutant that the tests must kill), plus `test_install_claude_recover_systemd.py` and `test_claude_launchers_recover.py`.
