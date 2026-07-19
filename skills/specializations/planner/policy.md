# Planner policy — detailed mechanics

Elaboration moved out of `SKILL.md` to keep that trigger surface lean. The skill keeps each rule's one-line directive + a pointer here.

## Numbers and deadlines without a source

If the task has concrete numbers, deadlines, TTLs, or limits **without** an explicit link to a field / config / document:

1. **Do not guess** a match to a constant in code "by proximity".
2. **Find the source** — domain docs, wiki, project memory leaf, MCP query, semantic search, comments on the source artifact.
3. If no source — return `ESCALATE:` with the specific question; do not commit a numeric value in the plan without basis.
4. In "Problem and done criteria" record: **what each key number means** and **which system layer** it affects.
5. **A source is only half the check.** Citing where a number/claim comes from proves the ground *exists*; it does not prove the claim *follows* from it. In a stage's `Principle:` block state both — `Source:` (the ground) and `Derivation:` (how the claim follows from that ground) — so a reviewer can check the premise twice: (1) does the source exist, (2) does the claim actually follow. A `Derivation:` that merely restates the `Source:` or the principle `statement` collapses the two checks back into one and is rejected by `plan.py` / `verify-plan-file.py`.
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
