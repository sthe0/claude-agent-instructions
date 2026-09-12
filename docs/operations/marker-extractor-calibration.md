# Marker extractor latency — calibration note

Dataset: `docs/operations/marker-extractor-calibration.jsonl`, 24 rows, 4 input sizes spanning
`lib.marker_extract._WINDOW_MAX` (12000 chars), 3 repeats idle **and** 3 repeats under induced
load at each size. Measured by `scripts/measure-marker-extractor-latency.py`, which drives the
real `lib.marker_extract.subprocess_runner` through a 300s runner timeout — far above any
expected single-attempt latency, so no datapoint is truncated by the bound under measurement.

Unlike `docs/operations/advisor-calibration.jsonl` (sampled while the fleet happened to be busy,
loadavg 5.8–13.0), this machine is not reliably busy on its own, so the loaded condition was
**induced**: a bank of CPU-bound stress subprocesses (one per host CPU) ran for the duration of
the loaded phase, started 45s before the first loaded call to let load ramp, and torn down
immediately after. Every row records the 1-minute loadavg sampled at that row's own call start,
so idle vs. loaded is verifiable per row rather than asserted for the phase as a whole.

## Sample

| input_chars | condition | median elapsed_s | min | max | max/min | loadavg range |
|---|---|---|---|---|---|---|
| 1200 | idle | 28.869 | 22.711 | 29.281 | 1.29× | 0.44–0.52 |
| 1200 | loaded | 18.899 | 17.944 | 19.290 | 1.08× | 7.58 |
| 3500 | idle | 18.911 | 18.568 | 24.780 | 1.33× | 0.44–1.25 |
| 3500 | loaded | 20.675 | 19.891 | 20.870 | 1.05× | 10.41 |
| 7000 | idle | 21.587 | 20.971 | 21.849 | 1.04× | 0.63–0.80 |
| 7000 | loaded | 23.978 | 22.730 | 24.390 | 1.07× | 12.25 |
| 12000 | idle | 17.551 | 16.819 | 18.053 | 1.07× | 0.54–0.85 |
| 12000 | loaded | 20.868 | 18.797 | 21.179 | 1.13× | 12.95 |

Every call returned `rc=0` — no truncation, no subprocess failure, at either condition.

## Derivation

