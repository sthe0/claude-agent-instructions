# Local agents (`agents-local/`)

Per-machine subagent definitions that should not live in the global git repo. Files placed here are symlinked into `~/.claude/agents/` by `setup-symlinks.sh` and are excluded from version control via `.gitignore` (`agents-local/*`).

Project-specific subagents (e.g. agents for `robot/deepagent` or other Arcadia projects) belong in **that project's** own `.claude/agents/` tree, not here.

## Usage

1. Drop a `*.md` file (with valid agent frontmatter) into this directory.
2. Run `~/claude-agent-instructions/scripts/setup-symlinks.sh`.
3. The agent appears in `~/.claude/agents/<name>.md` and is available via `Task`.

The file stays gitignored — moving to another machine requires copying it manually.
