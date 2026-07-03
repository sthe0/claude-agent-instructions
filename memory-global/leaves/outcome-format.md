---
name: outcome-format
description: The five-point shape of a report the user (or the next step) can act on without re-asking — status, what was done by step, artifacts with clickable markdown links, next-steps-only-if-not-done, and main-first presentation with interval estimates.
type: feedback
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-02
---

# Outcome format

The short rule lives in CLAUDE.md § On task resolution; this leaf carries all five points verbatim.

## Difficulty

A point number reads as false precision, unformatted figures vanish in prose, a buried conclusion makes the reader dig, and narrated presentation-wishes are reader noise.

## Guidance

A report the user (or the next step) can act on without re-asking:

**(1)** status — done / in progress / blocked;

**(2)** what was done, by step + who executed;

**(3)** artifacts — paths, links, commands, with the **clickable URL to the actual run** (never a truncated id fragment) for any external job / PR / CI, in status reports and user-facing comments alike — and present every URL / link as a **markdown link** (descriptive label in `[…]`, the URL in `(…)`) — never bare (a bare URL renders as monospace / non-clickable in some clients) and never wrapped in backticks (inline-code formatting strips the link affordance, so it no longer reads or clicks as a link);

**(4)** next steps **only if not done** — when done and accepted, **stop** (no teeing up the next roadmap phase, no restating a pointer that already lives in its canonical place);

**(5)** presentation — any user-facing report / tracker comment / message: order **main-first** (headline result → method → detail; reproducibility / SQL / paths last); give a numeric estimate as a **confidence interval** in compact `mean ± error` form (point value never the headline), and **apply the form silently** — write the number, don't narrate the presentation instruction into the text; **highlight** load-bearing figures with **bold** or a colored callout — not emoji (they hinder readability).

## See also

- `~/.claude-agent/CLAUDE.md` § On task resolution § Outcome format — the short pointer that loads this leaf.
