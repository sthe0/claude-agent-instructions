# Local agents (`agents-local/`)

## On Arcadia machines

Content is **not** in git `claude-agent-instructions`. Edit only on mount **`~/arcadia_the0-agents`**, branch **`the0-agents`**, path **`junk/the0/agents/agents-local/`**.

Symlinks (global): `~/claude-agent-instructions/scripts/setup-symlinks.sh`  
Arc scripts (local): `~/.claude/scripts-local/`  
Runbook: `~/.claude/memory/INDEX.md` → `claude-code/the0-agents-mount.md`

## On non-Arcadia machines (fallback)

Place gitignored `*.md` files directly in this directory. They are linked into `~/.claude/agents/` by `setup-symlinks.sh`. These files are excluded from git via `.gitignore` (`agents-local/*`), so they are machine-local only.

After adding files: `~/claude-agent-instructions/scripts/setup-symlinks.sh`.
