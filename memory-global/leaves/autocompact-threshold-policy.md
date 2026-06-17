---
name: autocompact-threshold-policy
description: Keep the working context bounded (~150k) before auto-compaction. TWO harness knobs, not one — CLAUDE_CODE_AUTO_COMPACT_WINDOW pins the effective window (takes precedence, value auto|100k–1M) and CLAUDE_AUTOCOMPACT_PCT_OVERRIDE sets the percent of that window (IS consumed on the main session). 1M tier disabled -> Opus is 200k. Pin window=200k + pct=75 -> ~140–150k.
metadata:
  type: feedback
---

**Difficulty:** the user wants the working context to stay bounded (target **~150k tokens**) before auto-compaction, independent of model/window. Env is read **once at session start**; a hook cannot re-threshold a live session. The earlier version of this leaf claimed the *only* knob is `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` and that 75% × 200k reliably gives 150k — that claim was **never runtime-verified and was falsified**: a long session ran to 199.9k/200k with no compaction (`/context` 2026-06-17 showed 200k window, 100%, "autocompact will trigger soon").

**Root cause of the earlier miss (verified by decompiling `claude.exe`, 2026-06-17):** the code path *does* honor the percent override on the main session — the overshoot was a **stale-env / config-drift** artifact: `settings/base.json` held `70` while live `~/.claude/settings.json` held `75`, and the offending session started before an effective value was in place. The real gap in the leaf was **omitting the primary knob** (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`) and asserting an unverified percent-only model.

## Verified mechanism (decompiled main-session path)

Autocompact decision: `MCf → $UH → As4(tokens, w1H(model, AUTO_COMPACT_WINDOW), opts) → zB8`.

- **Threshold** (`zB8(effW, opts)`): `testPctOverride` set & `0<pct≤100` → `min(⌊effW·pct/100⌋, effW−13000)`; else `effW−13000`. The `"compact"` level in `As4` fires when `enabled && tokens ≥ threshold`. So `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (→ `testPctOverride`) **is** consumed on the main interactive session, despite the "test" name.
- **Effective window** (`w1H`): `MU(model, AUTO_COMPACT_WINDOW).window − buffer`, where `MU` resolves `min(AUTO_COMPACT_WINDOW setting, model max window)`. When the setting is `auto` (default) it is just the model max.
- **1M tier:** `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` makes the 1M-capability check (`IR`/`bfH`) return false for Opus 4.7/4.8/Fable/Mythos → model max = **200k** (not 1M). So `/context` shows 200k and the window in the formula is 200k.
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is the **documented, precedence-taking** knob (binary: "the actual threshold is the minimum of this setting and your model's maximum context window"; "is set and takes precedence"). Settable via env or `/config → Auto-compact window`. Value: `auto` or `100k…1M` (parser `sAq`: accepts `k`/`m` suffix or a bare int ≥100 treated as ×1000).
- `autoCompactEnabled` (settings) / `DISABLE_AUTO_COMPACT` (env) is the on/off toggle.

> verified by: decompiled `bin/claude.exe` functions `zB8`/`nAq`/`As4`/`w1H`/`HYq`/`MCf`/`$UH`/`IR`/`bfH`/`sAq` — 2026-06-17 session.

## Policy

- **Target ceiling ≈ 150k.** Set **both** knobs in `settings/base.json` `env` (applied to live via `apply-settings.sh`):
  - `CLAUDE_CODE_AUTO_COMPACT_WINDOW = "200k"` — pins the effective window to 200k deterministically (belt-and-suspenders: if any model/provider re-enables extended context, this still caps the autocompact window at 200k).
  - `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE = "75"` — 75% of the pinned window. Net trigger ≈ `min(0.75·~187k, ~174k)` ≈ **140–150k** (the `−13000` buffer keeps it just under 150k → safe direction: compact no later than the ceiling).
- **Env is read at session start** — a settings change takes effect only on the **next Claude Code restart**, never the current session. Use `/compact` manually to bound the active session.
- **Avoid base-vs-live drift.** Always change the value in `settings/base.json` and run `apply-settings.sh` (or `set-context-cap.sh`, which writes base.json then applies). A hand-edit to live `~/.claude/settings.json` alone is clobbered on the next merge and silently desyncs the effective threshold — this drift caused the earlier overshoot.
- **Spawned `claude -p` sub-agents** get the percent via `claude --settings '{"env":{"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE":"<pct>"}}'` (`scripts/spawn-specialist.py`, keyed off resolved model). `--settings` sits above file `env` in the precedence ladder and wins per-key. See [[claude-code-settings-env-precedence]].

**Why:** keep the working context bounded so cost (cache read/write scales with retained context) and quality stay predictable, independent of which model/window is active. See [[token-economy-plan]].

**How to change the ceiling:** prefer `scripts/set-context-cap.sh <tokens>` (writes base.json + applies). To re-derive a percent against a pinned window: `pct = round(ceiling / window * 100)` (150k / 200k = 75%).

## Known gap (follow-up, not yet applied)

`set-context-cap.sh` and `spawn-specialist.py` set **only** `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (correct — it is consumed), but do **not** emit `CLAUDE_CODE_AUTO_COMPACT_WINDOW`. With 1M disabled this is fine (Opus = 200k). If extended context is ever re-enabled for any model, those paths would compute the percent against a ~1M window and compact far later than the ceiling. Robustness fix when needed: have both also pin `CLAUDE_CODE_AUTO_COMPACT_WINDOW` to the intended window.

## Per-model window table

| Model | Window | Source |
|---|---|---|
| Opus 4.7 / 4.8 | 200k | 1M tier disabled (`CLAUDE_CODE_DISABLE_1M_CONTEXT=1`; `IR()` → false) |
| Sonnet | 200k | base tier |
| Haiku | 200k | base tier |
| Fable | 200k | 1M-capable, but disabled by the flag |

> If 1M is re-enabled for any model, either pin `CLAUDE_CODE_AUTO_COMPACT_WINDOW` to the desired window or lower the percent so the trigger still lands at the ceiling. Assuming a window *smaller* than real is the only unsafe direction (compacts too late → exceeds ceiling).
