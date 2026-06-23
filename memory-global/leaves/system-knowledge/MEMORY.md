# Global system knowledge

Sub-index of `memory-global/leaves/system-knowledge/`. Durable facts about systems, processes, organizational structure, codebase architecture that aren't self-evident from `git log`/code/docs. Each leaf is **described by the difficulty it removes** (its functional ground), not as a free-floating fact. Recording criteria: `~/.claude/CLAUDE.md` § Memory § `system-knowledge/` leaves.

Pointed at from `memory-global/MEMORY.md`. Not auto-loaded by the harness.

- [Harness built-in Read dedup](harness-read-dedup.md) — difficulty: tempted to build a re-Read dedup hook / burning tokens on re-Reads. Fact: the harness already returns a "Wasted call" stub for re-Read of an unchanged file (Read or system-reminder), so no hook is needed.
- [DeepSeek v4 API specifics](deepseek-v4-api.md) — difficulty: need to call DeepSeek v4 without knowing the knobs. Fact: model names (`deepseek-v4-pro`/`deepseek-v4-flash`), thinking-mode via `extra_body`, `reasoning_effort` levels, `openai-agents` SDK pattern.
- [Cursor Agent CLI spawn](cursor-agent-cli-spawn.md) — difficulty: need a headless spawn in Cursor where `claude -p` is unavailable. Fact: `agent -p`, `~/.cursor_api_key`, install via `curl … | gunzip | bash`, wrapped by `spawn-cursor-escape.py`.
- [Cursor agent config location](cursor-agent-config-location.md) — difficulty: configuring Cursor's LLM provider server-side has no effect. Fact: that config is client-side (Cursor.app), not in `~/.cursor-server/`; only terminal-side CLI env vars are server-side.
- [iTerm2 + zsh nav keys](iterm2-zsh-nav-keys.md) — difficulty: Option/Cmd+arrow / Home / End print escape sequences. Fact: needs **both** an iTerm profile fix (`Option Key Sends = Esc+` / Natural Text Editing) AND a zsh `bindkey` block; `cat -v` diagnoses which half is broken.
- [Arcanum API: edit PR title/description](arcanum-api-readonly-pr-fields.md) — difficulty: updating a published PR's summary/description programmatically; the top-level object is read-only so it looks impossible. Fact: the sub-resources `PUT /api/v1/review-requests/{id}/description` and `/summary` accept `{"description"|"summary": "..."}` with an OAuth token → `204` (does not unpublish). `arc pr create` won't update an existing PR; amend+force-push only touches the commit-derived part, not a custom `-m` description block.
- [Home dir holds arc FUSE mounts](home-dir-arc-fuse-mounts.md) — difficulty: a broad find/grep/Grep/Glob rooted at `/home/the0`/`~`/`$HOME` fans across several network-backed `fuse.arc` mounts and is pathologically slow. Fact: scope every recursive search to a specific repo/subdir; a guard hook (`hook-arc-mount-search-guard.py`) denies roots spanning ≥2 arc mounts.

<!-- Add pointer lines per leaf as content accumulates. Group by topic when ≥10 leaves. Pattern: `- [<title>](<slug>.md) — difficulty it removes; fact.` -->
