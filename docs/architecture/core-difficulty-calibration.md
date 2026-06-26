# Core-difficulty mass threshold — calibration

*Difficulty removed: a guessed flagging threshold either floods the author with noise or buries
real recurring Core difficulties; the value must be derived from a stated basis and be
recalibratable against a real channel stream.*

This note records the formula choice, the chosen value of `core-difficulty-mass-threshold`
(config.md), and how to recalibrate. It closes ADR-0001 open question #2.

## Formula

A difficulty cluster's **mass** is the sum of a severity-weight ladder over its member reports:

| severity | weight |
|---|---|
| low | 1 |
| medium | 2 |
| high | 4 |
| critical | 8 |

The ladder is **geometric** (one high ≈ two mediums ≈ four lows) so that severity, not raw
report count, drives the mass — a single serious report outweighs a pile of trivia. The weights
live in `scripts/difficulty_channel/port.py` (`Severity.mass`); the digest
(`scripts/core-difficulty-digest.py`) sums them per cluster.

**Recency-decay** was considered and deliberately deferred: the channel stream is currently
single-author and low-volume, so an undecayed sum is faithful. When the stream grows, multiply
each report's weight by a decay factor `0.5 ** (age_days / half_life_days)` before summing — the
digest's `Cluster.mass` is the one place to add it.

## Chosen value

`core-difficulty-mass-threshold = 8`, with a **critical short-circuit** (any single critical item
flags the cluster regardless of mass).

**Basis.** `8` is the mass of:

- four independent medium reports (4 × 2), or
- two high reports (2 × 4), or
- one critical (8, also covered by the short-circuit).

A *single* high report (mass 4) does **not** flag — one report is not yet recurrence. The
threshold encodes the same principle as the experience-leaf unit: the thing worth a Core change
is a **recurring** difficulty, not a one-off. `8` is the point at which a difficulty has recurred
across enough independent reports (or arrived severe enough) to justify the cost of a batched,
author-approved Core change over the human spine.

## How to recalibrate

The threshold is a heuristic; treat the first weeks of a real channel stream as calibration data.

1. Run `core-difficulty-digest.py` against the real channels over a window (e.g. weekly).
2. Inspect the **flagging rate**:
   - *Too many* flagged clusters (the author drowns in proposals, most not acted on) → the value
     is too low; raise it (e.g. to 12 = six mediums / three highs) or enable recency-decay.
   - *Too few* (real, repeatedly-felt Core difficulties never surface) → too high; lower it
     toward 6 (three mediums) or audit whether reports are mis-clustering (functional-ground too
     coarse — see ADR-0001 open question #1 refutation).
3. Change the value in `config.md` only (the digest reads it by key — no code change), and update
   the basis sentence here so the number always carries its rationale.

The refutation condition (ADR-0001 stage principle): if the chosen threshold flags too many or too
few clusters against the real stream, it is mis-calibrated and this procedure adjusts it —
measured by the flagging rate over a real window.
