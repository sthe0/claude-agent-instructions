---
name: compose-query-cheaper-than-full-reissue
description: A plan's own cost estimate for "re-issue COMPOSE in full" can conflate a cheap read-only state query with an expensive full re-walk/re-spawn — verify the actual cost mechanism in code before accepting the plan's stated assumption.
type: reference
schema: leaf/v1
created: 2026-10-02
last_verified: 2026-10-02
---

# A plan's cost estimate for a remedy can conflate two different operations

## Difficulty

To achieve a cheap, correct remedy for a stale aggregate-state check, verify in code which concrete operation the plan's prose is actually naming — a plan author describing a remedy in natural language ("COMPOSE must be re-issued") can accidentally bundle two operations that differ in cost by orders of magnitude: a pure read-only query over state the engine already tracks, and a full re-walk that re-spawns every non-current unit. Accepting the plan's own stated cost ("a cost on the order of \$25 or more") at face value, instead of reading the code path the remedy actually calls, risks either paying for the expensive path unnecessarily or wrongly treating the cheap path as unavailable.

Concretely: `agentctl`'s `--review-topo` pair-based plan-review driver (`plan-review-topological.py`) has a `compose()` method that queries `agentctl plan-review-compose` — a pure read-only lookup against the engine's own already-recorded per-pair verdicts (populated incrementally by `record_argv` inside `settle()` on every real pair review). It spawns nothing and costs \$0. This is entirely distinct from the driver's full `--no-early-stop` walk (its step 5), which re-spawns every pair whose engine-recorded status is not current — the operation the plan's "RE-RUN RULE for defect 2" section had in mind when it estimated "~\$25+, re-spawns 37 pairs". A targeted `--pairs`-scoped rerun that flips one or more pairs' verdicts leaves the live-run log's last `COMPOSE: pass|blocked ...` line stale (it still names the pre-rerun blocked-set), and the plan's own text, written before this was checked, named only two remedies — "re-issue COMPOSE in full" (actually the costly walk) or "revise the check logic to be scoped-run-aware" — omitting the cheap third option.

## Guidance

Before accepting a plan's cost estimate for a stated remedy, read the code the remedy would actually invoke. If an aggregate consistency check (here: `verify_command`'s `cb == rev` assertion over a log-text `^COMPOSE: (pass|blocked)...` line) goes stale after a scoped rerun, the fix is often: query the engine's own live state directly (`python3 scripts/agentctl-cli.py plan-review-compose --session <sid> --target <plan>`), then append one line to the log matching `compose()`'s own emit format exactly — `"COMPOSE: blocked " + ",".join(failing-map keys)` (no annotation suffixes, comma-joined with no spaces) or `"COMPOSE: pass"` when `ok` is true. Verify the engine's returned `failing` set is set-equal to an independently recomputed "latest-verdict-is-revise" set from the raw logs before trusting it (confirms no bookkeeping defect, not just staleness) — if they diverge, that is a different, code-level difficulty, not this one.

> verified by: direct invocation of `plan-review-compose` against `review-prompt-fit-fixture.toml` returned a 38-pair `failing` set that was byte-for-byte set-equal to the independently recomputed `rev` set from `review-prompt-fit-single-pair.log` + `review-prompt-fit-live-run.log`, confirmed 2026-10-02.

## See also

- [[plan-cost-tier-empirical-stage-underestimate]] — a related but distinct cost-estimation gap: tiering a measurement/judge-gated stage as the plan author, at authoring time, rather than reading the remedy's own actual cost mechanism after the fact.
- [[verify-right-axis-report-honestly]] — the general discipline of checking the axis actually load-bearing for a claim rather than the one most readily asserted.
