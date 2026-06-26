# ADR-0001 — Consensus architecture for a distributed agent system

- **Status:** Proposed (2026-06-26)
- **Deciders:** system authors (commit-rights holders to this repo)
- **Difficulty removed:** when the agent system is handed to several developers, edits to its instructions (`CLAUDE.md`, `skills/`, `memory-*`, `config.md`) from different people conflict, and there is no systematic way to reach consensus.

## Context

The Claude-agent system is distributed to multiple developers. Each may edit the shared
instructions. Conflicts arise in three distinct classes, only the first of which git resolves:

1. **Textual** — two edits to the same lines. Git detects it. Solved.
2. **Semantic** — edits to *different* locations that contradict in meaning (rule *A* in one
   file vs. rule *not-A* in another). Git is blind to this; it needs a behavioural / meaning
   check, not a line diff.
3. **Layered / upstream-vs-local** — a shared core evolves while each developer keeps personal
   overrides on top. The correct operation is **override + rebase**, not **merge**.

The resolution must rest on the system's own ontology — *difficulty → generalization →
invariant/principle* — rather than on an external bureaucratic process bolted on top.

## Decision drivers

- **D1** — Core instructions are a shared resource; an uncontrolled edit breaks everyone.
- **D2** — Personal / project adaptation must stay cheap and must not require shared consensus.
- **D3** — Semantic conflicts are invisible to git → a behavioural / meaning check is required.
- **D4** — Consensus must not be blockable by a single vetoer (no-veto), yet must not be anarchy.
- **D5** — Decisions must be grounded in the system's own *difficulty → principle* ontology, not
  in an external process.

## Research findings (three deep-research passes, condensed)

Surveyed: structured/semantic merge, policy-as-code gates, graduated open-source consensus,
layered-config precedence (Helm/Kustomize/Spring/Viper/Dynaconf), prompt-management platforms
(MLflow/LangSmith/Langfuse/Humanloop/Agenta), and dotfiles/upstream-reconciliation tooling.

- **Precedence is universally documented and last-wins.** Every surveyed config system documents a
  single fixed lowest→highest ladder. Critically, **all of them default to *replacing* a complex
  value wholesale, not field-level merging** (Viper "entirely replaced"; Dynaconf last-wins; Helm
  deep-merges only map leaves). ⇒ An instruction-layering scheme **must explicitly choose**
  merge-vs-replace; it cannot assume merge.
- **Fail-closed gates** (OPA `--fail`/`--fail-defined`, Atlantis) — a machine check blocks by
  default until policy passes.
- **Graduated consensus without veto** — Apache lazy-consensus (72h silence = assent), Fuchsia RFC
  rough-consensus (no single veto), scikit-learn tiered approval thresholds.
- **Layered authority** — OpenAI Model Spec (Root > System > Developer > User > Guideline, recency
  tiebreak); cascading `CLAUDE.md`/`AGENTS.md` resolve nearest-wins by proximity.
- **Role-based authority + protected core** — `CODEOWNERS` + protected branches; MLflow RBAC
  (READ < USE < EDIT < MANAGE; EDIT cannot delete; wildcard grants). Notably, MLflow **deliberately
  ships no native approval workflow / arbitration** — that is pushed to external CI/CD. This is a
  *negative* result that **justifies the active synthesizer below**: even a mature platform leaves
  consensus/arbitration unaddressed.
- **`git rerere`** — replays recorded conflict resolutions across rebase iterations, so a personal
  layer rebased onto a moving core does not re-resolve the *identical* recurring conflict each time.
  Caveat: only identical recurring conflicts auto-resolve; if core alters the same hunk, a human (or
  the synthesizer) is required.

Industry-gap honesty: public approval-workflow details for Humanloop / Agenta / LangSmith were not
confirmed even after three passes — this is a gap in the *industry's* public documentation, not in
this analysis.

## Decision

### The single primitive

The whole architecture is derived from one operation — **critique**: compare two objects and
extract their **commonality (the invariant)** and their **difference (the boundary)**.

- Applied to *an example and an attempt to repeat it* → it induces a **principle** (the commonality)
  and a **refutation condition** (the difference).
- Applied to *two divergent contributor edits* → it yields the **invariant promoted to Core** (the
  commonality) plus the **context-local residue routed to Team/Personal** (the difference).

**Planning** is the synthesis half that *consumes* critique: find the **means** to overcome the
*difference*, and the **conditions** under which the *commonality* — what must be held invariant —
is preserved. These map directly onto the existing plan-activity elements `means` /
`conditions+invariants` / `refutable principle` (see `plan-activity-ontology`).

The full loop is **self-similar across scopes** (an applied task, self-improvement of the system
itself, and conflict resolution are the *same* mechanism):

> **critique (commonality / difference) → plan (means for the difference, conditions for the
> commonality) → act → critique**

Conflict resolution is therefore **generalization over two attempts to remove the same difficulty**,
and the active consensus-forging agent *is* the generalization engine — not a separate device.

### Principle as a concept with a generality gradient

- A difficulty/success is a **trivial principle** (generality level 0): "this worked once, here."
  Planning with it is literal repetition.
