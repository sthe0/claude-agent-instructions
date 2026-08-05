# Advisor enumeration latency — calibration note

Dataset: `docs/operations/advisor-calibration.jsonl`, 15 rows, 5 distinct plan sizes, 3 repeats each.
Measured by driving the real `advisor.enumerate_questions_health` through `functools.partial(advisor.subprocess_runner, timeout=600)`, so no datapoint is truncated by the 20 s cap under repair.

## Sample

| input_chars | median elapsed_s | min | max | max/median | loadavg range |
|---|---|---|---|---|---|
| 2853 | 18.0 | 15.1 | 18.4 | 1.02 | 5.9–6.8 |
| 23018 | 27.1 | 23.1 | 96.5 | 3.56 | 5.8–13.0 |
| 29849 | 97.0 | 25.0 | 101.5 | 1.05 | 6.5–10.7 |
| 75672 | 85.3 | 85.2 | 125.3 | 1.47 | 6.4–9.6 |
| 203681 | 107.4 | 103.2 | 170.7 | 1.59 | 5.8–9.5 |

## Fits — three two-parameter shapes `t = a + b*g(chars)`

| shape | a | b | SSE | RMS residual (s) | predicted at max size | max signed under-prediction |
|---|---|---|---|---|---|---|
| log | -158.011 | 21.7324 | 2077.40 | 20.38 | 107.7 s | +47.20% |
| sqrt | 18.984 | 0.217293 | 2551.63 | 22.59 | 117.1 s | +71.66% |
| linear | 42.771 | 0.000361181 | 3440.74 | 26.23 | 116.3 s | +81.20% |

Per-size residuals (median − fit), seconds:

| input_chars | log | sqrt | linear |
|---|---|---|---|
| 2853 | +3.1 | -12.6 | -25.8 |
| 23018 | -33.1 | -24.8 | -24.0 |
| 29849 | +31.1 | +40.5 | +43.5 |
| 75672 | -0.8 | +6.6 | +15.2 |
| 203681 | -0.3 | -9.7 | -9.0 |

**Winning shape: `log`.** no tie: the winning SSE is more than 10% below the runner-up.

## Plateau — DIAGNOSTIC ONLY, never shipped

Best three-parameter plateau `t = a + b*min(chars, knee)`: knee = 39499 chars, a = 3.664, b = 0.00234977, SSE = 1779.96 (against 2077.40 for the winning two-parameter fit).
It is reported and not shipped because stage 3's constant block represents exactly `a + b*g(n)`, and a third free parameter could not be provenance-checked by `--assert-matches-module`. If the plateau's SSE is far below the winner's, the two-parameter approximation is carrying a real interior knee as residual — which is what the escalation check below exists to catch.

## Escalation check (shape mis-specification)

Trigger: `max over sampled sizes of (median(n) − fit(n)) / fit(n) > 0.5`. Observed for the winner: **+47.20%** → no escalation.

## Refutation check (the stage's own principle)

Within-size spread (max/min per size): 2853: 1.22×, 23018: 4.17×, 29849: 4.06×, 75672: 1.47×, 203681: 1.65× — largest **4.17×**.
Between-size drift (median at max size / median at min size): **5.96×**.
Refutation does not fire: between-size drift dominates the within-size spread, so a size-dependent function is justified.

## Input to stage 3's SAFETY_FACTOR

Largest within-size `max/median` ratio: **3.558**; floored at 1.5 → **3.558**.

## Sampling rule and its two recorded deviations

Sizes are the min, p25, median, p75 and max of the plan park sorted by character count at the moment the stage ran, excluding this task's own plan. Two facts about the operative park are recorded here rather than left implicit:

1. **The park was filtered to plans `agentctl.plan.load_plan` accepts** — 35 of 66 files in `~/.claude-agent/plans/` (excluding this task's own). A file the loader rejects has no `meta.goal` / `meta.done_criterion`, so `enumerate_questions_health` could never be called on it; the rejected files are probes and pre-schema drafts, the smallest being 40 characters. The operative park is therefore the loadable one, and the quantiles are taken over it.
2. **Each sampled plan's text was snapshotted once, at selection time**, and all repeats ran against that snapshot. The park is state other live sessions write — the sampled max, `smd-act-defects-8.toml`, belongs to a session that was actively executing while this stage ran — so re-reading per repeat would have let `input_chars` move under the measurement and broken the three-repeats-per-size grouping. Each row pins `plan_sha256` of the snapshot it measured.

The sampled plan TEXTS are deliberately not committed: this repo's venue is public and the park holds live org work.
