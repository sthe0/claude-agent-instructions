# Spawn-outcome baseline — the pre-fix reading, frozen

This note is the prose half of `spawn-outcome-baseline.json`. The JSON is the
machine-readable freeze; this file states the one predicate behind it, the
derivations of every rule the JSON carries, and the readings that were tested
and withdrawn.

Both files describe **the same ledger bytes**, and say so in a way a later
re-freeze cannot quietly break:

- `window_end`: `2026-08-19T22:40:53+00:00`
- `ledger_sha256`: `a7ebae90e949404ca4cb1f8d54fb87c74b6fc47ffd6cb44a7f53e640ac21b75c`

Those two strings are quoted here verbatim from the JSON. `spawn-outcome-report.py`
refuses to overwrite an existing baseline without `--force`, and the stage gate
asserts that both strings still appear in this note — so a `--force` re-freeze
that rewrote the JSON alone would be caught by the gate rather than by whoever
happened to read a diff.

## The measurement

Instrument: `scripts/spawn-outcome-report.py`. Ledger:
`~/.local/log/claude-spawn-costs.jsonl`, written by `scripts/spawn-specialist.py`.

**The subpopulation** is `exit_code == 0 AND malformed`. A child that exited
non-zero has an independent failure and belongs to a different question, so
those rows are excluded throughout rather than counted as a different defect.

### The one re-spawn predicate

A row counts as re-spawned when **both** halves hold:

1. the row is **itself exit-0-and-malformed**, and
2. a **later spawn row of the same kind with `malformed` false begins within
   1800 seconds of it**.

The rate is taken over **all spawn rows in the window** — that is the one
denominator, recorded in the JSON as
`respawn_denominator = "all_spawn_rows_in_window"`. The numerator restriction in
half (1) is recorded beside it as
`respawn_numerator = "exit0_and_malformed_rows_in_window"`, because it is
load-bearing rather than clerical.

### What the numerator restriction is worth

The same 1800-second same-kind predicate, over the same 854 frozen rows, with
only the candidate restriction varied:

| candidate rows may be… | count | rate |
|---|---|---|
| any row | 471 | 0.5515 |
| any exit-0 row | 466 | 0.5457 |
| exit-0 **and** malformed (**shipped**) | 108 | 0.1265 |

The unrestricted reading is not a different opinion about the same quantity: it
counts ordinary back-to-back spawning as redundancy. It is also not free. The
fix removes the same absolute set of rows either way — the 84 re-spawns whose
original carried an extractor timeout — so the unrestricted arm would move
0.5515 → 0.4925 at a 60% recovery, and detecting that needs **1124 rows per arm**
at a two-sided 0.05 rather than the 462 the shipped predicate needs. A predicate
stated with its denominator but not its numerator therefore leaves the gate's
size undetermined by roughly a factor of two and a half.

### The denominator, and why not the conditional one

The alternative is re-spawns *per malformed row*. Measured, that conditional
rate barely moves under a successful fix, because the extractor-timeout class's
own conditional re-spawn rate is barely above the rest of the population's. The
conditional reading measures the coordinator's propensity to redo a discard,
which this work does not touch; the all-rows reading measures how often the
fleet pays for a redundant re-spawn at all, which is what the fix removes.

### A withdrawn reading

A competing figure of **255 re-spawns at 50.1%** was quoted in the task brief.
It was tested against the ledger and **reproduces under no predicate**. Over the
whole log (1959 spawn rows), varying the two free parameters:

| predicate | count |
|---|---|
| same kind, 1800 s | 236 |
| same kind, 2400 s | 269 |
| same kind, 3000 s | 290 |
| any kind, 1800 s | 366 |

The ledger did not grow between the two counts. The reading is therefore
**withdrawn, not reconciled**: there is one measurement here, not two to
average.

## Why the window starts at efd3f45

`--window-start` defaults to **2026-07-27T14:26:43Z**, which is not a chosen
date but a read one: it is the timestamp of the first ledger row carrying
`extraction_reason`, and so dates the deployment of commit **efd3f45**, the
second-pass marker extractor.

**What it costs:** 1105 earlier spawn rows — more than half the log — are
excluded. They can be counted but not attributed, because they carry no cause
field at all.