Same shape as `docs/operations/advisor-timeout-calibration.md`'s: `ceil_to_N(largest within-size
max/min spread x minimum elapsed_s at the largest sampled input_chars)`. Two differences from the
sibling, both forced by this pass's own workload rather than chosen for convenience:

1. **The spread and the minimum are taken over the POOLED (idle+loaded) rows**, not idle alone —
   the failures this stage exists to explain are tail events under load, and a dataset sampled
   only while the machine was idle would produce a constant tight enough to reproduce the very
   defect being removed.
2. **Rounding is to the nearest 5 seconds, not the nearest minute.** Advisor's constant lives in
   the hundreds of seconds; this one lives in the tens, so rounding to the minute would blow past
   the value entirely. 5s keeps a proportionally similar rounding-to-value ratio (~15–20% of the
   value here, ~12% of advisor's 480 at the minute).

Per-size within-size spread (max/min), pooled idle+loaded:

| input_chars | spread |
|---|---|
| 1200 | 1.6317989300044584× |
| 3500 | 1.3345540715208961× |
| 7000 | 1.1630346669209861× |
| 12000 | 1.2592306320233069× |

Largest spread: size 1200, **1.6317989300044584×**. Minimum elapsed at the largest sampled size
(12000): **16.819s** (an idle repeat — no loaded repeat at 12000 undercut it).

`ceil_to_5(1.6317989300044584 × 16.819) = ceil_to_5(27.445227239714987) = 30`

`lib.marker_extract._EXTRACT_TIMEOUT_S` carries this derivation at the literal, and
`scripts/tests/test_marker_extract_calibration.py` recomputes it from the raw committed dataset —
at full precision, not from the rounded digits displayed here — so an edit to this note can never
silently drift the shipped timeout.

## Escalation and refutation checks (the stage's own principle)

**Refutation check:** within-size spread must not dominate between-size drift, or a size-dependent
function would be justified instead of a constant. Largest within-size spread: **1.632×** (size
1200, pooled). Between-size drift (pooled median at max size / pooled median at min size):
18.425 / 21.0005 = **0.8774×** — actually a *decrease*, not growth, with size. Refutation does not
fire in the direction the sibling checked (spread dominating drift would argue for something
size-dependent); here drift runs the opposite way from what a size-dependent fit would need, which
argues even more strongly for a single constant: there is no size effect to be a function of. A
single scalar is used, matching the stage's own default.

**Escalation check (shape mis-specification):** not applicable in the sibling's sense — no
size-dependent fit was attempted, because the refutation check above rules one out before any
fitting is warranted.

## What the idle-only rows alone would have derived — and why they don't

Applying the identical rule to the idle-only subset (the same `derive_constant` callable, filtered
to `loadavg < 2.0`) yields a **different, smaller** constant:

Per-size idle-only spread: 1200: 1.2892871295847828×, 3500: 1.3345540715208961× (unchanged — no
loaded repeat at 3500 exceeded the idle range), 7000: 1.0418673406132277×, 12000:
1.0733694036506334×. Largest: **1.3345540715208961×** (size 3500). Minimum elapsed at the largest
sampled size (12000, idle-only): **16.819s** (same row — it was already an idle repeat).

`ceil_to_5(1.3345540715208961 × 16.819) = ceil_to_5(22.445864931904952) = 25`

25 is strictly below the pooled 30 — `scripts/tests/test_marker_extract_calibration.py` asserts
this inequality mechanically (`test_idle_only_derivation_is_strictly_below_the_pooled_one`) and
that the shipped value is not itself reproducible from the idle rows alone
(`test_shipped_constant_is_not_reproducible_from_idle_rows_alone`), so an idle-only derivation is
excluded by the gate rather than by this paragraph. An idle-only measurement would have called the
present 30s comfortably generous (30 vs. an idle-justified 25) — exactly the gap that let 150 real
extractor calls exceed 30s in production traffic this stage's own baseline measured, since idle
evidence alone cannot see the tail that lives under load.

## What the pooled derivation could NOT reproduce

The sibling advisor note measured a within-size spread of **4.17×** under real fleet contention
(loadavg 5.8–13.0, driven by other sessions' concurrent work, not induced). This pass's own
induced load reached a comparable loadavg range (7.58–12.95) but only local CPU contention — a
much narrower stressor than a busy fleet's actual API-server-side queuing — and produced a
markedly smaller spread (1.63× at most). The sibling's 4.17× is cited here **only as the prior
that motivated sampling under load at all**; it is not, and must not become, an input to this
pass's own constant, because it was measured over payloads up to 203681 characters against this
pass's 12000-character ceiling — the same cross-range borrowing this stage's method forbids, just
on the load axis instead of the size axis. The practical consequence: this calibration's own
induced-load rig could not reproduce the tail that the 150-timeout baseline implies really exists
in production (real multi-session contention, not one machine's own CPU). The derived 30s is
therefore the honest output of THIS measurement, not a claim that it eliminates the tail — that is
what stage 3's retry-then-fallback exists to absorb regardless of exactly where the per-attempt
cap sits.

## Cost of raising the cap

The shipped constant did not move (30s in, 30s out) — measured against the pre-existing value,
there is no new cost to a genuinely hung extractor here: a hang still occupies at most 30s per
attempt before stage 3's retry (up to 3 attempts, 90s overall deadline) takes over, same as before
this stage ran. The finding is not "raise the cap"; it is that the pre-existing 30s, previously
justified only by comparison to `advisor.py`'s 20s, now rests on a measurement that happens to
agree with it, and the note records exactly how much of that agreement is idle-derived (25s) vs.
load-derived (30s) so a later reader does not have to re-measure to find out.

## Sampling rule and its one recorded deviation

Sizes are 1200, 3500, 7000 and 12000 characters — spanning `_WINDOW_MAX` from a small marker-only
reply up to the pass's own elision boundary — built from real prose (this repo's own commit
messages, many of them literally a spawned developer's `COMPLETED` summary) padded to each target
length, never filler. The idle phase ran first, sequentially, each repeat preceded by a wait for
loadavg to clear 2.0; the loaded phase ran second, all repeats at a given size launched
concurrently against a stress bank sized to the host's own CPU count, mirroring a real spawn
tail's shape (several sessions landing together) rather than an artificial single-process
stressor.

The sampled specialist-output TEXTS are deliberately not committed — this repo's venue is public
and the texts are drawn from live commit history rather than being synthetic filler; only the
measured numbers land in `marker-extractor-calibration.jsonl`, matching the sibling note's same
constraint and resolution.