- Repetition + diff (critique) lifts a principle up the generality axis. **Refutation ≡
  generalization** — one operation: the *difference* found when repetition fails to reproduce the
  result is exactly what drives the principle to a broader form.
- `difficulty/v1` experience leaves are principles at level 0. Storage is a **fractal `principles/`
  tier** (the same `MEMORY.md → sub-index → leaf` shape), with provenance links down to the
  difficulties a principle was induced from and a refutation condition attached to each.
- Consumption: at the plan's `refutable principle` element, the planner **retrieves** relevant
  principles to ground each stage (retrieval-augmented planning over a principle library). The
  existing `coordinator-pitfalls.md` "symptom → better" table is the informal proto-version of this
  tier.

### Substrate — precedence layers

- Order `Core < Team < Personal`, **nearest-wins / last-wins**, documented as one explicit list.
- **Merge semantics are explicit**: replacement at the **atomic-leaf** level (prose is never merged
  — "one fact = one file" makes the leaf the unit of replacement); deep-merge is used **only** for
  the structured `config.md` constants (Helm-leaf style).
- For ordered layers (`skills/`, leaves): pin the insertion semantics (append vs prepend) and its
  version stability (cf. the Kustomize array-merge version gotcha).
- The personal layer is maintained with `git pull --autostash --rebase` + **`git rerere`**.

### Active synthesizer over batch accumulation (Variant D)

A dedicated agent runs the pipeline:

`normalize-to-difficulty → cluster-by-functional-ground → detect-conflict →
induce-invariant (critique: commonality/difference) → ranked menu to the admin (AskUserQuestion) →
promote-to-layer`

The agent **proposes, it does not execute**; there is **no veto**; authority is graduated. The
"induce-invariant" step is the single primitive above, applied to pairs of edits.

### Governance over the core slice (the operator's points 1–3)

1. **Authority gate** — Core is edited only by commit-authorized **system authors** (`CODEOWNERS` +
   commit identity; MLflow-style RBAC: Core = MANAGE, Team = EDIT, Personal = EDIT-in-scope).
   Non-authors may **only append to the inbox**, never edit Core. For now there is a single author.
2. **Difficulty inbox** — instead of editing Core immediately, `self-improvement` files a difficulty
   record (`layer: core`, `severity`, functional ground) into a shared append-only sink.
3. **Critical-mass trigger** — a cluster's mass = Σ severity (optionally recency-decayed); when it
   crosses a threshold (a new `config.md` constant), **or** a single `critical` item arrives, the
   accumulated cluster is surfaced to a system author for a *batched* Core change through the normal
   `planner → approval → developer` spine.

The Personal / project layers keep the **fast apply** path; governance is the wrapper around
applying the single primitive to the *Core* slice. This is what removes the original difficulty:
Core changes only via a distilled, accumulated, author-approved principle — never via a raw one-off
edit.

## Considered options

- **A. Git / textual merge only** — misses semantic conflicts (class 2). Insufficient.
- **B. Precedence layers (override) only** — removes class 3 but provides no consensus on the core
  itself.
- **C. Fail-closed gate + eval-suite only** — catches regressions but does not *synthesize* a
  resolution; it offloads the resolution to a human.
- **D. Active synthesizer over batch accumulation + layers + gate + principle loop (chosen)** — the
  only option that *synthesizes* an invariant rather than picking a winner, and the only one grounded
  in the system's own ontology. A/B/C are subsumed as substrate of D.

## Consequences

**Positive**

- One primitive (critique: commonality/difference) underlies conflict resolution, principle
  induction, and refutation → low cognitive cost, high internal consistency.
- Core is protected while personal adaptation stays cheap.
- Decisions are grounded in accumulated, refutable principles, not ad-hoc edits.
- Self-similar: the same loop scales across applied tasks, self-improvement, and conflict resolution.

**Negative / risks**

- Critique-as-a-service for semantic conflicts needs a behavioural eval suite (run cost).
- The critical-mass formula is a heuristic and needs calibration.
- A multi-developer inbox must be shared (a file in the repo or a tracker), which moves it out of the
  gate-exempt memory scope.
- The active synthesizer is the original, non-off-the-shelf part of this design → higher risk; it
  needs validation against real conflict streams.

## Open questions

1. Physical home of the difficulty inbox for multiple developers (shared file in the repo vs. a
   tracker).
2. Exact critical-mass formula (weighted count vs. recency-decayed) and threshold value.
3. The substrate for the behavioural eval of semantic conflicts (a tenet test-suite).
4. Approval workflows of Humanloop / Agenta / LangSmith remain publicly undocumented (an industry
   gap, not a blocker).

## References

- Three deep-research passes (precedence / governance / practitioner), 2026-06-26.
- `memory-global/leaves/plan-activity-ontology.md` — the 8 plan elements (`means`,
  `conditions+invariants`, `refutable principle`).
- `memory-global/leaves/coordinator-pitfalls.md` — the proto-principle "symptom → better" table.
- OpenAI Model Spec (layered authority); MLflow RBAC + alias promotion; Open Policy Agent
  (`--fail`); Apache lazy-consensus; Fuchsia RFC rough-consensus; `git rerere`; `CODEOWNERS` +
  protected branches; Helm / Kustomize / Spring / Viper / Dynaconf layered config.
