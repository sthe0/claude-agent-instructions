---
name: leaf-schema
description: The retired rigid section schema for ordinary memory leaves — required ## Difficulty / ## Guidance / ## See also, the grandfather rule for un-migrated leaves, and what verify-leaf-structure.py enforces. Experience leaves use difficulty/v1 instead (see experience-leaf-schema.md).
type: reference
schema: ontology/v1
converted_from: leaf/v1
created: 2026-06-24
last_verified: 2026-06-29
---

# Memory leaf schema (retired section form)

## Difficulty

A memory leaf with no rigid structure drifts into a free-floating fact whose **functional ground** — the difficulty it removes — is implicit or absent. Without that ground you cannot apply the fact correctly, judge whether it is still worth keeping, or tell signal from noise (the universal principle in `CLAUDE.md`: every leaf exists to remove a named difficulty, stated in "to achieve X, do Y" form). `leaf/v1` makes the ground a required, machine-checked section so a leaf cannot be written without naming why it exists.

## Distinction

Separates an ordinary memory leaf that names the difficulty it removes (a leaf/v1 leaf with required sections ## Difficulty, ## Guidance and ## See also, plus frontmatter name, description, type, schema, created and last_verified) from a free-floating fact whose functional ground is implicit or absent. The Difficulty section states the divergence between desired and actual state as the X in "to achieve X, do Y"; Guidance carries the durable fact, rule or runbook; See also holds cross-references and may be empty but must be present. It also separates opted-in leaves (carrying schema: leaf/v1, enforced by scripts/verify-leaf-structure.py) from grandfathered un-migrated leaves, and ordinary leaves from experience leaves, which carry the difficulty/v1 schema.

## Adequacy

The distinction is inadequate if leaves that carry all three required sections still leave their applicability or prunability undetermined (a Difficulty section that names no divergence), so that the section check passes while the ground stays implicit; or if leaves without the sections are applied and pruned as reliably as those with them; or if the Difficulty and Guidance sections cannot be told apart in practice (a leaf whose Difficulty section holds the rule). The retirement of leaf/v1 as a form in favour of principle/regularity/ontology leaves is itself evidence that the section split did not separate prescription from knowledge.

## Guidance

This was the single source of truth for the **ordinary-leaf** schema, now retired. It covered reference / feedback leaves and the `system-knowledge/` leaves. **Experience leaves use a different schema** (`schema: difficulty/v1`, see [experience-leaf-schema.md](experience-leaf-schema.md)) and are out of scope here.

### Frontmatter

Standard auto-memory frontmatter plus the schema tag and temporal fields. The example below is the retired `leaf/v1` shape; a leaf written today carries `principle/v1`, `regularity/v1` or `ontology/v1` instead (the retired tag was `leaf/v1`; the `schema:` value must stay bare, with no trailing comment):

```
---
name: <human title>
description: <one line — used for recall; for system-knowledge, name the difficulty>
type: reference | feedback
schema: <tag>
created: <YYYY-MM-DD>          # required — date first recorded; see memory-temporal-frontmatter
last_verified: <YYYY-MM-DD>    # required — date content last confirmed true; >= created
---
```

The two required date fields (`created` / `last_verified`) are common to every leaf schema and defined once in [memory-temporal-frontmatter.md](memory-temporal-frontmatter.md) — both required and tool-stamped. `last_accessed` is retired; validators reject it if present (see memory-temporal-frontmatter.md § last_accessed — retired).

### Required sections

A leaf in the retired section form contained these three H2 headers (enforced by regex on `^## `); a leaf converted to the principle, regularity or ontology form keeps the `## Difficulty` and `## See also` sections and replaces the middle one:

1. **`## Difficulty`** — the divergence between desired and actual state that this leaf removes; its functional ground. State it as the X in "to achieve X, do Y" — the goal the leaf serves; the Y is a norm and the knowledge behind it is a regularity (`regularity/v1`, [[regularity-leaf-schema]]), not part of this section. This is the section that makes the leaf applicable and prunable.
2. **`## Guidance`** — the durable fact, rule, or runbook itself (the "do Y"). Carries the body that the leaf existed for before the schema. Cite sources for OS / binary / version-dependent claims with a `> verified by: …` line.
3. **`## See also`** — cross-references: `[[other-leaf]]` links and pointers into the difficulty graph. May be **present-but-empty** if there are genuinely none — the header is still required.

### Grandfather rule

The retired form was enforced **only on opted-in leaves** and on new leaves; existing un-migrated leaves were grandfathered:

- A leaf opted into the retired form → the 3 sections were required (deny on any missing).
- A leaf **without** the tag → grandfathered. For `system-knowledge/` the carried-over **difficulty-lead baseline** still applies (the `description` names the difficulty, OR the body leads with a `> **Difficulty/Затруднение …** ` blockquote) — this is the rule the retired `verify-difficulty-lead.py` enforced, now folded into the same verifier. For other dirs, only the existing type + index guarantee applies.

The form is retired: convert a grandfathered leaf to the `principle/v1`, `regularity/v1` or `ontology/v1` form when you next touch it. `verify-principle-provenance.py` reports a leaf still tagged with the retired schema as `retired_leaf_schema`, and a converted principle is grounded in knowledge, typically regularities, and carries the `grounding_judged` stamp once the grounding judge accepts its link.

### What enforces it

`scripts/verify-leaf-structure.py` (subsumes the retired `verify-difficulty-lead.py`):

- **Scope:** non-experience leaves under any `leaves/**` (INCLUDING `system-knowledge/`, EXCLUDING `experience/`) plus project `.claude/agent-memory/**` leaves. `MEMORY.md` and `.gitkeep` are skipped.
- **Sections enforced per form** (the schema tag in the frontmatter decides; each section must be present, and for the two live forms non-empty): `leaf/v1` (retired, still checked while a leaf carries it) — `## Difficulty`, `## Guidance`, `## See also`; `regularity/v1` induced — `## Regularity`, `## Generality`, `## Refutation`, `## Induced from`; `regularity/v1` given (a `given:` field) — `## Regularity`, `## Given`, `## Refutation`; `ontology/v1` — `## Distinction`, `## Difficulty`, `## Adequacy`. A `principle/v1` leaf has no section check of its own here (its links are checked by `verify-principle-provenance.py`); it and a leaf with no schema both fall through to the grandfather rule above, so a `system-knowledge/` principle still gets the difficulty-lead baseline. The section lists live in `scripts/lib/leaf_forms.py`.
- **Modes:** default scan / `--staged` / `--hook` (PreToolUse, reads `tool_input.content`, exit 2 on violation) / `<path>` — mirroring `verify-experience-leaf.py`.
- Wired into `verify-all.py` (staged-aware) and the PreToolUse verifier-hook list in `scripts/install-reminder-hooks.sh`.

## See also

- [[experience-leaf-schema]] — the `difficulty/v1` schema for experience leaves (out of scope for the retired `leaf/v1`). It sits at generality 0 of the chain experience → regularity → principle; the retired `leaf/v1` shape is separate and not a point on that chain.
- [[regularity-leaf-schema]] — the `regularity/v1` schema (the knowledge layer induced from experience leaves).
- [[principle-leaf-schema]] — the `principle/v1` schema (the norm layer, grounded in regularities via `grounded_in`).
- [[memory-hierarchy]] — when to spin off `<subdir>/MEMORY.md` sub-indexes.
