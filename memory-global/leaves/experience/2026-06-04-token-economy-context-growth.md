---
name: 2026-06-04-token-economy-context-growth
description: Quota-cost analysis across all mounts reframed the driver (accumulated context, not static prefix) and shipped Sonnet-spawn routing, autocompact/bash-output env, 200k default, and a Cost-discipline CLAUDE.md section. Lessons on verifying web-research claims and the recurring CLAUDE.md cap.
type: reference
resolution_confirmed_by_user: "Да, решено (Recommended)"
plan_file: none (in-thread substantive, AskUserQuestion-scoped)
---

# 2026-06-04 — token-economy: accumulated context is the cost

User asked to analyse quota spend across the current mounts, find expensive spots, and reduce them — explicitly inviting analysis of the gena skill suite and fresh web research.

## Final plan as executed

No plan file. Recon → two parallel background research agents (gena practices via Explore; 2026 web best-practices via general-purpose) → local measurement script over all transcripts → synthesis → `AskUserQuestion` for scope (model window + which measure-bundles) → implement → verify → commit → push. The user picked opus(200k) default and all three bundles.

Shipped: `spawn-specialist.py` `MODEL_BY_KIND` (Sonnet for developer/thinker/tech-writer/yandex-cloud-expert; planner inherits) + `--model`; `settings/base.json` env `BASH_MAX_OUTPUT_LENGTH=30000` and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70`; machine-local model `opus[1m]`→`opus`; CLAUDE.md `## Cost discipline`; full rewrite of `token-economy-plan.md` (§ Update 2026-06-04, items re-statused, rows 12-20). Commit `ef028d5`.

## Difficulties

1. **Web-research agent returned confident but wrong / unverified claims.** It asserted `MAX_THINKING_TOKENS` was removed on Opus 4.8 (the official costs doc explicitly still documents it) and surfaced env names (`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `BASH_MAX_OUTPUT_LENGTH`) from community guides. Resolved by a verification pass against `code.claude.com/docs/en/{costs,settings,env-vars}` before touching settings: confirmed the two env vars, dropped `DISABLE_NON_ESSENTIAL_MODEL_CALLS` (unconfirmed name + <$0.04/session), kept effort behavioral. **The agent's own adversarial flags were accurate — heed them.**
2. **The measurement contradicted the prior plan.** The 2026-05-27 plan optimized the static prefix; the sweep showed the prefix is ~38k (5-10%) and 71% of cache_read cost is session turns beyond #200. Had to reframe, not extend.
3. **CLAUDE.md 400-line cap collision (again).** Adding the Cost-discipline section overflowed to 403; reclaimed by compressing the git + instruction-language sections to pointers. Same friction as the tech-writer task earlier the same day.

## Artifacts

- Commit `ef028d5` (pushed). Reusable aggregator `/tmp/cc-scratch/quota_analyze.py` (per-project tokens/cost, re-reads, large tool-results).
- `token-economy-plan.md` is the living programme — future token work updates it, not new leaves.

## Lessons

- **Verify web-agent claims against primary docs before any settings/env change** — version-specific knobs are exactly where general research goes stale. One targeted `WebFetch` of the official env-vars/costs page settled four uncertain claims cheaply.
- **Measure before optimizing.** A 30-line transcript aggregator overturned the prior plan's central assumption. `cache_read` per turn climbing to 870k in a single session is the signature of the real problem (long sessions on the 1M window), invisible to prefix-size reasoning.
- **The biggest token lever is behavioral, not config:** delegate verbose exploration to cheap sub-agents and keep sessions task-scoped. Config (200k default, earlier autocompact, bash-output cap) caps the tail; behavior prevents the growth.
- Reusable recon shape: parallel background research agents + inline scripted measurement, synthesized by the coordinator. Cheap and fast.

## Self-critique of the agent system

The **CLAUDE.md 400-line cap forced an ad-hoc reclaim for the second time today** (also the tech-writer task). Each time it was resolved cleanly by compressing pointer-heavy prose, and the file got tighter — so the cap is doing its job, not failing. This is mild, self-correcting friction, **not** yet a systemic defect needing new machinery (per `policy.md` § What NOT to encode — no speculative architecture). If a *third* addition cannot reclaim without losing substance, that is the signal to extract a CLAUDE.md section into an imported leaf — note it then, not now. Also: `offload-large.sh` is now partly redundant with the native `BASH_MAX_OUTPUT_LENGTH`; flagged in the plan leaf, no action taken.

## Cost, effort, and tool usage

- One in-thread session. Two background sub-agents (Explore gena ~84k tok / general-purpose web ~94k tok). No `claude -p` spawns. Wall-clock ~25 min; ~3 user interactions (scope question + push/resolution gate).
- Tools: `Agent` ×2 (research fan-out), `WebFetch` ×3 (doc verification), `Bash` (measurement + settings), `Edit`/`Write` (5 files), `AskUserQuestion` ×2, `TaskCreate/Update` (6 tasks).
- Cost driver of the task itself: the WebFetch verification pass — cheap and decisive, prevented committing two unverified env vars.
