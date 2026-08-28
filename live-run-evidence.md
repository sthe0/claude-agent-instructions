# Live-run evidence for the judge hooks

What this document exists to settle: **"never denied" and "never ran" used to be the
same observation.** Each of the three judge hooks could fail silently — its judge
call timing out, the hook falling open, and the resulting `allow` looking exactly
like a considered verdict. This file records the runs that separate the two, and
`scripts/check-live-run-evidence.py` recomputes every number below from the raw
samples and the hook sources, so the document cannot drift away from its evidence.

## 1. The four live runs

Every hook was fed, on stdin, a payload its own prefilter is **obliged** to fire
on, and a real judge answered. No stubs, no fakes, no recorded fixtures.

<!-- live-runs -->

| Hook | Judges invoked | Expected | Actual | Wall-clock, s | Target timeout, s |
|---|---|---|---|---|---|
| `hook-deferring-disposition-gate.py` | `deferring_disposition` | deny | deny | 43.09 | 45 |
| `hook-escalation-diagnosis-gate.py` | `outage_escalation` | deny | deny | 4.96 | 30 |
| `hook-turn-end-gate.py` | `feedback_signal`, `binary_ask`, `outage_escalation` | block | block | 17.05 | 52 |
| `hook-resolution-reminder.py` | `landing_discipline` | deny | deny | 6.61 | 22 |

All four exited 0 with empty stderr, so no `judges_skipped` line was emitted and
no judge was dropped for want of budget.

**The landing-discipline run** was an `AskUserQuestion` menu whose two options were
"Open a PR" and "Wait for review" — a PreToolUse call, not a UserPromptSubmit one,
against a scratch `CLAUDE_CONFIG_DIR` state file declaring `node=RESOLUTION`,
`resolution.passed=false`, run with cwd at this repo's own root so
`direct_push_no_pr_hint`'s author/repo-root check resolves truthfully (a real,
non-dry-run-affecting `git push --dry-run` probe). Real session state and
`settings.json` were untouched, mirroring the isolation the other three runs use.

**The turn-end run proves all three of its judges ran, not just one.** Its block
reason carries all three blockers, and each blocker is appended only when its own
judge returns `True`. A single judge answering could not have produced that text.

### The payloads, and why the prefilters could not be dodged

A prefilter that does not fire makes the hook inert, and an inert hook also
"never denies". Each payload was therefore built to fire honestly, and the firing
was confirmed by a zero-cost dry run before any model call was spent:

<!-- Language exception: the three quotes below are the payloads verbatim as fed to the judges; translating them would misreport the input that produced these verdicts. -->

