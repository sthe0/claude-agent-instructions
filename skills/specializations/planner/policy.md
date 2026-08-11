# Planner policy — detailed mechanics

Elaboration moved out of `SKILL.md` to keep that trigger surface lean. The skill keeps each rule's one-line directive + a pointer here.

## Numbers and deadlines without a source

If the task has concrete numbers, deadlines, TTLs, or limits **without** an explicit link to a field / config / document:

1. **Do not guess** a match to a constant in code "by proximity".
2. **Find the source** — domain docs, wiki, project memory leaf, MCP query, semantic search, comments on the source artifact.
3. If no source — return `ESCALATE:` with the specific question; do not commit a numeric value in the plan without basis.
4. In "Problem and done criteria" record: **what each key number means** and **which system layer** it affects.
5. **A source is only half the check.** Citing where a number/claim comes from proves the ground *exists*; it does not prove the claim *follows* from it. In a stage's `Principle:` block state both — `Source:` (the ground) and `Derivation:` (how the claim follows from that ground) — so a reviewer can check the premise twice: (1) does the source exist, (2) does the claim actually follow. A `Derivation:` that merely restates the `Source:` or the principle `statement` collapses the two checks back into one and is rejected by `plan.py`.
6. **Generalizes to every load-bearing decision/judgment**, not numbers only, for a reasoning/research deliverable (`--deliverable-kind reasoning` or `mixed` at classify). Record each as a claim in the provenance ledger: `agentctl ledger-add --status axiom|derivation|assumption ...` — axiom needs `--source`, derivation needs `--premise` ids to established claims, assumption needs `--basis`. The resolution gate blocks until the ledger is closed. Your own enumeration is primary; `agentctl ledger-enumerate` runs an independent second reading that only *raises* candidates it finds (recall < 100%, not a substitute) — each raised candidate must be recorded (`ledger-dispose --as recorded --claim <id>`) or dismissed with a reason (`--as dismissed --reason ...`) before resolution. Detail: [formalization-ladder-l1-l3.md](../../../memory-global/leaves/formalization-ladder-l1-l3.md).

## Gathering context

- Read the user's request and any linked source artifacts (tickets, RFCs, parent tasks) for the full picture.
- Comments on those artifacts — accepted decisions and links.
- Wiki / docs linked from them — read them.
- Familiar domain → relevant project memory leaves only.

## Reuse vs generalization

If the search surfaces a precedent for the current task, two outcomes:

1. **Reuse.** The prior solution applies almost as-is. The plan becomes "apply the recipe from `<source>` with these adjustments: …".

2. **Generalize.** The current task is the second (or third) instance of the same kind, and the precedent solved it as a one-off. Present **two alternatives** to the manager:
   - **(a) One-off** — solve this instance the same way as the precedent. Cheaper now, repeats the work next time.
   - **(b) Generalized** — extract the shared piece into a reusable abstraction (script, skill, leaf) and apply it here as its first consumer. Heavier now, cheaper later.

   Generalization is only applicable to systems we have edit access to (the instructions repo, project memory, project scripts, etc.). If the shared piece lives in a system we cannot modify, plan = (a) only — state the constraint explicitly.

   The manager surfaces both alternatives to the user for the choice; do not pre-decide.

If no precedent surfaces — no extra step; plan from scratch.

## Cost and resource assessment

The Dimensions table under `SKILL.md` § Cost and resource assessment sizes a stage by **executor work volume** (implementation effort, means reused, ongoing resources, maintenance surface, stability). That table alone under-counts a stage whose deliverable must be **accepted by a reviewer**: each **review round** — a full declare → investigate → critique or plan-review cycle triggered by a rejected verdict — costs roughly as much active effort as a major stage of the plan itself, and a stage estimate that only sums executor steps misses it entirely.

The trigger is **not** "this stage's own check is unmechanized". Concrete example: the `judge-import-blindness-and-norm-debt` plan spent **two** review rounds on the stage that mechanized pre-submit check observation — a stage whose own criterion was a fully mechanical `verify_command`, green on its first run — because what the reviewer contested was the **boundary of what the new mechanism claims to cover**, which no green check can attest. The following stage, four normalization records, then spent rounds of its own on the same axis — a reviewer contesting the placement and provenance of records whose own check was a green grep. So: when a stage's deliverable is a **norm, a verdict, or a mechanism's claimed coverage** — anything whose acceptance is a reviewer's judgment even where the stage's own check is mechanical — budget at least one review round beyond the Dimensions estimate, and say so explicitly in the stage's cost note.

Budgeting a round must not legitimize one that a cheaper discriminator would have removed. [[2026-08-05-deferring-ask-gate-and-the-binding-timeout-layer]] § Self-critique carries the paired obligation: three rounds each justified by a genuine blocker were still an effort signal, and two of them would have collapsed into one had the runtime axis been exercised **before** the first review. Budget the round; spend the cheapest discriminator first anyway.

