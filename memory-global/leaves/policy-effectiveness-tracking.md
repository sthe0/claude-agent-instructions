---
name: policy-effectiveness-tracking
description: Standing instrument that tracks the model/sub-agent invocation policy over time along two axes — efficiency (token cost, $, the user's attention) and effectiveness (proxies for task-resolution quality) — via a per-session ledger + weekly scorecard, so "policy → measured outcome → policy adjustment" is a closed loop instead of a hand-computed one-off jq audit.
schema: difficulty/v1
type: reference
refs:
  - delegatable-work-patterns
  - token-economy-plan
  - coordinator-objective
  - self-improvement
created: 2026-06-17
last_verified: 2026-06-24
---

## Difficulty

The model/sub-agent invocation policy (CLAUDE.md § Cost discipline + § Recognizing when to delegate, operationalized by [[delegatable-work-patterns]]) is only ever measured **ad hoc**. The 48h audit that produced `delegatable-work-patterns.md` (2026-06-17: 21 haiku / 0 sonnet / 27 opus spawns, 44/48 `model:"inherit"`→opus, ~2150 inline main-thread Read+Bash, `Agent` used in only ~21/65 sessions) had to be hand-computed with throwaway `jq`. There is **no standing instrument, no trend, and therefore no closed loop** from "policy → measured outcome → policy adjustment": a rule can be added to CLAUDE.md and no one can tell, a window later, whether it moved the metric it targeted.

## Order & criterion

The instrument is `scripts/policy-scorecard.py` over a per-session ledger `~/.local/log/claude-policy-ledger.jsonl` (one upsert-keyed JSON row per session; a session is re-scanned only when its transcript mtime grew; a manual `quality_rating` survives re-scans). It reuses `scripts/cost-report.py`'s pricing/usage/attention helpers (imported by path — no copy-paste).

**Metrics per session** (efficiency, compliance, attention, effectiveness):

- **Efficiency** — tokens by model (in/out/cache_read/cache_create), main thread (opus) vs `subagents/*.jsonl` (their own model); `$` via the reused pricing table; cache-read share of cost; `$`/session.
- **Policy compliance (headline)** — `Agent` spawn count + model mix; **inherit→opus rate** (spawns that ran opus with no explicit cheap `model:`); **missed-delegation clusters** = main-thread runs of ≥8 consecutive mechanical calls (Read / Bash `cat|grep|rg|tail|head|sed|awk|jq|yt|curl-poll` / Grep / Glob) uninterrupted by an `Agent` spawn — operationalizes Patterns A & B of [[delegatable-work-patterns]].
- **Attention** — AskUserQuestion, your prompts, interrupts, likely-correction prompts (reused `CORRECTION_RE`).
- **Effectiveness proxies** — resolution-confirmed (user-side confirmation phrase present), `REPLAN` count, `overcome-difficulty` invocations, sub-agent failures/non-clean returns, rework edits (repeated edits to the same file path).
- **Manual quality** — `quality_rating` (1–5) + note, attached out-of-band in **batch during the weekly review** (no per-resolution friction, no LLM-judge token cost).

**Modes:** `policy-scorecard.py [--days N] [--project P]` (upsert + print scorecard with trend-vs-previous-window arrows + a Flags section); `--ledger-only` (upsert silently, for the hook); `rate <session_id> <1-5> [--note "…"]` (attach a manual rating; accepts an unambiguous session-id prefix).

**Cadence:** `scripts/hook-policy-scorecard-due.py` (SessionStart, throttled once/7d via `~/.local/state/claude-policy-scorecard.stamp`) prints a one-line stderr nudge to run it. Nudge only — it does not auto-scan. Cron is avoided on purpose: recurring crons auto-expire after 7 days, breaking a weekly cadence; a throttled hook survives restarts and never expires.

**Criterion (review procedure — this is the loop closure).** When a Flag fires —
- inherit→opus rate > 0.5,
- missed-delegation clusters > 0.5/session,
- `$`/session up > 25% vs the previous window,
- resolution-confirmed rate down > 10 points,
- avg manual quality < 3 or down > 0.5 —

→ invoke `self-improvement` to adjust the policy (a CLAUDE.md rule, a delegation default, a hook), **then record the adjustment + the observed metric movement one window later in this leaf** (a Contexts entry). An adjustment with no recorded subsequent movement has not closed the loop.

## Contexts

- **2026-06-17 — instrument built (origin).** Created `policy-scorecard.py`, the ledger, the SessionStart nudge, and this leaf (plan `deep-watching-moore.md`). Baseline reproduction (`--days 2`) matched the hand-computed audit: 54 spawns (28 opus / 22 haiku / 4 sonnet), 46/54 no-explicit-model (24 resolved opus), 2446 main-thread Read+Bash, `Agent` in 23/77 sessions — every ratio matching the 65-session morning audit, absolute counts marginally higher only because the window is measured from wall-clock and captured ~12 newer sessions. First real review (thresholds, first ratings) is pending with the user. No metric-movement entry yet — the loop is armed, not yet closed once.

## Cost

The instrument's per-run cost is one transcript scan of in-window sessions, made cheap by the mtime-gated upsert (only changed sessions re-read) — the same economy [[token-economy-plan]] is built on. The cost it **removes** is the recurring hand-computed `jq` audit (≈30+ min each) *plus* the larger cost of an unmeasured policy: a delegation/model rule that silently fails to move its target metric, paid as opus-rate inline work indefinitely. See also [[coordinator-objective]] for the metric axes this scores against.
