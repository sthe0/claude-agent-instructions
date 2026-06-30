---
name: experience-leaf-schema
description: The difficulty-centric schema for experience leaves (schema:difficulty/v1) — sections, the difficulty graph (cycles allowed), search-before-record + multi-context merge, ticket-thin-leaf rule, and what verify-experience-leaf.py enforces.
type: reference
created: 2026-06-11
last_verified: 2026-06-29
---

# Experience leaf schema (`difficulty/v1`)

The atomic unit of recorded experience is a **recurring difficulty**, not a one-off task. A difficulty is a divergence between the plan and reality (the same object `overcome-difficulty` localizes). One leaf records one difficulty and accumulates every **context** in which that difficulty arose, with the plan that removed it in each context. As contexts accumulate, the leaf exposes what is *common* across resolutions and what *varies* — which is the basis for a general solution.

This is the single source of truth for the schema. `CLAUDE.md` § On task resolution points here; `scripts/record-experience.py` generates leaves to this shape; `scripts/verify-experience-leaf.py` enforces it. **Ordinary (non-experience) leaves** use the lighter `leaf/v1` shape instead — see [leaf-schema.md](leaf-schema.md).

**One difficulty-record model, two profiles.** `difficulty/v1` is the **generality-0 profile** of a single difficulty-record model: a recorded difficulty *is* a principle at generality level 0 — "worked once, here." When a difficulty recurs across contexts and its commonality is lifted into a rule a *different* task can consume, it graduates **up** to the **generality≥1 profile** — `schema: principle/v1`, stored under `principles/` (schema: [principle-leaf-schema.md](principle-leaf-schema.md)). Same model, two physical profiles distinguished by the `generality` field; the section sets differ because each profile answers a different question (this profile records *what worked in which context*, the principle profile records *the refutable rule it generalizes to*). The `leaf/v1` ordinary-leaf shape above is **separate and unrelated** — it is not a point on this continuum.

## The difficulty graph (cycles allowed)

Leaves link to each other via the `refs:` frontmatter list and inline `[[slug]]` references. The links form a **graph, not a DAG** — cycles are expected, because the framework is self-referential:

```
order → plan → implementation → difficulty → induced order → plan → …
```

A child difficulty that arose while resolving a parent is referenced, never inlined: record it as its own leaf (or extend an existing one) and link it from the parent's `## Contexts` at the point it arose. `verify-experience-leaf.py` enforces this: a standalone leaf that names side/child difficulties in prose without an inline `[[slug]]` link is rejected (advisory in the full-corpus scan, blocking at write/commit time). No tooling assumes acyclicity.

## Record flow — search before write

1. **Search first (mandatory).** `scripts/record-experience.py search "<keywords>"` ranks existing experience leaves by `description` + `## Difficulty` body. This is how recurring difficulties get merged instead of duplicated.
2. **If an analogous leaf exists → extend it.** `record-experience.py extend --leaf <path> --context "…" --plan "…"` appends a new `### context` and, once a leaf holds ≥2 contexts, prompts you to fill `## Common core & variations` — the distillation that turns scattered records into a general solution.
3. **Else → create a new leaf.** `record-experience.py new …` writes a schema-correct leaf, auto-dates the filename (`YYYY-MM-DD-<slug>.md`), and inserts the `experience/MEMORY.md` pointer line. If the difficulty you supply is similar to an existing leaf's ground (overlap ≥ JOIN_RATIO), `new` refuses with a pointer to `extend` — pass `--justify-new "<reason>"` only for a genuinely distinct difficulty that shares vocabulary.
4. **Ticket-driven work → thin leaf.** `record-experience.py ticket --ticket <KEY> …` writes a thin pointer leaf and emits the full structured body to stdout for the `tracker-management` skill to post on the ticket (see § Ticket-driven work).
5. **Periodically → run `promote-scan`.** `record-experience.py promote-scan` clusters the corpus by functional ground (reusing the same similarity measure as steps 1–3), sums the `### ` context blocks across each cluster's leaves (occurrences), and prints every cluster. Any cluster whose total occurrences ≥ `principle-promotion-threshold` (config key; default 3, the Rule-of-Three) is flagged as a principle-induction candidate — its member leaves should be distilled into a `principle/v1` leaf with `induced_from` pointing back. When a cluster has ≥2 distinct member leaves, `promote-scan` additionally reports fragmentation — those leaves cover the same difficulty and should be merged via `extend`. Flag-only: `promote-scan` never writes a principle.

## Standalone leaf shape

```
---
name: <slug>                              # kebab; for new leaves the file is YYYY-MM-DD-<slug>.md
description: <one-line hook = the recurring difficulty>
type: reference
schema: difficulty/v1
generality: 0                             # optional; implied 0 when absent (this is the generality-0 profile)
tier: 1                                   # optional; implied 0 when absent — difficulty tier (ADR-0002), emit only for tier-1
resolution_confirmed_by_user: "<user's literal confirmation quote>"
created: <YYYY-MM-DD>                      # required — date first recorded (record-experience stamps it)
last_verified: <YYYY-MM-DD>               # required — date content last confirmed true; >= created
refs: [<slug § stage>, …]                 # optional; free-form links into the difficulty graph (cycles OK)
plan_file: <abs path>                     # optional; the as-executed planner plan, if one exists
created: YYYY-MM-DD
last_verified: YYYY-MM-DD
---

# <difficulty title>

## Difficulty
The invariant essence of the divergence (reality vs plan) — what kept going wrong, stated once, independent of any single occurrence.

## Order & criterion
The order for the result/resource that removes the difficulty, **plus the acceptance check** — how you know the order was met. As the difficulty recurs this is refined toward the general form.

## Contexts
One `###` subsection per occurrence. Each: where/when it arose (+ `[[ref]]` if it surfaced inside another difficulty) and the **plan that worked there**. A first occurrence is a single context with no synthesis yet — i.e. the simple four-part record. Do not inline a child difficulty's resolution; reference its leaf.