**What it buys:** the excluded rows are evidence about a system that no longer
exists. The failure class this work removes is *that subprocess's own timeout*,
which could not occur before the subprocess existed. A baseline over the whole
log reads 26.3% where the regime being measured reads 23.4%, so freezing the
mixed figure would credit this work with a gain efd3f45 already banked. The
instrument still offers `--window-start all`, and prints an explicit banner that
the result mixes two regimes.

**And efd3f45 is itself the case for measuring rates at all.** Splitting the
1959 spawn rows at that timestamp:

- before: 315 / 1105 = **28.51%**
- after: 200 / 854 = **23.42%**
- two-proportion z = **−2.537**, p = 0.0112

The rate moved detectably — *and the dominant cause changed completely*: 152 of
the 191 attributable rows after that commit are the extractor's own timeout, a
failure mode that did not exist before it. The prior fix traded one cause for
another at a modest net gain. That is an outcome its test suite could not
report and only a rate measurement could see, which is why this instrument is
built and frozen **before** the first behavioural edit rather than after.

## The frozen reading

854 spawn rows, 2026-07-27T14:26:43Z → 2026-08-19T22:40:53+00:00, $2999.60.

| | count | rate | cost |
|---|---|---|---|
| exit-0-and-malformed | 200 | 0.234192 | $567.66 (18.9% of window spend) |
| re-spawned (predicate above) | 108 | 0.126464 | $224.63 |

Cause breakdown over the **191 attributable** rows (the other 9 malformed rows
carry no reason and are counted but not attributed):

| class | count | share of attributable |
|---|---|---|
| `extractor_timeout` | 152 | 79.58% |
| `no_marker_found` | 37 | 19.37% |
| `unrecognised_token` | 1 | 0.52% |
| `ok` | 1 | 0.52% |

> **One accounting convention worth naming.** `respawn_cost_usd` is the cost of
> the **distinct follower rows**, de-duplicated: a single clean spawn that closes
> out two malformed ones was still paid for once. Counting each pair's follower
> separately reads $260.85 over the same rows. The rate counts originals and the
> cost counts followers, which is deliberate — the rate answers *how often*, the
> cost answers *how much money*.

## The stopping rules, derived

Every rule below is **computed by the freeze from the rows it froze**, never
copied in from a plan. The figures here are read back out of the JSON. The gate
asserts the *relations* between them plus a drift band, so a later freeze over a
larger ledger neither breaks the gate nor ships a rule its own numbers do not
imply.

