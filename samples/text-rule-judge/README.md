# Text-rule judge calibration

Measures the model judge behind the published-text writer gate
(`advisor.judge_published_text_rules`, called from `scripts/hook-published-text-writer-gate.py`):
a bound TEXT body that trips the lexical prefilter (`lib/writer_rules.find_candidates`) is
judged against the tech-writer rules it nominated, and a genuine YES denies the publication.
A judge that can block a publication is not delivered until it has been run live on labelled
cases, including the incidents that motivated it.

## Files

| file | role |
|---|---|
| `labelled.jsonl` | 17 org-neutral items `{id, label, label_rules, body}`: 9 violations (both historical incidents, a calque, filler, speculative water, a local path, backticks in link text, two more second-person addresses) and 8 clean items that still trip the prefilter (a quotation with «вы», a UI label, a reply to an `@login` and to a named person, a code identifier equal to a calque term, an illustrative path in a code block, a quoted English error, a generic "you") |
| `calibrate.py` | `judge`: every item judged `--runs` times through `advisor.subprocess_runner`, plus the prefilter trigger rate over historical publications (counts only, no body text is written); `hook-e2e`: the real hook script fed a PreToolUse payload for a violating and a clean body bound by a fixture transcript |
| `check_calibration.py` | every acceptance assertion; recomputes from the per-run records and rejects a run whose `verdict` / `genuine` is not a JSON bool |
| `../../scripts/tests/test_text_rule_judge_fixtures.py` | hermetic: schema, prefilter recall 1.0 on every item, and that the checker goes red on a bad calibration |

## Re-run

```
python3 samples/text-rule-judge/calibrate.py judge --out <dir>/calibration.json
python3 samples/text-rule-judge/calibrate.py hook-e2e --out <dir>/hook-e2e.json
python3 samples/text-rule-judge/check_calibration.py <dir>/calibration.json <dir>/hook-e2e.json
```

Re-run after any change to the judge prompt, `_TEXT_RULES_JUDGE_MODEL`, a rule's wording in
`tech-writer/SKILL.md`, or a `judge` entry in `publish-rules.toml`. The live calls are real
model calls (about 2 per item); nothing is published and the hook's command is never executed.

## What the check asserts

- n >= 16 items, each judged twice, every run a JSON-bool `verdict` and `genuine`.
- Both incident items: every run is a genuine YES naming `say-13` with a quoted span present in the body.
- Zero false denies: no clean item has a run with `verdict` and `genuine` both true.
- At most one item whose two runs disagree (more than that means one pass is not evidence).
- Maximum latency over all attempts, retries included, is at most 184 s, one second under the 185 s
  family ceiling that every judge timeout is pinned to.
- The hook end-to-end run denies the violating body naming `say-13` and allows the clean one.

## Limits

- 17 items is a thin sample: it shows the judge separates these cases, not that its false-deny
  rate is below any given figure. The false-deny rate on a wider population is an acceptance-review
  question for the person landing the change.
- The latency row for this judge stays UNMEASURED in `lib/judge_latency.py`: the table describes the
  haiku family and this judge runs on a higher tier (the `acceptance_judge` precedent). The sonnet
  figures live in `calibration.json` and the published-text-writer-gate leaf only.
- The prefilter trigger rate comes from historical publications whose body could still be resolved;
  bodies that were files since deleted are counted as `unresolved`.
