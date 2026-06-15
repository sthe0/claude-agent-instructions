---
name: autocompact-threshold-policy
description: Hard ceiling for context before auto-compaction is 150k tokens; the only harness knob is CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (a percent of the window), so percent = ceiling / window. 1M context tier is disabled, so every model is 200k -> 75%; spawns set it per-model via --settings.
metadata:
  type: feedback
---

**Difficulty:** the user wants context to *never* exceed a fixed absolute size (**150k tokens**) before auto-compaction, but the only harness knob is `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` — a **percent of the model's context window**, read once at session start. A hook cannot initiate or re-threshold compaction. So an absolute ceiling has to be expressed as a per-model percent.

**Policy:**

- **Ceiling = 150 000 tokens.** Conversion: `pct = round(ceiling / window * 100)`.
- **Default `~/.claude/settings.json` `env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` = `75`** — the 1M context tier is disabled (`env.CLAUDE_CODE_DISABLE_1M_CONTEXT=1`), so every model (Opus included) runs a **200k** window. 75% × 200k = 150k.
- **Spawned `claude -p` sub-agents** get their percent set explicitly via **`claude --settings '{"env":{"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "<pct>"}}'`** (cmd construction in `scripts/spawn-specialist.py`), keyed off the resolved model. **Not process env:** `settings.json` `env` is applied after process start and *overrides* process env, so an env injection is silently clobbered by the file value; `--settings` sits above file settings in the precedence ladder and wins per-key. Keeping the explicit `--settings` (even though every window is 200k → 75% today) means spawns stay correct if the file default and a model's window ever diverge again (e.g. 1M re-enabled for one model). See [[claude-code-settings-env-precedence]].
- **Safe direction under window uncertainty:** *overestimate* the window → smaller percent → compacts no later than the ceiling. With 1M disabled, the default window is 200k (⇒ 75%); if you re-enable 1M for any model, raise that model's window (and the default) so the percent tracks it.

**Why:** keep the working context bounded so cost (cache read/write scales with retained context) and quality stay predictable, independent of which model/window is active. See [[token-economy-plan]].

**How to apply:** to change the ceiling, edit the number in `settings.json` *and* `AUTOCOMPACT_CEILING_TOKENS` / `MODEL_WINDOW_TOKENS` in `spawn-specialist.py` in lock-step — they encode the same ceiling against the window. To re-derive a percent: `pct = round(ceiling_tokens * 100 / window_tokens)` (150k / 200k = 75%).

Per-model window table — in `MODEL_WINDOW_TOKENS` in `spawn-specialist.py`:

| Model | Window | Percent (150k ceiling) | Source |
|---|---|---|---|
| Opus | 200k | 75% | 1M tier disabled (`CLAUDE_CODE_DISABLE_1M_CONTEXT=1`) |
| Sonnet | 200k | 75% | base tier; `/context` 2026-06-15 showed `93.2k / 200k` |
| Haiku | 200k | 75% | assumed (base tier) |
| Fable | 200k | 75% | assumed |
| unknown | 200k (default) | 75% | 1M disabled → no model exceeds 200k |

> note: the 1M context tier is disabled machine-wide via `env.CLAUDE_CODE_DISABLE_1M_CONTEXT=1`. An earlier `/context` (2026-06-15) showed Opus at `1m` *before* the tier was disabled; with it off, Opus is 200k like the rest.

If 1M is re-enabled for any model, bump its window in `MODEL_WINDOW_TOKENS` (and `DEFAULT_WINDOW_TOKENS`) so the derived percent tracks it. Wrong window in the *small* direction (assume smaller than real) is the only unsafe case (compacts too late → exceeds ceiling).
