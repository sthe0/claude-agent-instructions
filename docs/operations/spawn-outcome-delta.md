# Spawn-outcome delta — the real-world look is carved into issue #162

This note is the discharge point for **R8**, the order requirement behind
stage 8 of the `spawn-outcome-typing` plan: that the fix landed in stages
1–7/9 be shown, by measurement against the frozen pre-fix baseline in
`spawn-outcome-baseline.json`/`.md`, to actually move the exit-0-and-malformed
rate and the same-kind re-spawn rate — not merely to compile and pass its own
unit tests. That requirement is **unchanged and undiluted** by anything below.
What moved is *where* it discharges, not what it demands.

## The carve-out

Stage 8's original design made the plan's own `agentctl` session passable only
once the real ledger accumulated **462 post-fix rows** for look 1 (1134 for
look 2) — at the observed rate of 36.3 spawns/day, about **12.8 calendar days**
to look 1 and **31.3** to look 2. Run for real on 2026-08-21, the stage's own
`verify_command` failed exactly as designed: 1 post-fix row of 462 needed,
`STATUS: not-ready`. That failure went through `agentctl`'s
declare → investigate → critique cycle, which concluded the fault was
<!-- Language exception: нормативное/норма are the settled SMD source-ontology terms used throughout the overcome-difficulty skill; preserved verbatim for traceability. -->
**нормативное**: the plan's own goal-to-criterion mapping bound an inherently
multi-week observation to a single delivery session's own passability — a
норма problem, not a means or resource problem (see stage 8's
`[stage.principle]` in the plan TOML for the full derivation).

By the user's explicit decision of 2026-08-21 (session order element **O6**,
disposed as **cut** — recorded on the plan's `[meta] done_criterion` and
`[meta.order.coverage]` for this exact reason), the wait is carved out of this
plan's own session lifetime and into
[issue #162](https://github.com/sthe0/claude-agent-instructions/issues/162), a
standing record that outlives this session. That issue already carries:

- the frozen baseline, verbatim (`window_end` `2026-08-19T22:40:53+00:00`,
  `ledger_sha256` `a7ebae90e949404ca4cb1f8d54fb87c74b6fc47ffd6cb44a7f53e640ac21b75c`);
- the accumulation state as of the carve-out (1 post-`outcome_class` row, 461
  still needed for look 1);
- a live, detached poller (`spawn_outcome_trigger.py`, run on a loop against
  the **real** ledger, entirely outside this plan's or this repo's own
  verification surface) that posts an update to the issue itself the moment
  either pre-registered look resolves.

Every rate, both cause-level ceilings, and the two-look design from the
original stage 8 carry into issue #162 **byte-for-byte** — the stopping-rule
minimums (**462** rows for look 1, **1134** for look 2, a **37**-row look
window), the nominal per-look α, the extractor-timeout residual ceiling, and
the substitution ceiling. Nothing here is a weaker number; the number does not
change, only where it is checked.

## What THIS document does — and does not — claim

**As of this stage's landing, no real look has happened.** This document
verifies the measurement **automation**, not the measurement itself:

`spawn_outcome_trigger.py` — the exact script issue #162's poller runs against
the real ledger — was exercised via `--dry-run` against three synthetic
ledger scenarios, each built fresh by `make_synthetic_ledger.py` from the
frozen baseline's own rates and never read from or written to the live ledger
at `~/.local/log/claude-spawn-costs.jsonl`:

1. **Insufficient evidence** (10 post rows, well under the 462-row look-1
   floor) → `STATUS: not-ready`.
2. **Pass** (464 post rows at a malformed/re-spawn rate far below the frozen
   baseline) → `STATUS: ready (dry-run)`, composed comment carries
   **PASS (gate held)**.
3. **Fail** (464 post rows at the malformed rate held flat — not fallen —
   against the frozen baseline) → `STATUS: ready (dry-run)`, composed comment
   carries **FAIL (gate did not hold)**.

All three ran with `--dry-run`, so none of them ever reached the `gh issue
comment` call; this stage's own verification cannot post to the public issue.
Both the trigger script and the synthetic-ledger generator are personal
scratch tools (`/home/the0/cc-scratch/spawn_outcome_trigger.py`,
`/home/the0/cc-scratch/make_synthetic_ledger.py`) kept out of this repo by
design — they are not a mechanism other developers need, only a
verification harness for one plan's carved-out follow-up.

**The real-world statistical delta — R8's original ask — will be reported in
issue #162 as it resolves.** This document records the carve-out decision and
the automation's synthetic verification; it is not, and does not claim to be,
a real-world measurement.
