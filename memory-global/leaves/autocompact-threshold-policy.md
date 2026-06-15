---
name: autocompact-threshold-policy
description: Hard ceiling for context before auto-compaction is 150k tokens; the only harness knob is CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (a percent of the window), so percent = ceiling / window. Default settings.json value targets Opus 1M; spawns set it per-model.
metadata:
  type: feedback
---

**Difficulty:** the user wants context to *never* exceed a fixed absolute size (**150k tokens**) before auto-compaction, but the only harness knob is `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` — a **percent of the model's context window**, read once at session start. A hook cannot initiate or re-threshold compaction. So an absolute ceiling has to be expressed as a per-model percent.

**Policy:**

- **Ceiling = 150 000 tokens.** Conversion: `pct = round(ceiling / window * 100)`, i.e. `pct = round(15_000_000 / window)`.
- **Default `~/.claude/settings.json` `env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` = `15`** — sized for the **default chat model Opus 4.8, whose window is 1M** (confirmed via `/context`: `48.1k / 1m`). 15% × 1M = 150k.
- **Spawned `claude -p` sub-agents** may run a smaller-window model than the Opus-1M-sized default 15%, where 15% would compact far too early (e.g. 200k window → 30k → below the static prefix → autocompact thrash → child dies `MALFORMED`). So `scripts/spawn-specialist.py` passes the per-model percent via **`claude --settings '{"env":{"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "<pct>"}}'`** (cmd construction), keyed off the resolved model. **Not process env:** `settings.json` `env` is applied after process start and *overrides* process env, so an env injection is silently clobbered by the file's `15`; `--settings` sits above file settings in the precedence ladder and wins per-key. See [[claude-code-settings-env-precedence]].
- **Safe direction under window uncertainty:** *overestimate* the window → smaller percent → compacts no later than the ceiling. Unknown model ⇒ default window 1M ⇒ 15%.

**Why:** keep the working context bounded so cost (cache read/write scales with retained context) and quality stay predictable, independent of which model/window is active. See [[token-economy-plan]].

**How to apply:** to change the ceiling, edit the number in `settings.json` (Opus-1M default) *and* the `spawn-specialist.py` constant in lock-step — they encode the same ceiling against different windows. To re-derive a percent: `pct = round(15_000_000 / window_tokens)` for a 150k ceiling (generally `ceiling_tokens * 100 / window_tokens`).

Per-model window table — in `MODEL_WINDOW_TOKENS` in `spawn-specialist.py`:

| Model | Window | Percent (150k ceiling) | Source |
|---|---|---|---|
| Opus | 1M | 15% | `/context` 2026-06-15: `48.1k / 1m` |
| Sonnet | 200k | 75% | `/context` 2026-06-15: `93.2k / 200k`, "Auto-compact window: 200k" |
| Haiku | 200k | 75% | assumed (Haiku 4.5 base tier) |
| Fable | 200k | 75% | assumed |
| unknown | 1M (default) | 15% | safe overestimate |

> verified by: `/context` under `/model opus` and `/model sonnet` on 2026-06-15. Sonnet's 1M beta tier is **not** enabled on this account.

If the account's tiers change (e.g. Sonnet/Haiku/Fable gain the 1M tier), update `MODEL_WINDOW_TOKENS`. Wrong window in the *small* direction (assume smaller than real) is the only unsafe case (compacts too late → exceeds ceiling); the unknown-default of 1M is the safe (overestimate) direction.
