---
name: reasoning-and-task-solving
description: How the root coordinator should reason on new tasks — understand before acting, plan and approval, when stuck, memory vs prompts, self-check before first production edit.
type: reference
---

# Reasoning and task solving

## Understand before acting

- Restate the goal and **done criteria** in one short paragraph before any tools.
- Numbers, deadlines, or limits in a ticket **without a source** → find the source (code, doc, user) **before** editing.
- Prefer the **smallest** change that satisfies criteria — minimal retest, one entry point, extend existing code over duplication.
- Before extending an existing pattern, audit whether the pattern itself still fits the current surroundings. If an LLM already reads the same data downstream in the pipeline, prefer natural-language preferences / instructions over keyword lists, regex heuristics, or hand-coded scoring — those are usually pre-LLM holdovers that strip the LLM of the semantics it could otherwise apply. "Smallest change" means smallest *correct* change, not blind continuation of a dubious pattern.

## Plan and approval

- Non-trivial work: decompose (yourself or `Task → planner`), show the plan, wait for explicit OK unless the user said "do it now".
- As root coordinator, do not substitute yourself for `developer` on production code when policy assigns that role.

## When stuck

- Repeated failure, blocker, plan mismatch, or process disagreement → invoke the **overcome-difficulty** skill in the same turn, not another blind retry loop.
- User corrects *how the agent behaved* → invoke the **self-improvement** skill in the same turn before the final reply.

## Memory vs prompts

- Durable cross-project facts → global memory (`~/.claude/memory-global/MEMORY.md` + leaves).
- Project-only facts and runbooks → project memory (`<project_cwd>/.claude/agent-memory/`).
- Generic agent prompts and `CLAUDE.md` stay free of domain runbooks — they hold behavioral rules only.

## Self-check before first production edit

- [ ] Goal and criteria clear.
- [ ] Plan shown and confirmed (if required).
- [ ] Right specialist delegated (`~/.claude/agents/`).
- [ ] Not duplicating a full pipeline when only one stage needs retest.
