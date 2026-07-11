# Global system knowledge

Sub-index of `memory-global/leaves/system-knowledge/`. Durable facts about systems, processes, organizational structure, codebase architecture that aren't self-evident from `git log`/code/docs. Each leaf is **described by the difficulty it removes** (its functional ground), not as a free-floating fact.

Pointed at from `memory-global/MEMORY.md`. Not auto-loaded by the harness.

## Recording criteria

Record durable facts about systems, processes, org structure, component interrelations, codebase architecture that isn't self-evident. **Lead each leaf with the difficulty it removes** — describe the component/process by the divergence it resolves (its functional ground), not as a free-floating fact; the rediscovery cost the leaf spares *is* that difficulty. A fact whose difficulty you can't name fails criterion 1 below anyway. Filename is a content-keyed slug, no date (`auth-team-ownership.md`). Same frontmatter as other leaves (`name` / `description` / `type: reference`).

Record only if **all four** apply:

1. **Not reachable in 1–2 hops** of internet / intranet / `git log` / repo search.
2. **Not explicitly documented** in code, README, ADR, or known design docs.
3. **Not a duplicate** of an existing leaf — search `system-knowledge/` (and adjacent memory) before writing; update an existing leaf instead of creating a parallel one.
4. **Specific, not a principle** — names a concrete component / process / person / dataflow boundary. Generic patterns and reasoning practices belong in `leaves/*.md` (evergreen reference), not here.

Cite the source where possible (`> verified by: <commit>/<URL>/<conversation>`).

- [Claude Code drops pre-tool-call text](claude-code-drops-pre-tool-call-text.md) — difficulty: a deliverable (plan rendering, answer) written before a tool call in the same turn silently never reaches the user, who reacts as if it was never written. Fact: the CLI (confirmed v2.1.198) renders only the turn's final message and the AskUserQuestion question/options; deliver content in the final message or inside the question fields.
- [Harness built-in Read dedup](harness-read-dedup.md) — difficulty: tempted to build a re-Read dedup hook / burning tokens on re-Reads. Fact: the harness already returns a "Wasted call" stub for re-Read of an unchanged file (Read or system-reminder), so no hook is needed.
- [DeepSeek v4 API specifics](deepseek-v4-api.md) — difficulty: need to call DeepSeek v4 without knowing the knobs. Fact: model names (`deepseek-v4-pro`/`deepseek-v4-flash`), thinking-mode via `extra_body`, `reasoning_effort` levels, `openai-agents` SDK pattern.
- [Cursor Agent CLI spawn](cursor-agent-cli-spawn.md) — difficulty: need a headless spawn in Cursor where `claude -p` is unavailable. Fact: `agent -p`, `~/.cursor_api_key`, install via `curl … | gunzip | bash`, wrapped by `spawn-cursor-escape.py`.
- [Cursor agent config location](cursor-agent-config-location.md) — difficulty: configuring Cursor's LLM provider server-side has no effect. Fact: that config is client-side (Cursor.app), not in `~/.cursor-server/`; only terminal-side CLI env vars are server-side.
- [iTerm2 + zsh nav keys](iterm2-zsh-nav-keys.md) — difficulty: Option/Cmd+arrow / Home / End print escape sequences. Fact: needs **both** an iTerm profile fix (`Option Key Sends = Esc+` / Natural Text Editing) AND a zsh `bindkey` block; `cat -v` diagnoses which half is broken.
- [Arcanum API: edit PR title/description](arcanum-api-readonly-pr-fields.md) — difficulty: updating a published PR's summary/description programmatically; the top-level object is read-only so it looks impossible. Fact: the sub-resources `PUT /api/v1/review-requests/{id}/description` and `/summary` accept `{"description"|"summary": "..."}` with an OAuth token → `204` (does not unpublish). `arc pr create` won't update an existing PR; amend+force-push only touches the commit-derived part, not a custom `-m` description block.
- [Home dir holds arc FUSE mounts](home-dir-arc-fuse-mounts.md) — difficulty: a broad find/grep/Grep/Glob rooted at `/home/the0`/`~`/`$HOME` fans across several network-backed `fuse.arc` mounts and is pathologically slow. Fact: scope every recursive search to a specific repo/subdir; a guard hook (`hook-arc-mount-search-guard.py`) denies roots spanning ≥2 arc mounts.

- [Retire prewrite-plan-check hook](prewrite-hook-retirement-criterion.md) — difficulty: the legacy non-agentctl plan-check hook could be dropped on a guess, losing plan-gate coverage for prose-fallback sessions. Fact: it now logs every firing to `~/.claude-agent/agentctl/prewrite-fallback.jsonl`; retire only when `prewrite-fallback-report.py` shows 0 firings for ≥1 full window after engine auto-start ships.
- [Instructions-repo layout](instructions-repo-layout.md) — difficulty: disk disagrees with the canonical layout (a file not in the contract, a wrong symlink target, a script in the wrong dir) and resolving it needs the authoritative tree. Fact: the global tree, the `setup-symlinks.sh` runtime-path → repo-source table, and the per-project `agent-memory/` symlink wiring.
- [Landing changes: Core git & arc](landing-changes-core-git-and-arc.md) — difficulty: `git push origin main` rejected (WIP feature branch / stale main), `gh` absent, trunk protected → wrongly read as "blocked / no rights". Fact: land one commit to `main` via an isolated worktree cherry-pick; land to trunk via `arc checkout -b` → `arc pr create --publish`; missing-tool / non-ff / protected-branch is routing, not a permission wall (probe with `git push --dry-run` / `is_author()`).
- [Cross-session filesystem-scope isolation](cross-session-scope-isolation.md) — difficulty: parallel sessions sharing one working tree/mount collide, with only a reactive git-status/worktree playbook. Fact: a `session_scope` registry + VCS-agnostic path-overlap detector + `hook-scope-conflict.py` (PreToolUse) deny/warn on a LIVE cross-session overlap; block only a gated held path, warn otherwise, silent single-session; fail-open. Isolate, not serialize. Ops doc under `docs/operations/`.
- [CLAUDE_AFK_TIMEOUT_MS unverified](claude-afk-timeout-ms-unverified.md) — difficulty: an env var pinned in `settings/base.json` (2e9, to disable the ~60s AskUserQuestion auto-proceed) whose effect nothing confirms. Fact: no repo consumer reads it and no Claude Code primary source (docs/`settings.md`/CHANGELOG/GitHub) mentions it — purpose is asserted only by commit `cbe290f`; treat as possible dead config, verify empirically in a fresh session before relying on it (kept pending that check, 2026-07-02).

- [agentctl approve leaves runtime plan stale](agentctl-approve-stale-runtime-plan.md) — difficulty: a plan edited between `submit-plan` and `approve` silently carries a stale `state.stages`/`final_check` into partition/execution/verify-final (symptom: `partition` rejects a stage index that exists in the plan). Fact: only `submit-plan`/`replan` rebuild the runtime copy; `approve` re-snapshots but does not; recovery is `reset --force` + re-drive over byte-identical content; latent Core fix candidate (approve should refuse on divergence).

<!-- Add pointer lines per leaf as content accumulates. Group by topic when ≥10 leaves. Pattern: `- [<title>](<slug>.md) — difficulty it removes; fact.` -->
