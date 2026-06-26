---
name: 2026-06-26-critique-primitive-unifies-conflict-and-principle
description: Difficulty — designing the multi-developer instruction-distribution / consensus architecture, the option space first came out as only passive mechanisms (precedence layers, governance gates) and the design stayed un-unified across three sub-problems (conflict resolution, principle induction, refutation). The unlock — a single critique primitive (compare two objects -> commonality=invariant + difference=boundary) is the same operation behind conflict resolution, principle induction from experience, and refutation of a trivial principle. Full design record is ADR-0001; this leaf is the thin pointer + the reusable lessons.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "Да, решена"
refs: [2026-06-24-gate-exemption-is-category-error-for-result-images, 2026-05-26-agent-system-plan-vs-reality-drift, 2026-06-04-org-specific-vs-global-placement]
---

# One critique primitive (commonality / difference) unifies conflict resolution, principle induction, and refutation

## Difficulty
When the agent system is distributed to multiple developers, edits to its instructions conflict. Designing the resolution architecture had two traps. (1) On first enumeration the option space was narrowed to **passive** mechanisms only (precedence layers, governance gates) — missing the active-agentic axis (a synthesizer agent that proposes ranked resolutions) and the cadence axis (batch-accumulate contributions vs process each immediately); the user expected exactly those. (2) The design risked staying three disconnected machines — one for conflict resolution, one for inducing principles from experience leaves, one for refuting a principle — when they are the **same operation**.

## Order & criterion
1) When enumerating options for an architectural question, cover the **full space**: include the active-agentic variant (a dedicated agent that proposes, not just a passive rule) and the batch-vs-continuous cadence axis, and lean on the system's own ontology (difficulty -> generalization -> induced invariant), not only a catalog of passive mechanisms. Recorded as a behavioral rule in `coordinator-pitfalls.md` (commit bd1dc0b).
2) Reach for **one primitive — critique**: compare two objects, extract **commonality (the invariant to hold)** and **difference (the boundary)**. It unifies: (a) example + a repetition attempt -> a principle plus its refutation condition; (b) two divergent contributor edits removing the same difficulty -> the invariant promoted to Core, the context-local residue kept at Team/Personal. Conflict resolution **is** generalization over two attempts to remove one difficulty; refutation **is** generalization. Planning then consumes the critique: find the **means** to overcome the difference and the **conditions** that preserve the commonality.
3) An experience leaf (`difficulty/v1`) is a **trivial/degenerate principle** at generality level 0; a successful repetition + the common/different diff lifts it up a generality gradient. This is why principles store naturally in the fractal ontology — they generalize difficulty-removal by definition.
4) Decouple **submission** (non-author, no push rights — files via a channel they already have write to) from **aggregation** (author, has push — pulls all channels, clusters by functional ground, computes mass = Sum severity, triggers core self-improvement at threshold). Multi-tracker by a pluggable `DifficultyChannel` port.

**Acceptance check:** acceptance-review — user confirmed "Да, решена" after reviewing ADR-0001 and the committed root principle.

## Contexts

### 2026-06-26 — ADR-0001 consensus architecture
- Where it arose: designing distribution + conflict-reconciliation for the agent instruction system (CLAUDE.md + skills + memory leaves) across internal (Startrek) and external (GitHub/Linear/Jira) developers.
- Full design record: `docs/adr/0001-consensus-architecture.md` (commits d9b77da + amend 8568a84). Do not re-derive the design here — read the ADR.
- Spun-off behavioral rules already in their canonical homes: option-space enumeration -> `coordinator-pitfalls.md` (bd1dc0b); "formalize deterministic action sequences as code" root principle -> `CLAUDE.md` preamble + cursor mirror (51f623a).

