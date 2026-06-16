---
name: 2026-06-04-verify-load-bearing-axis
description: Recurring difficulty — a surface signal (daemon status, a confident research/web claim, a green static check, a prior plan's assumption) is trusted as evidence of the load-bearing fact, while the axis that actually matters tells a different story. Fix is to observe that axis directly before concluding.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "<migrated 2026-06-11 to difficulty/v1; per-context confirmations preserved inline>"
refs: [2026-05-26-agent-system-plan-vs-reality-drift]
---

# Verify the load-bearing axis, not the surface signal

## Difficulty
A proxy signal reads "fine" and is taken as proof of the underlying fact: a daemon is `active` and `getMe` returns 200, a research agent states a version-specific claim confidently, a static check (imports/tests/build-diff) is green, or a prior plan's central assumption is reused — while the *load-bearing* axis (background loops alive, primary-doc-confirmed, runtime behavior, measured data) is never checked and turns out to disagree.

## Order & criterion
Before acting or concluding, observe the axis that actually carries the conclusion — not the proxy. **Acceptance check:** the conclusion rests on a direct observation of the load-bearing fact (disk + log grep, a primary-doc `WebFetch`, a measurement over real data, a runtime test of the affected path), and a transient proxy blip is retried before being read as a state.

## Contexts

### 2026-06-04 — confident-but-wrong research; assumption overturned by measurement
Confirmed: "Да, решено". A web-research sub-agent asserted `MAX_THINKING_TOKENS` was removed on Opus 4.8 (the official costs doc still documents it) and surfaced env names from community guides. Resolved by a verification pass against the official `code.claude.com/docs` pages before touching settings — confirmed two env vars, dropped an unconfirmed one. Separately, a 30-line transcript aggregator overturned the prior plan's central assumption: the static prefix is ~5–10% of cost; ~71% of `cache_read` is session turns beyond #200 → had to *reframe* (long sessions on the 1M window), not extend. The research agent's own adversarial flags were accurate — heed them. Commit `ef028d5`.

### 2026-06-05 — healthy-looking daemon, dead background loops
Confirmed: "Да, пришёл — закрываем". ccgram "связь отвалилась" with `systemctl status` = `active` (1wk+ uptime) and `getMe` = 200, yet the bridge was degraded: `OSError: [Errno 28] No space left on device` had crashed the background poll loops, which do not self-resurrect (only a restart revives them). The health checks pointed away from the symptom the user felt. Fix: grep the log for ENOSPC + `df` *before* trusting `status`/`doctor`; a single `getMe` timeout that 200s on retry is a transient IPv6 egress blip, not an outage. Built a systemd-user watchdog. Diagnostic-order lesson now in the ccgram-management skill.


### 2026-06-16 — DEEPAGENT-433: 'default' change whose runtime effect must be located, not assumed
- Where it arose: robot/deepagent eval/convert/metrics — flipping Qwen3-14B→Qwen3-3A-30B defaults; verifying the change actually takes effect
- Working plan: Three surface-vs-load-bearing splits, each resolved by observing the real axis: (1) **enum default ≠ runtime effect** — the vh3.Enum model_architecture default (operations.py) looked like 'the convert default', but every prod call-site passes model_architecture explicitly from the registry (functions.py:455-473 options=model_conversion_kwargs); grepping call-sites showed only the metrics tokenizer_type default is actually runtime-effective (compute_metrics/render call-sites pass nothing). Verify call-sites before claiming a default change matters. (2) **green WI ≠ tokenizer loaded** — compute_metrics loads the tokenizer inside try/except (a failure only warns), so a success WI does not prove the new 30B tokenizer loaded; the load-bearing observable is the block stderr line 'Load tokenizer: Qwen/Qwen3-30B-A3B-Instruct-2507' + 'Updated row with auxiliary metrics', read via getBlockLogs→stderr.log (NOT the ui-api-proxy path, which 404s on the public blockGuid). (3) **static build ≠ offline runtime** — files are Docker-packaged (no ya-make test target), and the image preloads tokenizers under HF_HUB_OFFLINE=1; a default tokenizer string that is NOT in the Dockerfile preload list would fail at runtime offline. Validated by an in-graph image build (--images <commit>:arc_ref=<commit>) from the Dockerfile-change commit + offline run_eval whose compute_metrics loaded -Instruct-2507 from the baked cache.
## Common core & variations
**Common:** trust a *direct observation of the axis that carries the conclusion*, not a proxy that merely correlates with it. Mirrors [[2026-05-26-agent-system-plan-vs-reality-drift]]'s static-vs-runtime verification trap.

**Variations:**
- *External claim* → verify against the primary source (official docs), cheaply and decisively (one targeted `WebFetch`).
- *Health proxy* → probe the actual subsystem (background loops, disk), not the supervisor's liveness flag; retry transient blips before concluding outage.
- *Inherited assumption* → measure before optimizing; a small aggregator over real data can overturn a whole prior plan.

## Cost
- Both in-thread. 2026-06-04 used two background research sub-agents (Explore ~84k tok / general-purpose web ~94k tok) + 3 `WebFetch` doc-verification calls; the verification pass was the decisive cost driver and prevented committing two unverified env vars. ~25 min wall-clock each.
- 2026-06-05 ≈ 25 min; cost concentrated in the dateless-log time-windowing (no journal capture → `tail` + `awk` epoch conversion, `mawk` not `gawk`).