### <YYYY-MM-DD — short context label>
- Where it arose: …
- Working plan: …

## Common core & variations
Appears once the leaf has **≥2 contexts**. The shared solution (the general answer) vs what differed per context. This section is the payoff of merging.

## Cost
Per occurrence: `$` on `claude -p` spawns + wall-clock + user-intervention count (`scripts/cost-report.py --since <task-start>`), and the specialization/skill usage table (`scripts/tool-usage-report.py --since <task-start>`). Kept because cost analytics are still wanted across occurrences.

## Self-critique of the agent system    (optional)
Agent-system friction observed while resolving this task — missing affordance, stale guidance, wrong default; name the file/section/behavior. This is itself a difficulty **about the agent system**: record or extend a separate difficulty leaf for it (context = this task) and invoke `self-improvement` in the same turn. `hook-self-critique-reminder.py` nudges when this section is substantive. For friction recurring across ≥2 leaves, run `overcome-difficulty` against the agent-system-as-plan first (see [systemic-pattern-scan.md](systemic-pattern-scan.md)).
```

**Temporal frontmatter** (`created`, `last_verified`) follows the same contract as `leaf/v1` (see [leaf-schema.md](leaf-schema.md) § Temporal frontmatter): both are ISO `YYYY-MM-DD`, required, and set/bumped by tooling. `record-experience.py` stamps `created=last_verified=<today>` on every new/extended leaf. `last_accessed` is retired — validators reject it if present.

`generality` is **optional and implied 0** on an experience leaf — its absence *means* generality 0. This is deliberate: making it optional is precisely what avoids migration. Existing experience leaves carry no `generality` field and stay valid unchanged; `record-experience.py` emits `generality: 0` on **newly** created leaves only. There is no forced re-stamping and no conditional-required validator logic — a leaf without the field is the generality-0 profile by default.

### The `tier` field (difficulty tier, ADR-0002)

`tier` is **optional and implied 0** — the same migration-free contract as `generality`. It records the **tier of the difficulty** in the lifting hierarchy of [ADR-0002](../../docs/adr/0002-dialectical-transition.md): **tier 0** is a state-level difficulty `D=(s*,s)` (a divergence between desired and actual *states* — the overwhelming majority); **tier 1** is a difficulty `D⁽¹⁾=(P1,P0)` whose terms are *principles* — a refuted or sought rule, the fuel the σ (principle-revision) operator would consume. A clean run needs no tag: `record-experience.py` emits `tier:` only when `--tier 1` is passed, so absence reads as tier 0. The field is **not** the same axis as `generality` (which grades a record's *reusability*); `tier` grades *what the difficulty is about* (a state vs a principle).

The tag is a **σ-sentinel, not σ machinery** — its sole present purpose is to let `scripts/sigma-sentinel.py` measure the build-trigger for σ (condition **(A)**, re-refutation of an already-promoted principle) without yet building the operator. See [sigma-build-trigger.md](../../docs/sigma-build-trigger.md). `verify-experience-leaf.py` accepts it as an unknown-but-valid key (no validator change), exactly like `generality`.

## Ticket-driven work

When the task is a ticket (`ABC-123`), the **full structured record lives in the ticket** — the `tracker-management` skill posts the Difficulty / Order & criterion / Context / Working-plan body as the resolution comment. The leaf is then a **thin pointer**:

```
---
name: <slug>
description: <one-line reusable hook>
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "<quote>"
ticket: <KEY or URL>
created: <YYYY-MM-DD>
last_verified: <YYYY-MM-DD>
---

# <title>

Full structured record (Difficulty / Order & criterion / Context / Working plan) — in the ticket: <URL>.

<optional one-paragraph reusable distillation that a future *different* task needs and could not find by reading this ticket>
```

This keeps a single source of truth (the ticket), avoids context spent re-typing the plan, and prevents the leaf and ticket from diverging on later edits.

## What `verify-experience-leaf.py` enforces

- **Every** experience leaf: non-empty `resolution_confirmed_by_user` frontmatter (PreToolUse `Write` hook + `verify-all`). Writing on assumed resolution is a recurring failure mode.
- **`schema: difficulty/v1`** leaves additionally:
  - **standalone** (no `ticket:`): require `## Difficulty`, `## Order & criterion`, `## Contexts`, `## Cost` — and the `## Cost` section must not contain an unreplaced TODO placeholder; `agentctl resolve` auto-surfaces the plan cost figure, so the writer can fill the real value immediately.
  - **ticket** (`ticket:` non-empty): require the `ticket:` value to appear in the body (the pointer); sections relaxed — the record lives in the ticket.
- Leaves **without** a `schema:` field keep the legacy confirmation-only check (grandfathered).
- **Temporal fields:** like every leaf, an experience leaf carries `created` / `last_verified` (required); `record-experience.py` stamps `created`+`last_verified` on `new`/`extend`. `last_accessed` is retired — see [memory-temporal-frontmatter.md](memory-temporal-frontmatter.md) § last_accessed — retired.
- **`generality`** is accepted but **not required** on an experience leaf; its absence means generality 0 (the verifier ignores unknown/absent frontmatter keys, so no code change is needed to accept it). A leaf carrying `generality: 0` validates exactly as one without the field — this is the generality-0 profile of the unified model.
