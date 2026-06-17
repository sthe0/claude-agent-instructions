---
name: autocompact-threshold-policy
description: Keep the auto-compaction trigger comfortably ABOVE the ~150k post-compaction floor — a trigger at/below the floor re-fires every turn (thrash). Primary knob CLAUDE_CODE_AUTO_COMPACT_WINDOW pins the effective window (precedence, value auto|100k–1M). Verified trigger = min(round((window−20k)·(1−frac)), window−33k), frac≈0.2 default (server-tunable). The /context "Autocompact buffer" (33k = 20k+13k, window-independent) is a DISPLAY reserve, NOT window−trigger. Current: window=300k, 1M on, no PCT -> trigger ~224k; min safe window ~270k.
metadata:
  type: feedback
---

**Difficulty:** keep the working context bounded before auto-compaction **without** destabilizing the session — and the hard constraint is that the auto-compaction *trigger* cannot live near the **~150k post-compaction floor** (the context a compaction leaves behind: system prompt + memory + recent turns). Env is read **once at session start**; a hook cannot re-threshold a live session.

There are **two opposite failure modes**, both seen on 2026-06-17:

- **Thrash — compact every turn (DEEPAGENT-430, session `323be019`, ~18 events).** Commit `e8abc05` set `window=200k` + `pct=75` + `DISABLE_1M=1`. With the verified formula (effW=200k, z=200k−20k=180k): trigger `min(round(180k·0.8), min(⌊180k·0.75⌋, 180k−13k)) = min(144k, 135k) = 135k`. That sits **below the ~150k floor**: immediately after each compaction the retained context is already ≥ the trigger, so it re-compacts on the next turn, forever. The whole premise "cap context at ~150k" was *impossible* — you cannot trigger below where a compaction lands. Hand-fixed live, committed `fc7c5ce` + `83fa383`: `window=400k` (→ trigger ~304k), drop `pct`, re-enable 1M; later lowered to 300k (→ trigger ~224k) — both safely above the floor.
- **Overshoot — compact too late (earlier).** A long session ran to 199.9k/200k with no compaction. Not a code limitation: decompiling `claude.exe` confirms the main session *does* honor the percent override; the overshoot was **stale-env / config-drift** — `settings/base.json` held `70` while live `~/.claude/settings.json` held `75`, and the session started before an effective value was in place.

**Safe rule: the trigger must sit comfortably ABOVE the ~150k floor (margin ≥ ~50k → trigger ≥ ~200k → window ≥ ~270k at the default fraction).** A window whose trigger lands at or below the floor thrashes; a larger window is fine (just compacts later). When in doubt, err high. Because the fraction is server-tunable (see mechanism), treat the computed trigger as an estimate and keep the margin.

## Verified mechanism (decompiled main-session path)

The fire **trigger** and the `/context` **"Autocompact buffer"** are two different quantities from two code paths — do not confuse them.

**Effective window** (`w1H`): `z = min(AUTO_COMPACT_WINDOW setting, model max) − min(maxOutputTokens, 20000)`. The output reservation `min(maxOut, 20000)` is **20000** (Opus maxOut default 64000). So `z = window − 20000`. When the setting is `auto` it is just `model_max − 20000`.

**Fire trigger** (`Rr4` tests `tokens ≥ nAq(z, opts)`):
- `nAq(z) = min( z − round(z · frac), zB8(z) )` where `frac` = `precomputeBufferFraction` (default **0.2**, flag `tengu_amber_rokovoko`).
- `zB8(z)` = `min(⌊z·pct/100⌋, z−13000)` if `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`/`testPctOverride` is set, else `z−13000`. (The override **is** consumed on the main session despite the "test" name.)
- For normal windows the fraction term binds: **trigger ≈ round(z·(1−frac)) = round((window−20000)·0.8)**. The flat `−13000` only dominates when `frac` is tiny (≤ ~0.046).

**`/context` "Autocompact buffer"** (`pF8`): a window-independent display reserve = `min(maxOut,20000) + 13000` = **33000** (shown as 33000/window, e.g. 11% at 300k). It is **NOT** `window − trigger`. Don't size against it.

