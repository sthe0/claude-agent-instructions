---
name: instructions-repo-git-sync
description: "Operational reference for the instructions-repo git-sync install surface: the opt-in background-pull cron (and its systemd-timer fallback), the git-hooks installer (post-commit reminder, no auto-push), and the sync-instructions-repo.sh script table. Elaboration relocated out of self-improvement/policy.md; the firing sync/commit/push rules stay in that skill."
type: reference
schema: leaf/v1
created: 2026-07-20
last_verified: 2026-07-20
---

# Instructions-repo git-sync install & scripts reference

## Difficulty

Desired — when setting up or operating the sync surface of `~/claude-agent-instructions/`, know how to install the optional background pull, install the git hooks, and what each `sync-instructions-repo.sh` verb does. Actual — these install commands and the script table are pure operational reference (no rule that must *fire* while editing an instruction file), so carrying them inline in `skills/self-improvement/policy.md` spent that skill's line budget (it sat past the linter's WARN threshold) without earning salience. The firing rules — pull-then-re-read before editing, commit-then-push-only-after-confirm, the author-machine carve-out, no-push-rights, serving-checkout-stays-on-`main`, the `[self-improvement-reviewed]` marker — stay in `policy.md § Git sync`; this leaf holds only the reference the model reads on demand, not on every load.

## Guidance

### Background pull (opt-in, every 10 minutes)

Background auto-pull is **not installed by default**; `setup-symlinks.sh` does not enable it. Install manually only if you want it:

```bash
~/claude-agent-instructions/scripts/install-sync-cron.sh
```

Cron line (repo path substituted on install): `*/10 * * * * …/sync-instructions-repo.sh pull`.
Log: `~/.local/log/claude-agent-instructions-sync.log`.

If `crontab` is forbidden (corp VM): `scripts/install-sync-systemd-timer.sh`.

To disable later: `crontab -l | grep -v claude-agent-instructions | crontab -`.

Cron pull does **not** replace the mandatory reconcile at the start of a session that will edit code or instructions (`policy.md § After pull`).

### Git hooks

```bash
~/claude-agent-instructions/scripts/install-git-hooks.sh
```

`post-commit` only reminds that push needs user confirmation — it does **not** auto-push (see `policy.md § After editing`).

### Scripts

| Script | Purpose |
|---|---|
| `sync-instructions-repo.sh pull` | fetch + rebase / ff-only |
| `sync-instructions-repo.sh push` | push if local commits exist |
| `sync-instructions-repo.sh sync` | pull, then push |
| `install-sync-cron.sh` | cron line (pull every 10 min) — opt-in, run manually if desired |
| `install-sync-systemd-timer.sh` | user systemd timer (if cron unavailable) — opt-in |
| `install-git-hooks.sh` | post-commit → reminder (no auto-push) |
| `setup-symlinks.sh` | apply the runtime symlinks |
| `setup-project-memory.sh` | per-project: symlink shared agent memory into the project tree |
| `verify-instructions-sync.sh` | check symlinks and drift |
| `verify-layout-contract.sh` | tree vs the layout in the policy document |

## See also

- `skills/self-improvement/policy.md § Git sync (instructions repo)` — the firing rules this leaf's reference was extracted from.
- [instructions-repo-layout.md](instructions-repo-layout.md) — the canonical repo tree and the `setup-symlinks.sh` runtime-path → repo-source table.
