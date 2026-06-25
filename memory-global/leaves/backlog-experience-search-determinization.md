---
name: backlog-experience-search-determinization
description: Backlog item — determinize the mandatory "search before record" step for experience leaves into an agentctl guided flow (extend-vs-new). Deferred from the 2026-06-25 modular-state-machine task.
type: project
---

# Backlog: determinize experience-leaf `search → extend-vs-new` flow

**Difficulty it removes:** `record-experience.py search` before writing a leaf is *mandatory* per CLAUDE.md (§ What to record), but enforced only by prose — so it gets skipped, producing duplicate leaves instead of extended ones.

**Proposed shape:** a guided `agentctl record-experience` (or equivalent) flow that forces the search step and surfaces ranked existing leaves, then routes to `extend` vs `new` as a checkable predicate — rather than leaving the decision to prose discipline at resolution time.

**Status:** deferred to its own task/ticket. Raised 2026-06-25 alongside the modular-state-machine work (making `agentctl` pluggable so skills/tools/specializations attach their own sub-state-machines; tracker-management is the first consumer). Do **not** fold into that task — separate plan.

**Why deferred:** the modular-engine task is the architectural prerequisite; this experience-flow is one more consumer of the same plug-in mechanism and is cleaner to design once that exists.
