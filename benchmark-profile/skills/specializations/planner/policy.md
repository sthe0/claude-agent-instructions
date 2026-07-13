# Planner policy — detailed mechanics

Elaboration moved out of `SKILL.md` to keep that trigger surface lean. The skill keeps each rule's one-line directive + a pointer here.

## Numbers and deadlines without a source

If the task has concrete numbers, deadlines, TTLs, or limits **without** an explicit link to a field / config / document:

1. **Do not guess** a match to a constant in code "by proximity".
2. **Find the source** — domain docs, wiki, project memory leaf, MCP query, semantic search, comments on the source artifact.
3. If no source — return `ESCALATE:` with the specific question; do not commit a numeric value in the plan without basis.
4. In "Problem and done criteria" record: **what each key number means** and **which system layer** it affects.

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
