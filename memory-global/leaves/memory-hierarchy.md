---
name: memory-hierarchy
description: When and how to split memory into sub-indexes — default 2 levels (MEMORY.md + leaves), spin off sub-indexes for monotonic/domain-coherent/large content
type: reference
---

# Memory hierarchy

Default: **2 levels** — a single top-level `MEMORY.md` index + leaves in `leaves/` (or named subdirectories without their own indexes). This stays scannable and is what the harness auto-loads.

Spin off a **sub-index** (`<subdir>/MEMORY.md` listing only that subdir's leaves) when at least one trigger fires.

## Spin-off triggers

| Trigger | Rationale | Examples |
|---|---|---|
| **Monotonic growth** | Content is append-only and date-prefixed; retroactive migration is more painful than spinning off early. | `experience/` (one leaf per resolved task), `retrospectives/`, daily/weekly journals |
| **Domain coherence** | Self-contained navigation domain where browsing-within-domain pays off; readers come "looking for system-knowledge", not for everything mentioning component X. | `system-knowledge/`, `runbooks/`, `troubleshooting/` |
| **Display pressure** | A section in the top-level index would exceed ~30 lines, or the whole `MEMORY.md` approaches the 200-line harness truncation ceiling. | Long product-runbook section, accumulated coordination-discipline pointers |

**Don't split prophylactically.** 3 leaves in a section do not warrant a sub-index unless triggers 1 or 2 apply. Bureaucratic sub-indexes are worse than a slightly long top-level.

## Spin-off mechanics

1. **Sub-index location.** `<subdir>/MEMORY.md` inside the subdirectory whose contents it indexes. Same frontmatter-less shape as the parent index (it's an index, not a memory). Header names the domain (e.g. "# Resolved-task experience").
2. **Top-level index update.** Replace the inlined section with a **one-line pointer** to the sub-index. Pattern: `- [<Domain>](<subdir>/MEMORY.md) — <one-line hook explaining what lives there>.`
3. **Sub-index entries.** Same pointer-line format as the parent. For monotonic content: order by date (most recent first or last — pick one convention per sub-index and keep it).
4. **No auto-load.** Sub-indexes are NOT loaded by the harness. They're read on demand when the top-level pointer leads you there. Keep them tight (≤ 200 lines per the same ceiling) so they read fast.
5. **Cross-link liberally.** Leaves under one sub-index can `[[name]]`-link to leaves under another. The sub-index boundary is for navigation, not for content isolation.

## When NOT to spin off

- A short, stable section (≤ 10 lines, not growing). Just keep it inlined.
- A section that's intrinsically heterogeneous and doesn't form a domain (e.g. "miscellaneous corrections"). Splitting just hides things.
- A subdirectory with one leaf. Wait for triggers.

## Renaming and retiring

- When a sub-index outgrows itself (>200 lines), split *its* contents into a further level (`<subdir>/<topic>/MEMORY.md`). Triggers compose.
- When a sub-index loses purpose (content drained out, last leaf removed), fold remaining content back into the parent and delete the sub-index. Don't leave empty/near-empty index files.

## Worked example

Before (parent inlines experience entries):

```
## Resolved-task experience

- [2026-05-26 — DEEPAGENT-415 Stage A smoke](experience/2026-05-26-deepagent-415-stage-a-smoke.md) — long description
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

- [2026-05-26 — DEEPAGENT-415 Stage A smoke](2026-05-26-deepagent-415-stage-a-smoke.md) — long description
- [2026-05-27 — Token-saving audit](2026-05-27-token-saving-audit.md) — long description
```

The parent stays under display pressure; the sub-index becomes the natural reading surface when scrolling experience.
