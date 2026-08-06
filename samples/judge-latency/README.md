# Judge-latency samples

Raw evidence behind the five ceilings and five thresholds calibrated in stage 3 of
`judge-hook-timeouts-and-execution-counter-v2`. Committed inside the branch on purpose:
the plan spans several days, `/tmp` has its own TTL, and `check-live-run-evidence.py`
sits in `final_check` and re-runs **from canon** after the change lands.

## Estimator

- **p90** — nearest rank: `sorted(xs)[ceil(0.9*n) - 1]`. No interpolation.
- **Threshold** — `ceil(p90)`. The judge is expected to answer within it.
- **Per-call ceiling** — `ceil(max) + 1`, applied when a hook makes K >= 2 judge
  attempts. At K = 1 the per-call ceiling equals the hook budget instead.

Every latency below is wall-clock around `advisor.subprocess_runner`, measured with
`time.monotonic()`, one process at a time.

## The four calibrated rows

| pool | n | min | median | p90 | max | threshold | ceiling `ceil(max)+1` |
|---|---:|---:|---:|---:|---:|---:|---:|
| deferring | 18 | 10.29 | 17.43 | 37.58 | 39.99 | **38** | **41** |
| outage | 16 | 7.19 | 10.89 | 19.16 | 25.96 | **20** | **27** |
| feedback | 26 | 10.73 | 11.86 | 13.34 | 14.05 | **14** | **16** |
| binary_ask | 16 | 5.93 | 7.46 | 11.06 | 11.52 | **12** | **13** |

Provenance of each row, file by file:

| pool | composed from |
|---|---|
| deferring | `latency-sample.json:defer` (n=10) + `ab-sample.json:defer_std` (n=8) |
| outage | `latency-sample.json:outage` (n=10) + `ab-sample.json:outage_std` (n=6) |
| feedback | `latency-sample.json:feedback` (n=10) + `topup2-sample.json:feedback` (n=16) |
| binary_ask | `topup2-sample.json:binary_ask` (n=16) |

All 32 verdicts in `topup2-sample.json` are correct (`ok: true` on every row); the
sample measures latency, not accuracy, but a wrong verdict would have invalidated it.

## `topup-sample.json` is EXCLUDED — read this before using it

`topup-sample.json` is the first attempt at the `binary_ask` sample and the `feedback`
top-up. It is kept as evidence, **not** used in any row above. Its first calls are
2-3x the rest:

```
binary_ask  12.49 16.70 11.85 12.26 19.60 | 6.51 6.74 7.73 6.62 ...
feedback    15.91 18.30 15.19 16.67       | 11.10 11.50 10.97 10.60 ...
```

Cause: the coordinator was filing a ticket, calling the tracker API and writing files
in the foreground while the sampler ran. The `O_CREAT|O_EXCL` lock the sampler takes
guards against a **second copy of the sampler** — it does not guard against other work
on the same machine, which is exactly the contention that makes a sample invalid.

`topup2-sample.json` re-took both arms under the same lock with **no** foreground
activity. The slow head disappears (`binary_ask` starts at 7.60 instead of 12.49,
`feedback` at 10.76 instead of 15.91) and the rest of the series is unchanged, which
identifies the head as self-inflicted contention rather than a cold-start property of
the judge. Had the head survived the clean re-run, it would have been a real property
and would have had to go into the ceiling.

The exclusion moves numbers materially — `binary_ask` p90 16.70 -> 11.06, threshold
17 -> 12 — so it is recorded rather than silently dropped.

## Turn-end feasibility

`hook-turn-end-gate.py` runs three judges **sequentially on one budget**:
`judge_feedback_signal`, then `judge_binary_ask`, then `judge_outage_escalation`.
Against the approved budget of 45 s:

| basis | sum | fits 45? |
|---|---:|---|
| p90 of the three | 43.56 | yes, by 1.44 s |
| observed maxima | 51.53 | no |
| per-call ceilings (16 + 13 + 27) | 56 | no |

So the third judge completes at typical latency and is cut off on the tail. The plan
deferred exactly this inequality to this measurement ("выполнимость неравенства о
размере бюджета проверяется ПОСЛЕ предусловия"); the height is the user's call.

## Supporting samples (not part of the four rows)

- `lean-sample.json` — lean vs standard prompt A/B; shows the lean prompt is not
  uniformly faster and has a worse tail (`defer_yes` max 71.96).
- `haiku-sample.json` — the older haiku/sonnet baseline; the reason the original 5 s
  registration looked survivable and then silently stopped being so.

## Reproducing

`sample.py`, `ab.py`, `topup.py` / `topup2.py` are the runners as executed; `stats.py`
prints the table. They import `agentctl.advisor` from this branch and each call the
real judge, so a re-run costs real model calls and will not reproduce the latencies
exactly — only their shape.
