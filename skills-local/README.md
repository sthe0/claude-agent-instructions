# Local skills (`skills-local/`)

Directory is **not versioned** (except this README). Files `*.md` here are symlinked into `~/.claude/skills/` by `scripts/setup-symlinks.sh`.

Use for skills specific to this machine (e.g. skills that depend on local tooling or environment).

To restore on a new machine: copy the needed `*.md` files from another machine or a backup, then run `setup-symlinks.sh`.
