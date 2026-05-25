# Local skills (`skills-local/`)

Directory is **not versioned** (except this README).

Two layouts are supported, both symlinked into `~/.claude/skills/` by
`scripts/setup-symlinks.sh`:

- **Single-file skill** — drop `<name>.md` directly here. Linked as
  `~/.claude/skills/<name>.md`.
- **Multi-file skill** (preferred for anything with `policy.md`, helper
  files, or supporting data) — create `<name>/SKILL.md` with optional
  siblings. The whole directory is symlinked as `~/.claude/skills/<name>/`,
  mirroring how repo skills under `skills/` are linked.

Use for skills specific to this machine — those that depend on local tooling
or environment, or experimental drafts you don't want in the global repo yet.

To restore on a new machine: copy the relevant files / directories from
another machine or a backup, then run `setup-symlinks.sh`.
