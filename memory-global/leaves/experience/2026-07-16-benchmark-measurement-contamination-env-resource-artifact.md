---
name: 2026-07-16-benchmark-measurement-contamination-env-resource-artifact
description: A benchmark A/B comparison can be corrupted not by the systems under test but by artifacts of the measurement environment, which silently bias the result rather than crash. Two modes hit one pilot (agent-bench SWE-bench, layer vs bare): (1) USAGE/RATE-LIMIT TRUNCATION — a run cut off mid-solve by a transient auth-401/OAuth-refresh race, rate limit, or network drop is not a genuine task failure; counting it as unsolved understates whichever arm it hit more, and on n=50 this manufactured a fake +8pp effect that collapsed to +2pp once only the env-artifact cells were re-run on the same instances. (2) RESOURCE-SHORTAGE GRADING CORRUPTION — the swebench grader builds a ~2-3GB docker image per instance; a 99%-full disk made 94/120 image builds error, and error_instances DEFAULT to unresolved, so a silent undercount reported pass@3 ~20% against a known ~75% baseline (a plausible number, no crash).
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com"
refs: [/home/the0/agent-bench/report/report-opus-n120.md, /home/the0/agent-bench/report/grade_all.py]
created: 2026-07-16
last_verified: 2026-07-20
---

# Environment/resource artifacts silently contaminate a benchmark measurement

## Difficulty
A/B benchmark numbers were wrong for reasons unrelated to the systems under test: environment truncation (rate/usage limit, auth race, network) and a resource shortage during grading (disk-full -> per-instance docker build errors that default to 'unresolved'). Both fail SILENTLY into plausible-looking numbers, not crashes, so a structurally-green run reports a fabricated effect.

## Order & criterion
For every cell: classify a non-zero exit as env-artifact (re-run) vs genuine task failure (keep as honest unsolved) — do NOT let an env-truncated cell count as a failure. Before grading: pre-flight free-disk check sized to (n_instances * per-image GB). After grading: sanity-gate on error_instances ~ 0 and completed ~ total PER GROUP before trusting any headline; error_instances defaulting to unresolved is an undercount, not an error you'll see. Re-grade into a FRESH report-dir (the grader skips groups already present).

**Acceptance check:** measurable: 0 env-artifact truncations remain in the row set (re-run to convergence); post-grade every group shows completed≈total and error_instances≈0 (residual errors symmetric across arms only); the de-contaminated delta is stable across a re-run of the affected cells (fake effects move, real effects don't).

## Contexts

### 2026-07-16 — SWE-bench layer-vs-bare pilot
- Where it arose: agent-bench SWE-bench Verified ablation (claude-agent-instructions coordination layer vs bare Claude Code), opus-4.8, n=120x3 trials, flat/subscription mode. Generation needs 3 env vars mirrored from the working caller (OAuth creds, docker network=host for NAT64/DNS64, NODE_EXTRA_CA_CERTS corporate MITM CA); grading is local (no external LLM) but builds one docker image per instance so it is disk-bound.
- Working plan: 1. Distinguish env-artifact truncation from genuine failure per cell (auth-401/rate/network = re-run; task-merit failure = keep). 2. Re-run only the env-artifact cells; verify the delta is stable (a delta that collapses on re-run was contamination). 3. Pre-flight disk check before grading (n_instances * ~2-3GB). 4. Post-grade sanity gate: error_instances~0 & completed~total per group; treat error_instances as silent undercount. 5. Re-grade into a fresh report-dir. 6. Archive trajectories+prediction.patch before any /tmp cleanup (grader reads prediction_patch as a FILE PATH, not inline content).

## Cost
Generation was flat/subscription mode (real money ≈ $0; imputed list-price ≈ $0.92–1.10/run over 720 runs). The dominant cost was **wall-clock and rediscovery**, not dollars: each contamination mode cost a full re-run cycle plus root-cause investigation — the disk-full grade produced a plausible fabricated ~20% pass@3 that took an overcome-difficulty pass to unmask against the known ~75% baseline; the rate-limit contamination cost re-running every env-artifact cell across n=50 then n=120. Standing gates (pre-flight disk + post-grade error_instances sanity) save ≥ a full grade-cycle wall-clock plus the investigation that here only a lucky baseline made possible.

## Self-critique of the agent system
The disk-full corruption was only caught because the grade contradicted a known baseline; without that reference point the fabricated ~20% pass@3 would have been published. A pre-flight/post-grade gate must exist so detection does not depend on happening to have a baseline to contradict — this is the mechanization candidate routed to self-improvement.

**Update 2026-07-20 — MECHANIZATION LANDED** (task `agent-bench-grade-integrity-gates`, agent-bench commit `9de0969`): both gates now live in `report/grade_all.py` — `check_disk_headroom()` (pre-flight, `DEFAULT_MIN_FREE_GB=50` floor on docker's storage fs, local-harness path only, skipped under sb-cli/`--smoke`) and `assert_group_trustworthy()` (post-grade, refuses a group whose `error_instances > max(2, ceil(0.02*submitted))`, escape `--allow-degraded`, keyed on `error_instances` only — the disk-full undercount signal — and no-op when `submitted` is falsy). 13 mutation-proving tests. So disk-full grading corruption now fails LOUD at start or at record-time instead of silently publishing a fabricated headline; detection no longer depends on a lucky baseline to contradict.