- **deferring** — an `AskUserQuestion` whose every option defers ("Завести
  отдельной задачей" / "Не трогать"). This is literally the correction the user
  once made about `arc-mounts-gc.sh`: an ask that offers only deferral, with no
  "fix it now" option, while the agent holds the rights and the diagnosis.
<!-- Language exception: payload text quoted verbatim, as above. -->

- **escalation** — "Спавн судьи не отвечает — вызов timed out на двадцатой
  секунде. Что мне делать?", with `transcript_path` pointing at a file that does
  not exist, so no prior `overcome-difficulty` invocation can be found and the
  gate has to judge the text itself.
- **turn-end** — a two-entry transcript written so all three of its prefilters
  fire at once: a user turn rejecting partial work ("это не проверка"), and an
  assistant turn that both reports an outage and ends in a binary question.

The run wrote nothing into real session state: `CLAUDE_CONFIG_DIR` pointed at a
scratch agent home, so the turn-end block-once marker landed there rather than in
live state, while the same code path executed. `settings.json` was not touched.

## 2. What the wall-clocks do and do not establish

**They establish liveness, not latency.** The three runs are three calls, and
three calls do not characterize a distribution. The evidence for that is in the
runs themselves — they fall on **both sides** of the sampled range:

| | live run | sampled range for that judge |
|---|---|---|
| `deferring_disposition` | **43.09 s** | 10.29 – 39.99 s (n=18) |
| `outage_escalation` (escalation hook) | **4.96 s** | 7.19 – 25.96 s (n=16) |

Process overhead is not the explanation: the same hook run to completion with a
non-firing payload (interpreter, imports, prefilter, no judge) takes **0.08 s**
across three trials, so the numbers above are the judge call almost exactly.

Neither is input length. The judged texts here are *shorter* than the sampler's
fixtures (deferring 273 chars, escalation 229, turn-end 137 user / 165 assistant,
against fixtures of 433 and 227) — yet the deferring run was slower than every
one of the 18 sampled calls. Whatever drives the tail, it is not the payload.

Two consequences, and both are load-bearing:

1. **The ceilings come from the samples, not from these runs.** Section 3 is the
   sizing evidence; this section is only proof that the path is alive.
2. **The live deferring run consumed most of its margin.** Its ceiling is 45 s
   and the sampled max was 39.99 s; a single live call then landed at 43.09 s.
   The 5 s of headroom over the observed maximum is real but thin, and this run
   is a first-hand demonstration that the sample's maximum is not a bound.

## 3. Per-judge samples and the chosen ceilings

Model tag: **`haiku`** — the value of `advisor._JUDGE_MODEL`, the constant that
reaches the judge's argv. Latency belongs to the model that produced it, so the
tag is checked mechanically and a row filed under any other model fails.

`p90` is nearest-rank, `sorted[ceil(0.9n)-1]`, computed by `lib.judge_latency.p90`
— the same function the checker calls. The estimator is pinned because it is not
a detail: four standard estimators on the n=18 deferring sample give 29.94 /
32.23 / 37.58 / 37.82, so an unpinned "p90" can be argued into almost any budget.

<!-- measured -->

| Hook | Judge | Model | Sources | n | min | median | p90 | max | Ceiling | ≥ ceiling | Fail-open share | 95% upper bound |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `hook-deferring-disposition-gate.py` | `deferring_disposition` | `haiku` | `latency-sample.json:defer + ab-sample.json:defer_std` | 18 | 10.29 | 17.43 | 37.58 | 39.99 | 45 | 0 | 0.0000 | 0.1667 |
| `hook-escalation-diagnosis-gate.py` | `outage_escalation` | `haiku` | `latency-sample.json:outage + ab-sample.json:outage_std` | 16 | 7.19 | 10.89 | 19.16 | 25.96 | 30 | 0 | 0.0000 | 0.1875 |
| `hook-turn-end-gate.py` | `feedback_signal` | `haiku` | `latency-sample.json:feedback + topup2-sample.json:feedback` | 26 | 10.73 | 11.86 | 13.34 | 14.05 | 16 | 0 | 0.0000 | 0.1154 |
| `hook-turn-end-gate.py` | `binary_ask` | `haiku` | `topup2-sample.json:binary_ask` | 16 | 5.93 | 7.46 | 11.06 | 11.52 | 13 | 0 | 0.0000 | 0.1875 |
| `hook-turn-end-gate.py` | `outage_escalation` | `haiku` | `latency-sample.json:outage + ab-sample.json:outage_std` | 16 | 7.19 | 10.89 | 19.16 | 25.96 | 27 | 0 | 0.0000 | 0.1875 |
| `hook-plan-delivery-gate.py` | `approval_ask` | `haiku` | `approval-sample.json:approval + approval-sample.json:not_approval + approval2-sample.json:approval + approval2-sample.json:not_approval` | 64 | 5.88 | 12.77 | 17.29 | 19.14 | 30 | 0 | 0.0000 | 0.0469 |
| `hook-resolution-reminder.py` | `landing_discipline` | `haiku` | `landing-discipline-sample.json:pr_proposing + landing-discipline-sample.json:direct_push` | 16 | 3.88 | 4.96 | 6.37 | 15.38 | 22 | 0 | 0.0000 | 0.1875 |
| — | `acceptance_judge` | `haiku` | UNMEASURED — no latency sample exists | — | — | — | — | — | — | — | — | — |
| — | `question_materiality` | `haiku` | UNMEASURED — no latency sample exists | — | — | — | — | — | — | — | — | — |

`acceptance_judge` and `question_materiality` are listed because leaving them out
would be the quieter lie: the `MEASURED` table carries a row for each, and a reader
comparing the two would otherwise assume they were covered. Both run outside any
hook, so no harness timeout kills them and the last-resort ceiling applies. Neither
is sized by evidence.

### The zero rule

Every pool above observed **zero** calls at or above its ceiling. That is not a
residual of zero, and the checker treats a bare "0" with no bound as a failure.
With zero events in n trials the 95% upper bound on the rate is the rule of three,
3/n — so the honest statement is not "the hooks never fail open" but:

> On the evidence available, the per-call fail-open rate is **at most 17%**
> (deferring, 3/18), **19%** (outage, 3/16), **12%** (feedback, 3/26),
> **19%** (binary_ask, 3/16), **5%** (approval_ask, 3/64) and **19%**
> (landing_discipline, 3/16), each at 95% confidence.

These bounds are wide because the samples are small, and they shrink only with
more calls. Section 2 supplies the concrete reason not to dismiss them: a live
run landed above the sampled maximum on the very first attempt.

### The judge's own accuracy is a separate residual

A judge that answers in time can still answer wrongly, and a timeout budget does
nothing about that. In the verdict-labelled series, `ab-sample.json:outage_std`
records **1 wrong verdict in 6 labelled calls**. The remaining 10 calls in that
16-call pool come from `latency-sample.json`, which carries no `ok` field at all
— so the labelled-verdict error rate is 1/6, and any smaller figure quoted over
the full 16 assumes the 10 unlabelled calls were all correct, which these files
do not establish.

## 4. Alternatives considered

<!-- alternatives -->

| Alternative | Status | Evidence |
|---|---|---|
| Truncate the judge's context to a leaner prompt | **MEASURED AND REJECTED** | `ab-sample.json` / `lean-sample.json`: the lean prompt is not uniformly faster and has the worse tail (`defer_yes` max 71.96 s), and it costs accuracy — `defer_lean` gets 2 verdicts wrong of 8 against 0 of 8 for the standard prompt. Paying accuracy for a worse tail is the wrong trade for a gate. |
| Change the judge model | **NOT MEASURED** | The only cross-model file, `haiku-sample.json`, predates the prompt growth, so it measures a prompt that no longer exists. No honest comparison is available, and none is claimed. Sizing a ceiling on it would be a guess wearing a number. |

## 5. What this run does NOT check

<!-- not-checked -->

- **Latency.** Three calls are three calls. Section 2 shows they land both above
  the sampled max and below the sampled min, so no distribution is estimated here.
- **The harness's real invocation.** The hooks were driven by `subprocess` on
  stdin. That exercises the hook, not Claude Code's own hook dispatch — argv,
  cwd, environment and the timeout the harness itself imposes are all assumed.
- **Behaviour under budget exhaustion.** All three runs finished with budget to
  spare, so the drop-from-the-tail path and the `judges_skipped` stderr line were
  never exercised live; they are covered only by the unit tests.
- **Concurrency.** The runs were sequential, on an idle machine, deliberately —
  the same discipline the sampler took after `topup-sample.json` was contaminated
  by a parallel run. Contention is therefore untested, and it is precisely what
  the tail is most likely made of.
- **Verdict correctness in general.** Each run confirms one expected verdict on
  one payload. The residual error rate is in section 3, not closed by these runs.
- **The `acceptance_judge` and `question_materiality` paths**, which no hook
  invokes and no sample covers.

## 6. Reproducing this

```bash
cd /home/the0/cai-wt-judge-budget
python3 scripts/check-live-run-evidence.py live-run-evidence.md
```

The checker recomputes `n`, min, median, p90 and max of every row above from the
sample files it cites, compares each against the frozen contract in
`lib/judge_latency.py`, reads each ceiling out of the hook source with `ast`,
recounts the exceedances and their confidence bounds, and fails on a model tag
that is not `advisor._JUDGE_MODEL`, on `n` below 15, or on a zero claimed without
a bound. The first three live runs were driven by `/tmp/cc-scratch/live-run4/run.py`
with the payloads beside it; the `landing_discipline` run was driven the same way
in spirit — a `subprocess.run` of the hook on stdin with `CLAUDE_CONFIG_DIR`
pointed at a scratch agent home carrying only the `RESOLUTION`-node state the
hook needs — via a throwaway pytest test, since that machine's session confined
direct script execution to pytest-mediated invocation. Re-running any of these
costs real judge calls.
