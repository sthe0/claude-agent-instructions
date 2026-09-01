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

## The six calibrated rows

| pool | n | min | median | p90 | max | threshold | ceiling `ceil(max)+1` |
|---|---:|---:|---:|---:|---:|---:|---:|
| deferring | 18 | 10.29 | 17.43 | 37.58 | 39.99 | **38** | **41** |
| outage | 16 | 7.19 | 10.89 | 19.16 | 25.96 | **20** | **27** |
| feedback | 26 | 10.73 | 11.86 | 13.34 | 14.05 | **14** | **16** |
| binary_ask | 16 | 5.93 | 7.46 | 11.06 | 11.52 | **12** | **13** |
| approval_ask | 64 | 5.88 | 12.77 | 17.29 | 19.14 | **18** | **21** |
| landing_discipline | 16 | 3.88 | 4.96 | 6.37 | 15.38 | **7** | **17** |

`approval_ask`'s threshold/ceiling are as computed by `lib/judge_latency.py`
today; see "approval2-sample.json — the regime shifted" below for why this row
now spans two non-overlapping populations and why its median is not usable for
sizing anything.

Provenance of each row, file by file:

| pool | composed from |
|---|---|
| deferring | `latency-sample.json:defer` (n=10) + `ab-sample.json:defer_std` (n=8) |
| outage | `latency-sample.json:outage` (n=10) + `ab-sample.json:outage_std` (n=6) |
| feedback | `latency-sample.json:feedback` (n=10) + `topup2-sample.json:feedback` (n=16) |
| binary_ask | `topup2-sample.json:binary_ask` (n=16) |
| approval_ask | `approval-sample.json:approval` (n=16) + `approval-sample.json:not_approval` (n=16) + `approval2-sample.json:approval` (n=16) + `approval2-sample.json:not_approval` (n=16) |
| landing_discipline | `landing-discipline-sample.json:pr_proposing` (n=8) + `landing-discipline-sample.json:direct_push` (n=8) |

All 32 verdicts in `topup2-sample.json` are correct (`ok: true` on every row); the
sample measures latency, not accuracy, but a wrong verdict would have invalidated it.
Likewise all 32 verdicts in `approval-sample.json` (both arms) are correct — the
`approval` arm's expected answer is YES, `not_approval`'s is NO, and `ok` is exactly
that comparison, not a raw count.

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

## `approval2-sample.json` — the regime shifted

Taken because `approval-sample.json`'s max (11.42s) stopped bounding observed
latency: on a live session the same judge, on the real ask text of that
session, returned in 12.71 / 12.51 / 11.96s — past the ceiling the first
sample computed — and the judge ledger recorded two consecutive
`timed_out: true` rows for it minutes apart.

`approval2.py` is identical in method to `approval.py` — one process under an
`O_CREAT|O_EXCL` pid lock, the `approval`/`not_approval` arms alternating
inside it so machine-load drift hits both equally, N=16 per arm — and was run
with a **120s per-call timeout**, well above anything either sample measured.
That makes its 32 latencies **uncensored**: every call was left to run to
completion rather than being cut off at a budget. This is NOT true of the
production judge ledger — a call killed at the hook's whole-invocation budget
is recorded there at approximately the budget, not at how long it would
actually have taken — which is why the ledger's `timed_out: true` rows can
show a problem exists but cannot be used to size the fix.

The two samples' ranges do not overlap at all:

```
approval-sample.json   (n=32):  5.88 -- 11.42
approval2-sample.json  (n=32): 14.12 -- 19.14
```

i.e. the judge roughly doubled in latency between the two measurements, with
an empty gap between the fastest call in the second sample and the slowest
call in the first. The `approval_ask` row above pools all four series (both
samples, both arms) into one n=64 population per
`lib/judge_latency.py`'s own comment on that row — the merge is deliberate,
not an oversight, and that comment explains the one place it costs something
(the merged median falls inside the empty gap and describes no call that ever
ran, which is safe only because this judge's median is never used to size a
ceiling or a floor).

All 32 verdicts in `approval2-sample.json` are correct (`ok: true` on every
row, both arms), same as `approval-sample.json`.

## Turn-end feasibility

`hook-turn-end-gate.py` runs three judges **sequentially on one budget**:
`judge_feedback_signal`, then `judge_binary_ask`, then `judge_outage_escalation`.
Against the approved budget of 52 s:

