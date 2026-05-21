# Git: instructions repository (`~/claude-agent-instructions`)

Symlinks: `~/.claude/agents`, `~/.claude/CLAUDE.md`, `~/.cursor/rules/claude-code-sync.mdc` → files in the repo.

## Before editing (mandatory)

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
```

Fetch `origin/main`. On rebase conflict the script prefers **incoming** changes (`--theirs`); if that fails — resolve manually.

## After pull (mandatory reconcile)

When `pull` brought new commits (`behind > 0` before pull, or log shows `pull: done` with updates):

1. **Verify tree** — `scripts/verify-instructions-sync.sh` and `scripts/verify-layout-contract.sh` (no FAIL).
2. **Read what changed** — `git log -3 --oneline` and, if needed, `git diff HEAD@{1}..HEAD --stat` for agents, `CLAUDE.md`, `cursor-rules/`, `memory-global/`.
3. **Reconcile active work** — compare open plan, pending edits, and delegation choices with new policy. If pulled rules **contradict** what you already did or planned in this session:
   - **stop** further production edits until aligned;
   - adjust plan or revert local tactical changes;
   - tell the user what conflicted (file/section) and which rule now applies.
4. **Do not assume** pre-pull mental model still holds for gates (manager, self-improvement, mount, planner approval, instruction language).

Cron background `pull` does not replace this reconcile at the start of a session that will edit code or instructions.

## After editing

```bash
cd ~/claude-agent-instructions
git add -A && git commit -m "…"
~/claude-agent-instructions/scripts/sync-instructions-repo.sh push
```

**Every** commit in this repo is followed by **push** to `origin` (without asking the user).

If push is rejected (remote ahead): `pull` → fix conflicts if needed → `push` again.

## Background sync (every 10 minutes)

```bash
~/claude-agent-instructions/scripts/install-sync-cron.sh
```

Cron line (repo path substituted on install): `*/10 * * * * …/sync-instructions-repo.sh pull`.

Log: `~/.local/log/claude-agent-instructions-sync.log`

If `crontab` is forbidden (corp VM): `scripts/install-sync-systemd-timer.sh` — user systemd timer (pull every 10 min). Agent also runs `pull` before each edit and at session start when planning to touch the repo.

## Git hooks

```bash
~/claude-agent-instructions/scripts/install-git-hooks.sh
```

`post-commit` automatically runs `sync-instructions-repo.sh push` after each commit (duplicates explicit agent push — for manual commits).

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/sync-instructions-repo.sh pull` | fetch + rebase/ff-only |
| `scripts/sync-instructions-repo.sh push` | push if local commits exist |
| `scripts/sync-instructions-repo.sh sync` | pull, then push |
| `scripts/install-sync-cron.sh` | add cron line |
| `scripts/install-sync-systemd-timer.sh` | user systemd timer (if cron unavailable) |
| `scripts/install-git-hooks.sh` | post-commit → auto-push |
| `scripts/setup-symlinks.sh` | Claude + Cursor symlinks |
| `scripts/verify-instructions-sync.sh` | check symlinks and drift |
| `scripts/verify-layout-contract.sh` | tree vs contract |