The mechanistic effect model is one assumption applied twice: the fix removes a
fraction of the extractor-timeout class, and nothing else. So

    p1 = p0 − recovery × (that class's share of window rows)

with the class contributing 152 rows to the malformed rate and 84 to the
re-spawn rate.

### Look 1 — a 60% recovery

| test | p0 | p1 | Δ | n per arm |
|---|---|---|---|---|
| malformed rate | 0.234192 | 0.127400 | 0.106792 | 238 |
| re-spawn rate | 0.126464 | 0.067447 | 0.059016 | **462** |

### Look 2 — a 40% recovery

| test | p0 | p1 | Δ | n per arm |
|---|---|---|---|---|
| malformed rate | 0.234192 | 0.162998 | 0.071194 | 577 |
| re-spawn rate | 0.126464 | 0.087119 | 0.039344 | **1134** |

`stopping_rule_min_n` is the max of its own look's two per-test sizes — 462 and
1134. The re-spawn test binds at both looks.

### The two-look boundary

Two looks are taken, both pre-registered here before any post-fix row is read,
and there is no third. The boundary constant is held equal at both looks and
solved against **this design's own information fractions** (0.4074, 1.0) rather
than borrowed from an equal-spacing table:

- nominal two-sided α per look: **0.028365**
- boundary constant: **z = 2.19220**
- correlation between the two looks: ρ = √(462/1134) = 0.6383
- **exact overall two-sided α = 0.050009**

(The design was solved at the planning-time reading of 464 and 1138, where the
same constant buys 0.050003. Holding the constant fixed while the frozen n's
drifted moves the overall α in the sixth decimal, which is recorded rather than
re-tuned — re-solving the boundary against each re-freeze would make the
pre-registration a function of when it was run.)

**Why two looks and not one.** The 60% recovery premise is sharply
load-bearing. Power of the binding re-spawn test against the true recovery:

| true recovery | p1 | power at look 1 (n=462) | power at look 2 (n=1134) |
|---|---|---|---|
| 100% | 0.0281 | 1.00 | 1.00 |
| 80% | 0.0478 | 0.98 | 1.00 |
| **60%** | 0.0674 | **0.80** | 0.99 |
| 40% | 0.0871 | 0.40 | 0.80 |
| 30% | 0.0970 | 0.22 | 0.52 |
| 20% | 0.1068 | 0.10 | 0.23 |

A single look at 60% records a real-but-smaller fix as FAILED with high
probability. The second look buys 40%-recovery detection at 0.80 instead of
0.40, for +72 rows always and +672 only when look 1 does not conclude. A
non-significant look 2 is final and routes to the difficulty cycle; extending n
after seeing it would be optional stopping.

### The look windows

`look_window_rows` = **37**, derived as one day of observed traffic
(854 rows over 23.34 days). `--check-delta` accepts `n_post` only in
`[462, 499]` or in `[1134, ∞)`, and refuses anything strictly between the two as
a third look. One day is the finest granularity at which the gate can be run
without the single look becoming a sequence of looks; the cost of a wider window
is exactly the α inflation the boundary constant was solved to control.

Overshooting look 1 is not fatal — it costs a wait until 1134 rows, not the
validity of the result. `--check-delta` reports how many more rows are needed,
so the look is reached by row count rather than by calendar.

## The two cause-level ceilings

A rate that falls is not on its own evidence the fix worked: it could have
substituted one failure class for another, or left the class it targets
untouched. Both are conditions of `--check-delta` exiting 0.

### The extractor-timeout residual

The 60% assumption implies its own residual — the class does not vanish:

    residual = (1 − 0.60) × 152/854 = 0.0711944

Sampling at `n_post` adds a one-sided 95% normal allowance on top:

| n_post | ceiling | smallest tripping count |
|---|---|---|
| 462 (look 1) | 0.090873 | 42 of 462 |
| 1134 (look 2) | 0.083755 | 95 of 1134 |

**Break-even recovery = 0.4894**: below that true recovery, the observed timeout
share exceeds the look-1 ceiling. Demanding the class be reduced to near zero
would have failed the design's own success case, which is why the ceiling is
derived rather than asserted.

### Class substitution

`substitution_ceiling_share_of_attributable` = **37/191 = 0.193717** — the
largest share of attributable rows held by any single pre-existing non-timeout
class. A cause class **absent from the frozen breakdown** trips it only when its
**Wilson one-sided 95% lower bound** on its share of post attributable rows
exceeds that ceiling:

| post attributable n | smallest tripping count | its lower bound |
|---|---|---|
| 20 | 7 | 0.202260 |
| 55 | 16 | 0.201881 |
| 150 | 38 | 0.199648 |

A 3-row fluke cannot fire it (3 of 20 has a lower bound of 0.0616). efd3f45's
own substitution — one new class taking 79.6% of attributable rows — would have
fired overwhelmingly.

## Invariants

- The live ledger is **read only**. The instrument never writes to, rotates or
  rewrites it, and no test reads it.
- The baseline is **frozen once**, by the procedure that created it, and is
  never recomputed after a behavioural change lands. `--freeze-baseline` refuses
  an existing path without `--force`.
- The stage's verify command performs **no write of any kind**, so re-running it
  cannot change what it verifies.
- No stopping rule, ceiling or rate is ever quoted as a literal by anything that
  reads the frozen file. A drift-band breach is a difficulty to record, never a
  band to widen: a breach means the regime moved and the recovery assumption is
  what needs re-deriving.
- `--check-delta` fails **closed** on insufficient evidence. The honest failure
  of a delta gate is "not yet measurable", never a pass by default.
- The post arm starts at the **first ledger row carrying `outcome_class`** — a
  boundary the ledger can date, discovered exactly as `--window-start`'s default
  was. The rows between `window_end` and that row are pre-fix traffic belonging
  to neither arm; the instrument reports how many were excluded and accepts an
  explicit `--post-start` only if it is *later*.
