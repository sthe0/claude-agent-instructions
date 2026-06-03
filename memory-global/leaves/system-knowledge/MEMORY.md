# Global system knowledge

Sub-index of `memory-global/leaves/system-knowledge/`. Durable facts about systems, processes, organizational structure, codebase architecture that aren't self-evident from `git log`/code/docs. Recording criteria: `~/.claude/CLAUDE.md` § Memory § `system-knowledge/` leaves.

Pointed at from `memory-global/MEMORY.md`. Not auto-loaded by the harness.

- [Harness built-in Read dedup](harness-read-dedup.md) — Claude Code short-circuits re-Read of unchanged files (counting both prior Reads and prior system-reminder surfacing) with a "Wasted call" stub. No custom hook needed for this.
- [DeepSeek v4 API specifics](deepseek-v4-api.md) — model names (`deepseek-v4-pro`/`deepseek-v4-flash`), thinking-mode toggle via `extra_body`, `reasoning_effort` levels, `openai-agents` SDK integration pattern.
- [Cursor Agent CLI spawn](cursor-agent-cli-spawn.md) — `agent -p` headless escape in Cursor; `~/.cursor_api_key`; install via `curl … | gunzip | bash`; `spawn-cursor-escape.py`.
- [Cursor agent config location](cursor-agent-config-location.md) — Cursor's custom LLM provider config is client-side (Cursor.app), not in `~/.cursor-server/` on the remote; what *can* be done server-side is env vars for terminal-side CLI.
- [iTerm2 + zsh nav keys](iterm2-zsh-nav-keys.md) — Option/Cmd+arrow / Home / End escape-sequence printing on user's Mac needs both iTerm profile fix (`Option Key Sends = Esc+` / Natural Text Editing preset) AND zsh `bindkey` block; `cat -v` diagnoses which half is broken.

<!-- Add pointer lines per leaf as content accumulates. Group by topic when ≥10 leaves. Pattern: `- [<title>](<slug>.md) — one-line hook.` -->