## Submission-seam fields

`submission.py`'s `_SUBSTANTIVE_SUBMISSION_FIELDS` refuses a substantive plan at the submit-plan seam when a stage omits one of these — each fills a distinct functional place the plan-activity ontology names, not a restatement of a field already required elsewhere:

- **`knowledge`** — what must already be known for the declared method over the declared means to reach the result image. A place of its own (element 8), separate from `principle` (why this method/means was chosen) and from `means`/`method` (what is used, and the requirement on using it) — knowing a fact and choosing an approach on its basis are different acts. Alternatively satisfiable by a supply edge: `[[stage.supplies]] on = <n>, element = "knowledge"` carries a dependency stage's own knowledge forward, so a stage need not restate what an earlier stage already established.
- **`material_refs`** — the structural projection of `material`: the concrete symbols (file · function/class · config key) this stage TRANSFORMS. An empty list reads the same as an absent key — a substantive stage that transforms nothing is not a case this grade admits.
- **`knowledge_refs`** — the structural projection of `knowledge`: the symbols this stage only READS to ground its method, without transforming them.
- **A symbol named in both `material_refs` and `knowledge_refs` is a smell.** It is not refused at the seam — a stage can legitimately read a symbol to understand its current shape and then transform that same symbol — but the overlap must be justified in the stage's own prose (why the same symbol is both a source of knowledge and a target of change), not left for a reader to wonder whether it is a copy-paste artifact.
- **`preconditions`** — the starting requirements the stage's execution depends on (an approved upstream decision, a branch checked out, a fixture present), kept apart from `conditions` (properties the stage runs under / must preserve) because a precondition failing means the stage cannot even begin, where a condition violated mid-stage means the stage did the wrong thing.
- **`means.method`** / **`means.procedure`** — see `SKILL.md` § Plan format's Means & method / Procedure bullets; both are submission-required once a stage is substantive.

## Meta submission fields

`submission._SUBSTANTIVE_META_FIELDS` requires three `[meta]`-level fields on every substantive plan. **Falsiness is the test throughout** — an empty string, and an empty `[[final_check]]` list, read the same as an absent key, exactly as § Submission-seam fields states for `material_refs` / `knowledge_refs`; writing the key with nothing in it does not satisfy the seam.

- **`goal`** / **`done_criterion`** — see `SKILL.md` § Plan format's Problem-and-done-criteria item: the plain-language end state and the definition-of-done, cached by the engine as the session's own comparison target.
- **`final_check`** — the plan's end-to-end verification list (`## Final verification`, § Plan format item 6), run at resolution; each entry is a `[[final_check]]` table naming a `venue` and a check, the meta-level analogue of a stage's `verify_command`. It is not a summary of the stage checks and is not satisfiable by them: a plan whose only controls are per-stage asserts that each step went as declared and nothing about the whole, and the assembled product is what `verify-final` re-runs these against.

## Order

For a substantive plan, `[meta.order]` states the order this plan answers — the customer, the need, and the requirements on the product — structurally rather than only in the free-text Context section, so a machine (not only a reviewer) can check the plan against it:

- **`customer_id`** — the machine-comparable identifier of the position the order came from (a role, a ticket reporter, a team). The identifier half of the customer pair: an acceptance author is checked against this field, and comparing an author against a paragraph is either vacuous or absurd.
- **`customer`** — the position that identifier names, in prose. The other half of the pair — `customer_id` is what a machine compares, this is what a reader needs to know whose requirements these are.
- **`functional_place`** — the place this plan's product fills, and in what respect its current filling is inadequate. A need is a functional place stripped of adequate filling, which is why the two are one field and not two.
- **`requirements`** — the requirements on the product, as `{id = "R1", text = "..."}` pairs, never bare sentences: the id is the key `[meta.order.coverage]` and every acceptance verdict range over, and a list of prose leaves that load-bearing key as something someone has to parse back out later.
- **`[meta.order.coverage]`** — a table mapping each requirement id to the control(s) that decide it, e.g. `R1 = ["stage 2 verify_command"]`, `R2 = ["final_check 1"]`, `R3 = ["stage 3 landed assertion"]`. `submission.py`'s `_order_violations` checks this map is total in the PRESENCE direction (every declared requirement has an entry and vice versa); `scripts/check-order-coverage.py` checks the RESOLUTION direction — that each named control actually exists in the plan (a real stage's `verify_command`, a real `final_check` index, or a real landed-kind stage). Run it against the plan file as part of a substantive plan's own verification before presenting it for approval.

A narrowing chosen at authoring time — a requirement the plan deliberately does not cover — is not silently dropped: name it and why in the turn carrying the plan's essence (`CLAUDE.md` § Escalation to the user), the same discipline that governs any other cut against the order.
