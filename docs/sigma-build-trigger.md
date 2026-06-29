# σ build-trigger — when to build the σ operator

> Operational distillation of [ADR-0002](adr/0002-dialectical-transition.md). The ADR records the *model* (the difficulty-primitive-by-tier; κ vs σ; the dialectical transition). This document records the *decision criterion*: under what observable conditions the σ operator stops being premature and gets built — and the kill-condition under which the idea is archived instead.

## The principle under test

> **P0 — "the manual path suffices."** The existing engines — κ/critique plus the manual `promote-scan` (Rule of Three) — are enough to handle tier-1 difficulties (refuted principles). No automated σ operator is needed.

The decision "build σ or not" is itself a tier-1 difficulty. Rather than decide by intuition, we **pre-register the experimentum crucis** that would refute P0. The empirical probe (ADR-0002 § Empirical probe) showed the σ-fuel is real but rare in product work (~7 %) and concentrated in self-improvement (~65 %), and the one product instance was already promoted by hand — so P0 is *not yet* refuted. This instrument exists to detect, falsifiably, the moment it is.

## The build-trigger: three pre-registered refutation conditions

The trigger is **not** "there is enough fuel" — there already is some, and it is not enough. The trigger is **"the manual path has sprung a leak."** Three measurable conditions; each lights up exactly one code seam (ADR-0002 § the three seams), reusing existing thresholds where possible.

| Condition | Signal | Threshold | Seam it lights | Status |
|---|---|---|---|---|
| **(A)** re-refutation of an *already-promoted* principle | a tier-1 difficulty whose `P1` is already in the instructions is refuted again | ≥ `principle-promotion-threshold` (Rule of Three) | tag + registry | **measured now** |
| **(B)** rising rate of missed promotions | tier-1 signatures run as tier-0 replans and never promoted | ≥ `principle-promotion-threshold` per scan window | tier verdict in critique | **deferred** — needs accumulated tag history + a temporal series |
| **(C)** proliferation pressure at the reduction floor | `CLAUDE.md`/policy hits the dedup-floor, needs reformulation, takes a patch-boundary instead | ≥ 1 cycle with no reformulation | synthesis-scan | **cheap proxy reported now; reframing-discriminator deferred** |

Why (A) is measurable now and (B) is not: (A) is a **static cross-check** — match a tier-1 leaf's refuted principle against the already-promoted `principles/` corpus; it reuses the existing `principle-promotion-threshold`, no new guessed number. (B) must *infer* missed tier-1 cases from unlabeled tier-0 records — a noisy classifier whose ground truth is exactly the `tier` tag this slice only now introduces (instrument-before-baseline), and it needs a temporal series the young corpus does not yet have. (C)'s cheap proxy (corpus size, near-duplicate density) is low-cost and reported as plain numbers; its discriminating clause — "growth *without reformulation*" — needs semantic diff-history judgment and is deferred to avoid false alarms.

**Build is incremental:** a condition fires → only its seam is built, never the whole rig.

## Bootstrap caveat

To *see* (A)/(B) at all, a minimal instrument is needed. The near-zero **`tier` tag** ("clean run, difficulty stands") plus a periodic read-only scan is **a σ-sentinel, not σ machinery** — its justification today is to measure the trigger, not to build the operator. Without it, only (C) (a ceiling linter) and partly (A) (a duplicate surfacing during search-before-write) are visible; (B) is invisible. Hence the split:

- **justified now:** the σ-sentinel (the optional `tier` tag + `sigma-sentinel.py`);
- **deferred until the first trigger fires:** the build itself, and only the seam that fired.

## Deferred increment → activation trigger

The deferred work is **scheduled, not dropped**. Each item names the observable that starts it:

| Deferred increment | Picked up when | Builds |
|---|---|---|
| Signal **(B)** — missed-promotion rate | the `tier` tag has ≥ 1 full scan-window of accumulated history **OR** condition (A) first flags (whichever comes first) — at that point the baseline exists to calibrate (B) against | the tier-verdict emission in critique |
| The dear **(C)** reframing-discriminator | the cheap (C) proxy first reports a corpus that *looks* like proliferation (size/near-dup numbers a human judges as floor-pressure) | the `synthesis-scan` semantic diff-history check |
| The **σ operator** itself (per-seam) | the corresponding trigger condition (A/B/C) fires at its threshold | only the lit seam — tag+registry (A) / tier-verdict (B) / synthesis-scan (C) |
| The reflexive horizon `σ(κ, σ)` | **not scheduled** — recorded in ADR-0002 as a horizon requiring hard boundaries, not a planned increment | — |

## Kill-condition (falsifiable in both directions)

If, over **~1 year** of the σ-sentinel running, none of (A)/(B)/(C) ever fires and the manual path does not leak, then **P0 "the manual path suffices" is corroborated** — the σ idea is archived (ADR-0002 superseded by a short note), and the σ-sentinel may itself be retired. Pre-registering the corroboration that kills the project is the symmetric half of pre-registering the refutation that starts it.

## What the digest measures vs decides

`scripts/sigma-sentinel.py` is **read-only**. It *measures* condition (A), *reports* the cheap (C) proxy as numbers, and *logs* (B) and the dear-(C) discriminator as out-of-scope (no silent cap). It **does not decide** to build σ and **does not build** anything — the decision, when a trigger fires, routes through the normal `planner → approval → developer` spine like any other Core change.
