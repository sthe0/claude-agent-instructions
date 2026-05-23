# Local agents (`agents-local/`)

Deprecated for Arcadia: Logos agents live in `logos/neuro/agent/claude_code/agents/` (exposed via `logos/.claude/agents/` symlinks). **yandex-guru** lives in `robot/deepagent/.claude/agents/`.

## On non-Arcadia machines (fallback)

Place gitignored `*.md` files directly in this directory. They are linked into `~/.claude/agents/` by `setup-symlinks.sh`. Excluded from git via `.gitignore` (`agents-local/*`).

After adding files: `~/claude-agent-instructions/scripts/setup-symlinks.sh`.
