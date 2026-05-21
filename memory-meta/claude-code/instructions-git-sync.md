# Git: instructions repository (`~/claude-agent-instructions`)

Symlinks: `~/.claude/agents`, `~/.claude/CLAUDE.md`, `~/.cursor/rules/claude-code-sync.mdc` → files in the repo.

**Canonical copy:** `~/.claude/memory-global/agent-instructions/instructions-git-sync.md`

## Before editing (mandatory)

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
```

Fetch `origin/main`. On rebase conflict the script prefers **incoming** (`--theirs`); if that fails — resolve manually.

## After editing

```bash
cd ~/claude-agent-instructions
git add -A && git commit -m "…"
~/claude-agent-instructions/scripts/sync-instructions-repo.sh push
```

**Every** commit is followed by **push** to `origin` (without asking the user).

If push rejected (remote ahead): `pull` → fix conflicts → `push` again.

## Background sync (every 10 minutes)

```bash
~/claude-agent-instructions/scripts/install-sync-cron.sh
```

Cron: `*/10 * * * * …/sync-instructions-repo.sh pull`. Log: `~/.local/log/claude-agent-instructions-sync.log`

If `crontab` forbidden: `scripts/install-sync-systemd-timer.sh`. Agent also `pull`s before each edit.

## Git hooks

`install-git-hooks.sh` — `post-commit` → auto `push`.

## Scripts

| Script | Purpose |
|--------|---------|
| `sync-instructions-repo.sh pull` | fetch + rebase/ff-only |
| `sync-instructions-repo.sh push` | push if local commits |
| `sync-instructions-repo.sh sync` | pull, then push |
| `install-sync-cron.sh` | add cron |
| `install-sync-systemd-timer.sh` | user systemd timer |
| `install-git-hooks.sh` | post-commit → push |
| `setup-symlinks.sh` | Claude + Cursor symlinks |
| `verify-instructions-sync.sh` | symlinks and drift |
