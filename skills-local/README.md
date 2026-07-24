# Local skills (`skills-local/`)

Directory is **not versioned** (except this README).

Two layouts are supported, both symlinked into `~/.claude-agent/skills/` by
`scripts/setup-symlinks.sh`:

- **Single-file skill** — drop `<name>.md` directly here. Linked as
  `~/.claude-agent/skills/<name>.md`.
- **Multi-file skill** (preferred for anything with `policy.md`, helper
  files, or supporting data) — create `<name>/SKILL.md` with optional
  siblings. The whole directory is symlinked as `~/.claude-agent/skills/<name>/`,
  mirroring how repo skills under `skills/` are linked.

Use for skills specific to this machine — those that depend on local tooling
or environment, or experimental drafts you don't want in the global repo yet.

To restore on a new machine: copy the relevant files / directories from
another machine or a backup, then run `setup-symlinks.sh`.

## Two `skills-local/` locations — which to use

There are **two** directories named `skills-local/`, both symlinked into the
same `~/.claude-agent/skills/` catalog, for two different purposes:

| Location | Tracked by | Use for |
|---|---|---|
| This directory, `<repo>/skills-local/` | This repo's git (README only — contents gitignored) | Drafts and machine-specific skills kept alongside the repo checkout. |
| `<agent-home>/skills-local/` (`$CLAUDE_AGENT_HOME/skills-local/`, default `~/.claude-agent/skills-local/`) | Not tracked anywhere — machine-local only | The overlay a skill lands in when it is **extracted out of this public repo** because it is specific to one machine or one organization and must not ship here. |

Extraction is driven by a manifest, `<agent-home>/extracted-skills.local` — one
skill name per line, `#` starts a comment, blank lines ignored. The format
lives in one place, [`scripts/lib/extracted-skills.sh`](../scripts/lib/extracted-skills.sh),
which both controls below read through. A missing manifest is a valid state
(no extracted skills; both controls stay green).

Two controls enforce the contract for every name the manifest lists:

- [`scripts/verify-layout-contract.sh`](../scripts/verify-layout-contract.sh) —
  fails if the name still has a directory under `<repo>/skills/` (extraction
  incomplete), or if `<agent-home>/skills-local/<name>/SKILL.md` is missing
  (overlay copy absent).
- [`scripts/verify-extracted-skills-resolve.sh`](../scripts/verify-extracted-skills-resolve.sh) —
  fails if the name does not resolve to a real skill under
  `~/.claude-agent/skills/` (i.e. `setup-symlinks.sh` has not linked the
  overlay copy in, or the link is dangling).
