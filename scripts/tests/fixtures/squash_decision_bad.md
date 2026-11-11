# Squash landing: measured decision

**Question.** Would landing each branch as one squashed commit, rebased onto the
tip, save enough conflict-resolution time to be worth it? The bar is set in
advance: adopt squash only if it saves at least 5 minutes per landing **and** the
`Task:` trailers that Core's checks read survive the squash.

**Answer: no.** On two weeks of real history, squash would save about
**0.16 min per landing**, and at most **1.3 min** under the most favourable count.
Both are far below the 5-minute bar. Merge-based landing stays.

The figures below are machine-checked: `scripts/measure-branch-refresh.py
--check-doc` recomputes every number in this file from the git history at the
stated revision and refuses the file if any number differs, or if the Decision
line contradicts the rule applied to those numbers.

## What was measured

A *refresh* is a merge that brings trunk into a working branch. When it
conflicts, someone resolves the conflict by hand; that resolution is the cost a
squash-and-rebase landing is supposed to remove. The saving per landing is
therefore estimated as

    conflicting refreshes × median resolution time / landings

Definitions, as the script implements them:

- **Landing** — one unit of delivered work on trunk's first-parent chain,
  keyed by its `Task:` trailer; a commit without the trailer counts as its own
  landing.
- **Refresh merge** — any merge commit on the first-parent chain except a
  pull-request merge made by the hosting service (recognised by its committer
  address).
- **Conflicting refresh** — a refresh whose re-merge (`git show
  --remerge-diff`) shows conflict markers, i.e. the recorded result differs
  from what an automatic merge would produce at a conflict.
- **Resolution time** — in the agent's session transcripts, the minutes from a
  `CONFLICT (...)` message on a path that exists in this repository to the next
  `git commit` call in the same session.

## Figures

Window: 2026-10-26..2026-11-09
Revision: 208f377b904f5c224f9278d45f592229d35c4145
Landings: 83
Refresh merges: 11
Conflicting refreshes: 2
Median resolution (min): 6.7
Saved per landing (min): 0.16
Threshold (min): 5
Task trailers survive squash: yes

Resolution samples from the transcripts, in minutes:

- Sample (min): 0.2
- Sample (min): 0.3
- Sample (min): 0.5
- Sample (min): 0.6
- Sample (min): 2.7
- Sample (min): 3.8
- Sample (min): 4.3
- Sample (min): 9.1
- Sample (min): 13.7
- Sample (min): 40.9
- Sample (min): 69.1
- Sample (min): 123.7
- Sample (min): 979.1
- Sample (min): 11095.1

Decision: reject

## Cross-check over the whole graph

Landing by fast-forward moves trunk's first-parent chain onto the branch that
landed last, so earlier branches and their refreshes drop off that chain. The
first-parent count above therefore undercounts refreshes. To make sure this does
not flip the answer, the script also counts over the whole commit graph and
divides by the number of distinct `Task:` values instead of all landings, which
gives an upper bound on the saving:

Whole-graph refresh merges: 37
Whole-graph conflicting refreshes: 10
Whole-graph Task values: 50
Whole-graph saved per landing, upper bound (min): 1.34

Even this bound is a quarter of the 5-minute bar.

## Caveats

- **The median is an upper bound.** A sample runs from the conflict message to
  the next commit, so it includes idle time and unrelated work in between; the
  multi-hour samples are clearly such cases. A smaller true median only lowers
  the saving further.
- **Unmatched refreshes are valued at the median.** Not every conflicting
  refresh has a transcript sample; each is assumed to cost the median.
- **Grouping by `Task:` shifts the count.** Commits without the trailer count
  as separate landings, which raises the denominator in the primary figure; the
  whole-graph bound divides by Task values alone to remove that bias.
- **Time zones differ.** Transcript timestamps are UTC, commit dates are local
  (UTC+3); at the window edges a few hours of samples may fall on the other side.

## Would the checks still work after a squash?

| What Core reads per commit | After a squash |
|---|---|
| `Task: <id>` line in the delivered range (the engine's landed check, landed providers, result recording) | Survives, provided the squash message composes every source commit's trailers into one trailer block. `--check-trailers` verifies this on a scratch repository. |
| `Agent-Session` / `Agent-Task` trailer of each commit | Collapses to one commit; per-commit attribution is lost. |
| Per-commit subjects and hashes (quality-regression investigation, marker-latency measurement) | Lost: the squashed commit replaces the history these tools walk. |

## Why squash is not worth it here

The saving is small for a structural reason, not only because conflicts are
rare. Rebasing a squashed branch onto trunk meets the same three-way conflicts a
refresh merge meets — the same files changed on both sides — so the
resolution work does not disappear; it moves. What squash really removes is the
merge commits in the history. In exchange, it produces a new tree that no
pre-land test run has covered, so it would need its own full verification
before landing, and it loses per-commit history that two Core tools rely on.

**Decision: keep merge-based landing.** Revisit only if a later measurement over
a fresh window shows at least 5 minutes saved per landing.
