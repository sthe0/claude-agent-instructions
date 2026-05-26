---
name: decomposition-markers
description: Markers M1-M4 for deciding whether to split a substantive task into multiple PRs/tickets. Apply after a plan exists, before starting implementation.
type: reference
---

# Decomposition markers (M1–M4)

A separate question from § Classify task weight in CLAUDE.md. Weight class decides routing (chat / small / substantive). **These markers decide whether a substantive task should ship as one PR or several.**

Apply after the plan is approved, **before** spawning the developer. Adapted from `<arcadia>/ai/artifacts/skills/gena/gena-decompose` — but the framework is repo-agnostic.

## Markers (evaluate top-down)

1. **M1 — Independence.** Can a group of steps / files / contracts be carved out into a PR with **standalone value** — i.e. after that PR merges, the system stays working and the remaining work can continue independently? Without M1, decomposition usually creates churn instead of saving it.
2. **M2 — Heterogeneity.** Does the task mix layers (DB / service / frontend / infra), expertises, or preparatory refactor + new feature in the same change? Each homogeneous slice is easier to review.
3. **M3 — Blocking dependencies.** Is part of the work waiting on an external decision / someone else's PR / another ticket, while another part is unblocked? Split off the unblocked half.
4. **M4 — Rollback risk.** Does the change touch critical surface (migrations, auth, billing, public API) where small steps with separate verification are safer to revert?

Volume alone is not a reason. A 2000-line uniform refactor with no M1 still ships as one PR.

## Verdict (one line)

- **recommended** — M1 holds **and** at least one of M2–M4 has a clear signal; **or** M3/M4 alone are severe enough to require a separate PR.
- **possible** — M1 partial or weak, but M2–M4 give a reason to consider splitting; leave the choice to the user.
- **not required** — M1 doesn't hold and M2–M4 don't push for it.

## Where the verdict goes

If the plan was written to a file (e.g. `~/.claude/plans/<slug>.md`), add a `## Decomposition` section: verdict line, 1–2-sentence rationale citing the markers that fired, and — if recommended — a numbered list of sub-PRs. Each sub-PR: imperative title, 1-line description (what + scope boundary), dependency note (`after #1` / `parallel with #2`) when material.

If the plan is in-conversation only, surface the verdict and reasoning in the manager's reply before the user-approval gate. The user decides whether to split.

## See also

- `~/.claude/CLAUDE.md` § Classify task weight — chat / small / substantive routing (orthogonal axis).
- `<arcadia>/ai/artifacts/skills/gena/gena-decompose/SKILL.md` — the upstream skill this is adapted from.
