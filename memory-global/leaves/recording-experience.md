---
name: recording-experience
description: The discipline for recording a resolved-task experience leaf — the quality bar (decide before writing), what to record (search-before, scope, schema, ticket-thin, required frontmatter, self-critique), and the auto-trigger of self-improvement from the self-critique. The resolution gate itself stays in CLAUDE.md § On task resolution; this leaf is the execution-time how-to.
type: reference
schema: leaf/v1
created: 2026-06-25
last_verified: 2026-06-27
---

# Recording a resolved-task experience leaf

## Difficulty

At task resolution you must decide *whether* and *how* to record the experience. Recording everything bloats the always-loaded memory surface (worse than a gap); recording nothing loses reusable difficulties. Both failure modes cost future tasks. This procedure is consulted **only at the moment of recording** — after the resolution gate in `CLAUDE.md` § On task resolution has passed — so it lives next to its live tool (`scripts/record-experience.py`) rather than in the always-loaded prompt.

## Guidance

### Quality bar (decide before writing)

Record only if a future you, opening a similar task, would actually want to **read** this leaf first. Concrete tests — at least one must be a clear "yes":

- Was there a non-obvious choice that would not be visible from the code / commit log alone?
- Was a difficulty encountered and overcome in a way that is reusable?
- Did the task reveal a missing tool, missing memory, or missing instruction?
- Would skipping this leaf cost a future similar task at least `rediscovery-threshold-min` minutes of rediscovery (see `~/.claude-agent/config.md`)?

If none — do not record. Memory bloat is worse than memory gap. The git log + the code are the default record. Skip the leaf entirely for trivial Q&A turns and one-line tasks; the whole rule applies only to substantive work where you planned, delegated, or hit a difficulty.

### What to record

The unit of experience is a **recurring difficulty** (a plan-vs-reality divergence — the object `overcome-difficulty` localizes), not a one-off task. One leaf records one difficulty and accumulates every context it arose in, plus the plan that removed it in each.

- **Search before recording (mandatory; engine-gated).** `scripts/record-experience.py search "<keywords>"` ranks existing leaves by `description` + `## Difficulty`. If an analogous leaf exists, **extend** it (`record-experience.py extend …`) rather than duplicate — accumulated contexts of one difficulty expose recurring patterns and justify a general solution; else create a new leaf (`record-experience.py new …`). This discipline is also **enforced at write time**: `new` refuses to fragment an analogous leaf without `--justify-new "<reason>"`, so the search step cannot be bypassed by omission.
- **Child difficulties: extract, never inline.** A side/child difficulty met while resolving the parent is recorded as its own leaf (or `extend` an analogous one) and linked inline `[[slug]]` from the parent's `## Contexts` at the point it arose — never described in prose inside `## Contexts` / `## Cost`, where `record-experience.py search` (it ranks only `description` + `## Difficulty`) cannot find it. `verify-experience-leaf.py` blocks a leaf that names side/child difficulties without an inline `[[slug]]` link (advisory in the full-corpus scan, blocking at write/commit time).
- **Scope.** Cross-project → `~/.claude-agent/memory-global/leaves/experience/`. Project-specific → `<project_cwd>/.claude/agent-memory/experience/`.
- **Schema and tooling.** Leaves follow `schema: difficulty/v1` (sections **Difficulty / Order & criterion / Contexts / Cost**, free-form `refs:` into the difficulty graph — cycles allowed, the framework is self-referential) — the **generality-0 profile** of one difficulty-record model whose generality≥1 profile is the `principle/v1` principle leaf ([principle-leaf-schema.md](principle-leaf-schema.md)). Full schema + search / extend / new / ticket flow: [experience-leaf-schema.md](experience-leaf-schema.md). Generate via `scripts/record-experience.py` (auto-updates the `experience/MEMORY.md` sub-index); `verify-experience-leaf.py` enforces the shape. For standalone leaves, the **`## Cost` section must not contain an unreplaced TODO** — fill it from the figure surfaced by `agentctl resolve` (the plan/task `CostRollup`); `verify-experience-leaf.py` rejects the generated placeholder.
- **Ticket-driven work → thin leaf.** When the task is a ticket, the full structured record lives **in the ticket** (the `tracker-management` skill posts it via `record-experience.py ticket`); the leaf is a thin pointer (`ticket:` frontmatter + one-line reusable hook). Single source of truth — no duplication.
- **Required frontmatter `resolution_confirmed_by_user: "<quote>"`** — enforced by `verify-experience-leaf.py` (PreToolUse hook + `verify-all.py`). Writing on assumed resolution is a recurring failure mode; the check makes "confirm → record" mechanical.
- **At resolution (or periodically) → `promote-scan` for principle induction.** `record-experience.py promote-scan` surfaces difficulties whose accumulated recurrence (Σ `### ` context blocks across all leaves sharing the same functional ground) has reached `principle-promotion-threshold` (config.md; default 3). A flagged cluster is a candidate to lift into a `principle/v1` leaf; a cluster spanning ≥2 distinct leaves also signals fragmentation worth merging via `extend`. Run at resolution or whenever you notice repeated friction in the same area.
- **Self-critique feeds self-improvement.** Agent-system friction is itself a difficulty about the agent system — record/extend its leaf (context = this task) and invoke `self-improvement` the same turn (§ Auto-trigger below). For friction recurring across ≥2 leaves, run `Skill(overcome-difficulty)` against the agent-system-as-plan first — the replanning task is an architectural improvement, not a rule tweak. Full discipline: [systemic-pattern-scan.md](systemic-pattern-scan.md).

### Auto-trigger self-improvement from the self-critique

If the **self-critique** names concrete agent-system friction, **invoke `self-improvement` the same turn** (after writing the leaf, before the final reply) — treat the self-critique as if the user said "that was annoying because X, fix it". This turns experience into actual instruction changes instead of dead text. **For systemic patterns** (friction recurring across leaves), invoke `overcome-difficulty` against the agent-system-as-plan first; its replanning task is the architectural proposal `self-improvement` then writes — see [systemic-pattern-scan.md](systemic-pattern-scan.md).

## See also

- `CLAUDE.md` § On task resolution — the resolution gate (confirm before close, recap + `AskUserQuestion`, gratitude ≠ confirmation, Outcome format) that precedes this recording step.
- [experience-leaf-schema.md](experience-leaf-schema.md) — the `difficulty/v1` schema and `record-experience.py` tooling.
- [systemic-pattern-scan.md](systemic-pattern-scan.md) — scanning experience for recurring friction and routing it through `overcome-difficulty` → `self-improvement`.