- **frac is server-driven** (LaunchDarkly flag `tengu_amber_moleskin`); the binary only carries the 0.2 fallback. A larger live frac lowers the real trigger, so the computed trigger is an estimate — keep margin / verify empirically.
- **1M tier:** `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` makes the 1M-capability check (`IR`/`bfH`) return false for Opus 4.7/4.8/Fable → model max = **200k**. With 1M on, model max = 1M, so the window setting governs.
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is the **precedence-taking** knob: `min(setting, model max)`. Settable via env or `/config → Auto-compact window`. Value: `auto` or `100k…1M` (parser `sAq`). `autoCompactEnabled` / `DISABLE_AUTO_COMPACT` is the on/off toggle.

**Worked example (window 300k, no override, frac 0.2):** `z = 280000`; trigger `= min(round(280000·0.8), 280000−13000) = min(224000, 267000) = 224000`. Display buffer = 33000.

> verified by: decompiled `bin/claude.exe` functions `Rr4`/`nAq`/`zB8`/`w1H`/`WYH`/`MU`/`IR`/`bfH`/`sAq`/`pF8` and constants `Ks4=13000`/`js4=20000`/`lAq=0.2` — 2026-06-17 session (byte-offset extraction; trigger derived from code, not empirically tripped).

## Policy

- **Prefer the window knob; leave a margin above the floor.** Pin `CLAUDE_CODE_AUTO_COMPACT_WINDOW` so the trigger (`≈ round((window−20k)·0.8)`) sits a comfortable margin (≥ ~50k) above the ~150k floor; **do not** set `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` on the main session and **do not** set `CLAUDE_CODE_DISABLE_1M_CONTEXT` unless you have a reason — letting 1M ride is fine because the window pin caps the trigger regardless. Current: `CLAUDE_CODE_AUTO_COMPACT_WINDOW = "300000"` (+ top-level `autoCompactWindow`), no PCT, 1M on → trigger ~224k. (The window-pin approach landed in `83fa383` at 400k; lowered to 300k via `set-context-cap.sh` for a tighter context/cost ceiling.)
- **Never pick a window below ~270k.** Its trigger lands at/near the ~150k floor and thrashes (see Failure modes). If you genuinely need a tighter active session, use `/compact` by hand — don't lower the auto-trigger into the floor.
- **Env is read at session start** — a settings change takes effect only on the **next Claude Code restart**, never the current session. After any change: **restart and verify in the new session via `/context`** (window + "will trigger soon" line) before trusting it.
- **`apply-settings.sh` is additive — dropping a key from base does NOT remove it from live.** `env = base.env + live.env` with live winning on conflict. So (a) a stale live value silently shadows base (the drift that caused the overshoot), and (b) when you *remove* a key from `base.json` (as `fc7c5ce` removed `pct`/`DISABLE_1M`) you must also delete it from `~/.claude/settings.json` by hand or it persists. Always verify the live `env` after applying.
- **Spawned `claude -p` sub-agents:** `scripts/spawn-specialist.py` may inject `--settings '{"env":{...}}'` (precedence above file `env`, per-key). Keep sub-agent caps subject to the same floor rule. See [[claude-code-settings-env-precedence]].

**Why:** keep the working context bounded so cost (cache read/write scales with retained context) and quality stay predictable, independent of which model/window is active. See [[token-economy-plan]].

**How to change the window:** use `scripts/set-context-cap.sh <window-tokens>` (writes base.json + applies + forces live). It takes the desired **window**, prints the expected trigger via the verified formula, and **refuses** any window whose trigger would fall below ~200k (floor + margin) — i.e. windows under ~270k. Then restart and verify via `/context`.

## Per-model window table (1M re-enabled by `83fa383`)

| Model | Max window | Effective window (z = effW−20k) | Trigger ≈ round(z·0.8) |
|---|---|---|---|
| Opus 4.7 / 4.8 | 1M | min(300k, 1M)=300k → z=280k | **~224k** (1M tier on) |
| Fable | 1M | min(300k, 1M)=300k → z=280k | **~224k** |
| Sonnet | 200k | min(300k, 200k)=200k → z=180k | **~144k** (sub-agent) |
| Haiku | 200k | min(300k, 200k)=200k → z=180k | **~144k** (sub-agent) |

> The window-pin only *lowers* below the model max (`min`), so the 300k pin caps Opus/Fable at 300k but is a no-op on 200k-max models (Sonnet/Haiku → effW 200k, trigger ~144k). That ~144k would be at/below the **main-session** floor (~150k), but the floor is config-dependent: sub-agents run a leaner system prompt + less memory, so their floor is well under 150k and ~144k is normally safe for them. The unsafe direction is always a trigger at/below whatever the *actual* post-compaction floor is for that session.
