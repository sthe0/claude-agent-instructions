---
name: a-dispatcher-that-tracks-a-units-status-but-not-its-artifact-restarts-a-repeat-dispatch-from-nothing
description: A dispatcher that records a unit's status but not where its artifact lives gives each repeat dispatch a fresh workspace, so finished work is re-derived from zero
type: reference
schema: regularity/v1
generality: 1
induced_from: [redispatch-continuity-check]
rule_derived: "lifted from redispatch-continuity-check.md (## Difficulty, observed 2026-08-28); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-dispatcher-that-tracks-a-units-status-but-not-its-artifact-restarts-a-repeat-dispatch-from-nothing

## Regularity

A dispatcher that records a unit of work's status but not where its artifact physically lives gives each repeat dispatch of an independent unit a fresh workspace that cannot see what the earlier dispatch finished, so completed work is re-derived from zero; nothing signals the waste until an effort-divergence count fires, several dispatches late. Observed in agentctl dispatch, whose worktree continuity covers only a stage that depends on a prior spawn stage.

## Generality

Level 1 — ranges over dispatchers of independent units of work that fork a fresh workspace per dispatch. It is deliberately not claimed for units that depend on a prior dispatch, for which continuity is recorded.

## Induced from

- [[redispatch-continuity-check]] — contributes the observed case behind the statement: four successive dispatches of one independent stage each forked a new worktree and re-derived a deliverable that was already finished and reviewed.

## Refutation

A repeat dispatch of an independent stage that reused a prior dispatch's finished worktree without being told of it (no depends_on, no continue-worktree flag), or a dispatcher that recorded artifact location for independent units and still re-derived finished work, would show the claim false.
