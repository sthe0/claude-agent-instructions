---
name: autocompact-threshold-policy
description: Keep the auto-compaction trigger comfortably ABOVE the ~150k post-compaction floor — a trigger at/below the floor re-fires every turn (thrash). Primary knob CLAUDE_CODE_AUTO_COMPACT_WINDOW pins the effective window (precedence, value auto|100k–1M); CLAUDE_AUTOCOMPACT_PCT_OVERRIDE is a secondary percent-of-window knob. Threshold = min(⌊window·pct/100⌋ when pct set, window−13k). Current: window=300k, 1M on, no PCT -> trigger ~287k.
metadata:
  type: feedback
---

**Difficulty:** keep the working context bounded before auto-compaction **without** destabilizing the session — and the hard constraint is that the auto-compaction *trigger* cannot live near the **~150k post-compaction floor** (the context a compaction leaves behind: system prompt + memory + recent turns). Env is read **once at session start**; a hook cannot re-threshold a live session.

There are **two opposite failure modes**, both seen on 2026-06-17:

- **Thrash — compact every turn (DEEPAGENT-430, session `323be019`, ~18 events).** Commit `e8abc05` set `window=200k` + `pct=75` + `DISABLE_1M=1` → threshold `min(⌊200k·0.75⌋, 200k−13k) = 150k`. That **collides with the ~150k floor**: immediately after each compaction the retained context is already ≥ the trigger, so it re-compacts on the next turn, forever. The whole premise "cap context at ~150k" was *impossible* — you cannot trigger below where a compaction lands. Hand-fixed live, committed `fc7c5ce` + `83fa383`: `window=400k`, drop `pct`, re-enable 1M → trigger ~387k, safely above the floor.
- **Overshoot — compact too late (earlier).** A long session ran to 199.9k/200k with no compaction. Not a code limitation: decompiling `claude.exe` confirms the main session *does* honor the percent override; the overshoot was **stale-env / config-drift** — `settings/base.json` held `70` while live `~/.claude/settings.json` held `75`, and the session started before an effective value was in place.

**Safe rule: the trigger must sit comfortably ABOVE the ~150k floor (margin ≥ ~50k → target cap ≥ ~200k).** A cap at or below the floor is unachievable and thrashes; a cap far above is fine (just compacts later). When in doubt, err high.

## Verified mechanism (decompiled main-session path)

Autocompact decision: `MCf → $UH → As4(tokens, w1H(model, AUTO_COMPACT_WINDOW), opts) → zB8`.

- **Threshold** (`zB8(effW, opts)`): `testPctOverride` set & `0<pct≤100` → `min(⌊effW·pct/100⌋, effW−13000)`; else `effW−13000`. The `"compact"` level in `As4` fires when `enabled && tokens ≥ threshold`. So `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (→ `testPctOverride`) **is** consumed on the main interactive session, despite the "test" name.
- **Effective window** (`w1H`): `MU(model, AUTO_COMPACT_WINDOW).window − buffer`, where `MU` resolves `min(AUTO_COMPACT_WINDOW setting, model max window)`. When the setting is `auto` (default) it is just the model max.
- **1M tier:** `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` makes the 1M-capability check (`IR`/`bfH`) return false for Opus 4.7/4.8/Fable/Mythos → model max = **200k** (not 1M). So `/context` shows 200k and the window in the formula is 200k.
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is the **documented, precedence-taking** knob (binary: "the actual threshold is the minimum of this setting and your model's maximum context window"; "is set and takes precedence"). Settable via env or `/config → Auto-compact window`. Value: `auto` or `100k…1M` (parser `sAq`: accepts `k`/`m` suffix or a bare int ≥100 treated as ×1000).
- `autoCompactEnabled` (settings) / `DISABLE_AUTO_COMPACT` (env) is the on/off toggle.

> verified by: decompiled `bin/claude.exe` functions `zB8`/`nAq`/`As4`/`w1H`/`HYq`/`MCf`/`$UH`/`IR`/`bfH`/`sAq` — 2026-06-17 session.

## Policy

- **Prefer the window knob; leave a margin above the floor.** Pin `CLAUDE_CODE_AUTO_COMPACT_WINDOW` so the trigger (`window − 13k`) sits a comfortable margin (≥ ~50k) above the ~150k floor; **do not** set `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` on the main session and **do not** set `CLAUDE_CODE_DISABLE_1M_CONTEXT` unless you have a reason — letting 1M ride is fine because the window pin caps the trigger regardless. Current: `CLAUDE_CODE_AUTO_COMPACT_WINDOW = "300000"` (+ top-level `autoCompactWindow`), no PCT, 1M on → trigger ~287k. (The window-pin approach landed in `83fa383` at 400k; lowered to 300k via `set-context-cap.sh` for a tighter context/cost ceiling.)
- **Never aim a cap at or below ~200k.** The ~150k post-compaction floor is a hard lower bound on the usable trigger; a target cap near it thrashes (see Failure modes). If you genuinely need a tighter active session, use `/compact` by hand — don't lower the auto-trigger into the floor.
- **Env is read at session start** — a settings change takes effect only on the **next Claude Code restart**, never the current session. After any change: **restart and verify in the new session via `/context`** (window + "will trigger soon" line) before trusting it.
- **`apply-settings.sh` is additive — dropping a key from base does NOT remove it from live.** `env = base.env + live.env` with live winning on conflict. So (a) a stale live value silently shadows base (the drift that caused the overshoot), and (b) when you *remove* a key from `base.json` (as `fc7c5ce` removed `pct`/`DISABLE_1M`) you must also delete it from `~/.claude/settings.json` by hand or it persists. Always verify the live `env` after applying.
- **Spawned `claude -p` sub-agents:** `scripts/spawn-specialist.py` may inject `--settings '{"env":{...}}'` (precedence above file `env`, per-key). Keep sub-agent caps subject to the same floor rule. See [[claude-code-settings-env-precedence]].

**Why:** keep the working context bounded so cost (cache read/write scales with retained context) and quality stay predictable, independent of which model/window is active. See [[token-economy-plan]].

**How to change the cap:** use `scripts/set-context-cap.sh <tokens>` (writes base.json + applies). It pins `CLAUDE_CODE_AUTO_COMPACT_WINDOW = tokens + 13000` so the trigger lands at the requested cap, and **refuses** any cap below ~200k (floor + margin) rather than producing a thrash config. Then restart and verify via `/context`.

## Per-model window table (1M re-enabled by `83fa383`)

| Model | Max window | Effective autocompact window | Notes |
|---|---|---|---|
| Opus 4.7 / 4.8 | 1M | min(`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, 1M) = **300k** | 1M tier on (`DISABLE_1M` dropped); trigger ~287k |
| Fable | 1M | min(window setting, 1M) = **300k** | 1M-capable, tier on |
| Sonnet | 200k | min(300k, 200k) = **200k** | base tier; window setting can't exceed model max |
| Haiku | 200k | min(300k, 200k) = **200k** | base tier |

> The window-pin only *lowers* below the model max (`min`), so a 300k pin on a 200k model just yields 200k (trigger ~187k — still well above the floor). Setting the pin *higher* than the model max has no effect. The only unsafe direction is aiming the resulting trigger at/below the ~150k floor.
