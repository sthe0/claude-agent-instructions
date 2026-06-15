---
name: Claude Code — settings.json env wins over shell env
description: Env vars in settings.json `env` block are applied by Claude Code after process start and override the shell environment, including `env -u`. Auth precedence ladder and consequences.
type: reference
---

# `settings.json` `env` precedence in Claude Code

**Fact**: variables under the `env` key in `~/.claude/settings.json` are applied by Claude Code itself after process start and override values from the parent shell, **including `env -u`** (unsetting in the shell does not help).

**Empirical verification (2026-05-24, the0.klg.yp-c.yandex.net)**: with `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` set in settings.json (ELIZA proxy), running `env -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL claude --print "..."` resolved to the same ELIZA endpoint with the same token (same 429 quota response, same request_id pattern). Shell-side override had zero effect.

## Consequence

To replace an env var set in settings.json, you must either:
1. **Remove it from settings.json** and provide it via a shell wrapper / sourced env file before launching `claude`.
2. **Use a separate config dir**: `CLAUDE_CONFIG_DIR=/path/to/other-dir claude` — that dir's settings.json takes over completely (downside: hooks/plugins/agents in `~/.claude` won't apply).
3. **Pass `--settings '{"env":{...}}'`** (file or inline JSON) — CLI `--settings` sits **above** all file-based settings in the precedence ladder, so it overrides `~/.claude/settings.json` per-key (other env keys from the file still apply). Best when you need to override **one** key for a single `claude` invocation without touching the user's settings file.

**Does NOT work**: `env -u VAR claude`, `VAR= claude` (empty), `unset VAR; claude`.

## Concrete instance (2026-06-15)

`spawn-specialist.py` injected `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (per-model, e.g. 75 for sonnet's 200k window → 150k ceiling) into the **child's process env**. It was silently clobbered by `env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=15` (Opus-1M-sized) in `~/.claude/settings.json` → every non-opus spawn compacted at 15% × 200k = 30k tokens, below its static prefix → autocompact thrash → child died with `MALFORMED` (no return marker). Opus spawns (1M window, 15% = 150k) were unaffected, so the bug stayed latent. **Fix**: pass the threshold via `claude --settings` (consequence #3) instead of process env.

## Auth precedence ladder (first wins)

1. Cloud provider creds (Bedrock / Vertex / Foundry env vars)
2. `ANTHROPIC_AUTH_TOKEN`
3. `ANTHROPIC_API_KEY`
4. `apiKeyHelper` (script path from settings.json, output is the token)
5. `CLAUDE_CODE_OAUTH_TOKEN`
6. Subscription OAuth (`~/.claude/.credentials.json`, populated by `claude login` or in-session `/login`)

## Fallback design notes

- `apiKeyHelper` returns **only a token**, not `BASE_URL`. If two providers have different base URLs (e.g. an internal proxy vs `api.anthropic.com`), a single `apiKeyHelper` is insufficient — needs a local HTTP proxy or a shell-wrapper switch.
- `claude login` itself respects `ANTHROPIC_BASE_URL` from settings.json and will try OAuth handshake against that endpoint (which fails if the endpoint is a proxy without OAuth support, or if it returns the proxy's own error like a 429 quota response). Workaround: in-session `/login` slash command goes to api.anthropic.com directly regardless of `BASE_URL`.

## Worked example

Real-world wiring of a default-ELIZA + fallback-to-personal-OAuth setup on the0.klg: see project memory `project_the0klg_claude_fallback.md` (in `~/.claude/projects/-Users-the0/memory/`).