### 2026-06-26 — S1–S4 implementation (the design built out)
- ADR-0001 implemented whole as one approved 14-stage plan (S1 precedence layers + fractal `principles/`; S2 `difficulty_channel` port + Startrek/external adapters; S3 `core-difficulty-digest` clustering + `authority` routing; S4 `consensus-synthesizer` propose-only + `consensus_eval` semantic-conflict substrate + threshold calibration). Code is the record — read `scripts/difficulty_channel/`, `scripts/core-difficulty-digest.py`, `scripts/consensus-synthesizer.py`, `scripts/consensus_eval/`, ADR status now Accepted.
- **Reusable implementation gotcha (cost real debug time):** loading a *hyphenated* script (`core-difficulty-digest.py`) via `importlib.util` for reuse in tests/other scripts — a `@dataclass` inside it fails with `AttributeError: 'NoneType' has no '__dict__'` because the dataclass machinery resolves `cls.__module__` through `sys.modules`, which the spec-loader hasn't populated yet. Fix: register `sys.modules[mod_name] = mod` **before** `spec.loader.exec_module(mod)`. Subpackages (`difficulty_channel.adapters`) should instead be imported normally (conftest puts `scripts/` on `sys.path`) — don't importlib-load a package by file path.
- **Reuse discipline that held:** one ranking engine — `tokenize()`/`term_score()` extracted into `record-experience.py`, reused verbatim by digest clustering and synthesizer; no second similarity engine was written. The same primitive backs search-before-record, cluster-by-functional-ground, and the critique commonality/difference split.
- Mechanical: every new `scripts/*.py` with a shebang needs `chmod +x` (lint-hooks-executable) + a `scripts/README.md` row (verify-readme); package modules (no shebang) need neither.


### 2026-06-27 — principle-induction recurrence signal made machine-computable
- Where it arose: Implementing the principle-induction arm of ADR-0001: accumulated difficulty recurrence produced no machine signal, so 'lift a recurring difficulty into a principle' never fired automatically and fragmented similar leaves were invisible to each other (the clustering/mass/flag code ran only over external channels, never the experience corpus).
- Working plan: Factor the single cluster-by-functional-ground primitive into record-experience.py (one ranking/clustering engine); add promote-scan (clusters corpus, sums Σ '### ' contexts derived from the body, flags clusters >= principle-promotion-threshold=3 [Rule of Three] as principle/v1 candidates, surfaces fragmentation); add a deterministic cmd_new fragmentation guard (refuse forking an analogous leaf at _similarity >= JOIN_RATIO unless --justify-new). Key lessons: count derived from body not a frontmatter field (no drift); JOIN_RATIO=0.6 catches near-duplicates (lexical overlap) only, not mere topical similarity, so it is a speed-bump atop search-before, not a replacement; reuse the existing ranking engine rather than a second clustering copy.

## Common core & variations
**Common:** Principle induction from experience is the critique primitive (commonality across recurrences) made operational: a recurrence COUNT over functional-ground clusters is the machine signal that drives promotion; the same _similarity/JOIN_RATIO clusters, guards new, and (via the digest) flags Core difficulties — one engine.

**Variations:** ADR-0001 establishes the unifying primitive conceptually; this task makes the experience→principle promotion signal computable (promote-scan) and adds write-time anti-fragmentation (cmd_new guard). Threshold semantics differ from the digest's severity-mass: recurrence count (rule-of-three) vs Σ severity.

## Cost
manager in-thread, engine-driven (design: multiple reset/classify/plan/approve/partition spine runs; implementation: 14-stage per-stage next-stage/verify/commit/record-result loop), one independent `code-reviewer` spawn at final verification (2 blocking + 6 non-blocking findings, all addressed). Design commits bd1dc0b, d9b77da, 8568a84, 51f623a; implementation shipped as 4 logical slices (S1–S4) over ~14 commits ending abc3560. All pushed to origin/main. 638 tests green, verify-all OK.

## Self-critique of the agent system
The deterministic agentctl spine was hand-walked ~20 tool calls across the session — which the user flagged directly ("Важно максимально все детерминированные последовательности действий оформлять кодом"), and which became the committed root principle plus a follow-up task to build an `agentctl drive`/`close` wrapper. The narrowed-option-space slip (passive-only) repeated a pattern already worth a standing rule; that rule now exists. Lesson carried forward: when a deterministic ceremony is being hand-issued repeatedly, that is itself a difficulty signal — stop and encode it.