| basis | sum | fits 52? |
|---|---:|---|
| p90 of the three | 43.56 | yes, by 8.44 s |
| observed maxima | 51.53 | yes, by 0.47 s |
| per-call ceilings (16 + 13 + 27) | 56 | no |

So all three judges complete even on the slowest run yet observed, and only the
sum of the per-call ceilings — each of which is already one second above its own
observed maximum — does not fit. The plan deferred exactly this inequality to this
measurement: the budget-size inequality is checked AFTER the precondition, in the
approved plan's own words —
<!-- Language exception: verbatim quote of the approved plan's condition; translating a citation stops it being one. -->
"выполнимость неравенства о размере бюджета проверяется ПОСЛЕ
предусловия". The height is the user's call, and 52 s is what was approved.

## `landing-discipline-sample.json` — sixteen distinct menus, no prefilter

`judge_landing_discipline_ask` (the semantic judge behind
`hook-resolution-reminder.py`'s PreToolUse landing-discipline check) has no
regex/content prefilter ahead of it — every invocation at an open resolution
gate consults the judge directly. Its sample therefore needed real wording
diversity rather than one text repeated: 8 `pr_proposing` and 8 `direct_push`
menus, each a distinct, hand-authored AskUserQuestion resolution-gate menu
(question + every option's label/description), run one process at a time
under an `O_CREAT|O_EXCL` pid lock with the two arms alternating inside it —
same discipline as `approval2.py`.

15 of the 16 verdicts are correct (`ok`, computed the same way as the other
rows' — the comparison against the arm's expected label, not a raw count).
The one miss (`direct_push` index 5, judged YES/proposes-PR when the expected
answer was NO) is a fixture-labeling artifact, not a judge accuracy miss:
that menu's own rejected option is worded "Оставить на review в другом
репо" —
<!-- Language exception: verbatim quote of the fixture's own option text; translating it would stop the quote from evidencing the vocabulary overlap it names. -->
its non-recommended alternative carries the same "review" vocabulary
the judge's YES criterion looks for, so the text itself is genuinely
ambiguous about which arm it belongs to, independent of the judge under
test. Either way the latency is still a real, countable call — dropping it
would undercount `n` for no reason the estimator cares about, and
`hook-resolution-reminder.py`'s own PreToolUse branch fails open on judge
unavailability, never on a judge answering wrong, so this is a note for
whoever authors the next fixture batch, not a defect in this stage's
measurement.

## `committed_data` — the one row that is NOT measured yet

`sample_committed_data.py` is written but **has not been run**: it costs real
model calls, which the stage that added the judge had no permission to make. So
`lib/judge_latency.py` carries `committed_data` as an unmeasured row with
`UNMEASURED_HOOK_CALLED_NOTE`, and that is deliberately not harmless the way the
other two unmeasured rows are — those run outside any hook, this one is named in
`HOOK_CALL_SEQUENCE`. `required_budget_s` therefore raises on it, and the test
`test_each_hooks_budget_covers_the_calls_it_declares` (in
[scripts/tests/test_hook_wiring.py](../../scripts/tests/test_hook_wiring.py))
fails with a `KeyError` naming this script. That failure IS the calibration
obligation; it is not a defect to route around.

Meanwhile `scripts/hook-guard-committed-data.py` sizes itself from
`LAST_RESORT_CEILING_S` (41 s) — the module's own documented rule for a judge
with no row, and strictly more conservative than any measured row's floor — so
the hook is honestly sized today, just not yet *checkably* sized.

To close it: run `python3 sample_committed_data.py` (16 calls, 8 per arm), pipe
the two latency lists through `stats.py`, replace the placeholder row with the
result plus `committed-data-sample.json` as its provenance, and the test above
goes green on its own.

## Supporting samples (not part of the four rows)

- `lean-sample.json` — lean vs standard prompt A/B; shows the lean prompt is not
  uniformly faster and has a worse tail (`defer_yes` max 71.96).
- `haiku-sample.json` — the older haiku/sonnet baseline; the reason the original 5 s
  registration looked survivable and then silently stopped being so.

## Reproducing

`sample.py`, `ab.py`, `topup.py` / `topup2.py`, `sample_landing_discipline.py` are the
runners as executed (`sample_committed_data.py` is written but not yet run — see
above); `stats.py` prints the table. They import `agentctl.advisor` from
this branch and each call the real judge, so a re-run costs real model calls and will
not reproduce the latencies exactly — only their shape.
