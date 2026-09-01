---
name: memory-hierarchy
description: When and how to split memory into sub-indexes — default 2 levels (MEMORY.md + leaves), spin off sub-indexes for monotonic/domain-coherent/large content; two decomposition axes (part-whole, base-service); bias to more layers once a cut is warranted; generalize-and-group recurring near-duplicates
type: reference
created: 2026-05-27
last_verified: 2026-09-01
---

# Memory hierarchy

Default: **2 levels** — a single top-level `MEMORY.md` index + leaves in `leaves/` (or named subdirectories without their own indexes). This stays scannable and is what the harness auto-loads.

Spin off a **sub-index** (`<subdir>/MEMORY.md` listing only that subdir's leaves) when at least one trigger fires.

## Spin-off triggers

| Trigger | Rationale | Examples |
|---|---|---|
| **Monotonic growth** | Content is append-only and date-prefixed; retroactive migration is more painful than spinning off early. | `experience/` (one leaf per resolved task), `retrospectives/`, daily/weekly journals |
| **Domain coherence** | Self-contained navigation domain where browsing-within-domain pays off; readers come "looking for system-knowledge", not for everything mentioning component X. | `system-knowledge/`, `runbooks/`, `troubleshooting/` |
| **Display pressure** | A section in the top-level index would exceed ~30 lines, or the whole `MEMORY.md` approaches the 200-line harness truncation ceiling (CONFIRMED real, not cosmetic — verified 2026-07-23 against the installed client bundle; see [MEMORY.md](../MEMORY.md) line 5). | Long product-runbook section, accumulated coordination-discipline pointers |

**Bias to more layers once a cut is warranted.** When a trigger *does* fire, don't hesitate to add depth — a tree of small, single-purpose sub-indexes reads faster than one flat overgrown index, and a navigable decomposition is instrumental to reflexion (the search space of every self-improvement / overcome-difficulty is this same knowledge space — see [[reflexive-exit-is-base-activity-figure]]). **But don't split prophylactically:** 3 leaves in a section do not warrant a sub-index unless a trigger applies. The bias is toward *depth when cutting*, not toward *cutting when idle* — bureaucratic empty sub-indexes are worse than a slightly long top-level. This trigger table governs the canonical (content-decomposition) spin-off; a distinct **index-only** variant, driven by a mechanical redundancy measurement rather than by these triggers, is scoped separately below (§ Index-only variant: redundant-partition spin-off).

## Two decomposition axes — how to cut (canonical, content-decomposition)

Once a trigger says *split*, cut the set of entries along one of **two canonical axes** (the same axes the [[plan-activity-ontology]] uses to structure an activity — a memory index is the organizedness a body of leaves fills). These two axes decompose a *domain*; the **index-only** redundant-partition variant (§ below) is a separate, non-canonical cut that applies only when the pressure is purely on the index file's own bytes and the entries are otherwise heterogeneous — see that section before reaching for a third axis here.

| Axis | Question | Produces | Examples |
|---|---|---|---|
| **Part-whole (hierarchical / mereological)** | What are the *constituent parts* of this whole? | A sub-index per part | a `product/` domain → `product/{pipeline,models,eval}/`; a subsystem → its components |
| **Base-service (functional)** | What is the *base concern*, and what merely *serves* it? | A base sub-index + one per supporting concern | `coordination/` (base) vs `coordination/{session-mechanics,communication}/` (service); a runbook vs its auxiliary how-tos |

The two axes compose: carve the whole into parts (part-whole), then within a part separate its base concern from its service concerns (base-service). Each cut yields **an index**, not a loose pile — every sub-index is itself pointed at from its parent by a one-line pointer.

## Generalize and group

Before (or instead of) splitting, look for **recurring and similar** entries and **generalize-and-group** them:

- **Group** near-duplicate leaves that answer the same question under one entry — merge the content, keep the union of specifics, delete the duplicates.
- **Generalize** a cluster of same-shaped difficulties by lifting their commonality into a **parent** (a `principle/v1` leaf at the right generality level — see [[principle-leaf-schema]]) and pointing the specifics at it. This is the memory-side face of principle induction (`record-experience.py promote-scan` flags the cluster at `principle-promotion-threshold`).

Grouping shrinks the set *before* you decide how deep to cut, so the two-axis decomposition operates on generalized units rather than scattered near-duplicates.

## Spin-off mechanics

1. **Sub-index location.** `<subdir>/MEMORY.md` inside the subdirectory whose contents it indexes. Same frontmatter-less shape as the parent index (it's an index, not a memory). Header names the domain (e.g. "# Resolved-task experience"). This location rule is shared by the **index-only** variant (§ below) — `verify-memory-index.py` discovers sub-indexes as `leaves.rglob('MEMORY.md')`, so an index-only sub-index must live under `memory-global/leaves/` like any other.
2. **Top-level index update.** Replace the inlined section with a **one-line pointer** to the sub-index. Pattern: `- [<Domain>](<subdir>/MEMORY.md) — <one-line hook explaining what lives there>.`
3. **Sub-index entries.** Same pointer-line format as the parent. For monotonic content: order by date (most recent first or last — pick one convention per sub-index and keep it).
4. **No auto-load.** Sub-indexes are NOT loaded by the harness. They're read on demand when the top-level pointer leads you there. Keep them tight (≤ 200 lines per the same ceiling) so they read fast.
5. **Cross-link liberally.** Leaves under one sub-index can `[[name]]`-link to leaves under another. The sub-index boundary is for navigation, not for content isolation.

## Index-only variant: redundant-partition spin-off

A distinct variant applies when the pressure is purely on the **index file's own bytes** (harness truncation ceiling) rather than on any of the three canonical triggers above, and the entries at risk are heterogeneous — no domain coherence, no monotonic growth, no natural part-whole or base-service seam to cut along. This is an **index-only** spin-off: it relocates *pointers*, never leaf content, and the cut criterion is **redundancy against an external always-loaded surface** (e.g. `CLAUDE.md`), not either canonical axis.

**Redundancy test.** A pointer entry is *redundant* iff its target leaf's filename stem already occurs somewhere in the external surface that loads regardless of this index — that surface's own mention already keeps the norm always-loaded-visible, so the index's pointer is not the entry's only always-loaded notice. Redundant entries move into an `index-only` sub-index (still `<subdir>/MEMORY.md`, per the shared location rule in § Spin-off mechanics item 1). Entries whose stem is *not* named on the external surface are **sole-notice** — the index pointer is their only always-loaded visibility — and MUST stay in the parent index, unmoved.

**Re-derive live, don't recite.** The redundancy partition drifts as the external surface and the index both change independently; re-run the stem-membership test at spin-off time rather than trusting a prior measurement, and report any divergence from a prior count as drift rather than silently reconciling it.

**Scope: not a third canonical axis.** This redundancy criterion is deliberately **not** promoted alongside part-whole/base-service in § Two decomposition axes — it doesn't decompose a domain into parts, it partitions by an accidental, external relation (visibility elsewhere) that applies only to an index-only cut. It also carries a maintenance obligation the canonical axes don't: if the external surface later stops naming a moved entry's stem, that entry becomes sole-notice again and belongs back in the parent index.

## When NOT to spin off

- A short, stable section (≤ 10 lines, not growing). Just keep it inlined.
- A section that's intrinsically heterogeneous and doesn't form a domain (e.g. "miscellaneous corrections"). Splitting just hides things.
- A subdirectory with one leaf. Wait for triggers. (This "wait for triggers" default is the canonical spin-off's rule; the **index-only** variant does not wait for the three triggers above — it fires from the mechanical redundancy measurement described in § Index-only variant: redundant-partition spin-off, which can apply even to a single at-risk entry.)

## Renaming and retiring

- When a sub-index outgrows itself (>200 lines), split *its* contents into a further level (`<subdir>/<topic>/MEMORY.md`). Triggers compose.
- When a sub-index loses purpose (content drained out, last leaf removed), fold remaining content back into the parent and delete the sub-index. Don't leave empty/near-empty index files.

## Worked example

Before (parent inlines experience entries):

```
## Resolved-task experience

- [2026-05-26 — Session A Stage A smoke](experience/2026-05-26-session-a-stage-a-smoke.md) — long description
- [2026-05-27 — Token-saving audit](experience/2026-05-27-token-saving-audit.md) — long description
... (3rd, 4th, Nth entries accumulating)
```

After (parent points to sub-index):

```
## Resolved-task experience

- [Experience index](experience/MEMORY.md) — chronological log of resolved-task experience leaves.
```

Sub-index `experience/MEMORY.md`:

```
# Resolved-task experience

Chronological log of leaves recording how non-trivial tasks were resolved — lessons,
artifacts, costs.

- [2026-05-26 — Session A Stage A smoke](2026-05-26-session-a-stage-a-smoke.md) — long description
- [2026-05-27 — Token-saving audit](2026-05-27-token-saving-audit.md) — long description
```

The parent stays under display pressure; the sub-index becomes the natural reading surface when scrolling experience.
