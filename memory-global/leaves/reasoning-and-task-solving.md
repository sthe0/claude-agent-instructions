---
name: reasoning-and-task-solving
description: How the root coordinator should reason on new tasks — understand before acting, plan and approval, when stuck, memory vs prompts, self-check before first production edit.
type: reference
created: 2026-05-22
last_verified: 2026-05-24
---

# Reasoning and task solving

## Understand before acting

- Restate the goal and **done criteria** in one short paragraph before any tools.
- Numbers, deadlines, or limits in a ticket **without a source** → find the source (code, doc, user) **before** editing.
- When producing content for a **user-facing artifact** (config, preferences, agent prompt, README, ticket field they will read) — include only what the user stated or what the surrounding code / spec strictly requires. Do not extrapolate "reasonable defaults" they didn't ask for: extra channels, "especially interesting" categories, padded keyword lists, helpful-sounding examples. If a value is missing — leave it empty, ask, or pick the minimum and surface the gap explicitly. Fabricated content is harder to spot later than a missing one.
- Prefer the **smallest** change that satisfies criteria — minimal retest, one entry point, extend existing code over duplication.
- Do not silently "optimize" a parameter the user explicitly **deprioritized**. If the user has stated a priority ("cost of X is negligible", "don't bother with Y"), acting against it as an unstated optimization is a scope violation, not a helpful default — and it can cost more than it saves. Parameters that affect **correctness, accuracy, or reproducibility** (sample counts, judge/repeat runs, thresholds, seeds) are **decisions to surface or ask about**, not constants to bury in a launcher; flag the knob and its consequence rather than choosing quietly.
- Before extending an existing pattern, audit whether the pattern itself still fits the current surroundings. If an LLM already reads the same data downstream in the pipeline, prefer natural-language preferences / instructions over keyword lists, regex heuristics, or hand-coded scoring — those are usually pre-LLM holdovers that strip the LLM of the semantics it could otherwise apply. "Smallest change" means smallest *correct* change, not blind continuation of a dubious pattern.

## Plan and approval

- Non-trivial work: decompose (yourself or `Task → planner`), show the plan, wait for explicit OK unless the user said "do it now".
- As root coordinator, do not substitute yourself for `developer` on production code when policy assigns that role.

## When stuck

- Repeated failure, blocker, plan mismatch, or process disagreement → invoke the **overcome-difficulty** skill in the same turn, not another blind retry loop.
- User corrects *how the agent behaved* → invoke the **self-improvement** skill in the same turn before the final reply.

## Memory vs prompts

- Durable cross-project facts → global memory (`~/.claude-agent/memory-global/MEMORY.md` + leaves).
- Project-only facts and runbooks → project memory (`<project_cwd>/.claude/agent-memory/`).
- Generic agent prompts and `CLAUDE.md` stay free of domain runbooks — they hold behavioral rules only.

## Self-check before first production edit

- [ ] Goal and criteria clear.
- [ ] Plan shown and confirmed (if required).
- [ ] Right specialist delegated (`~/.claude-agent/agents/`).
- [ ] Not duplicating a full pipeline when only one stage needs retest.
