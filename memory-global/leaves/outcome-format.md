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

**(3)** artifacts — paths, links, commands, with the **clickable URL to the actual run/graph itself** (never a truncated id fragment) for any external job / PR / CI, in status reports and user-facing comments alike — **an artifact/output-folder or dataset link does not substitute for the run link**: when a job produces both, give the run/graph URL first and label the folder distinctly (naming an output folder "the graph" is the exact miss this clause removes); and present every URL as a **clickable link in the form the reader's client actually renders**: in a markdown-rendering client (web / desktop app) a markdown link (descriptive label in `[…]`, URL in `(…)`); in a terminal that linkifies bare URLs but **not** markdown syntax (iTerm2 and many TUIs — there a bracketed-label link, square brackets around the text with the URL in parentheses, shows as literal text and does not click), a **bare URL on its own line**. Match the reader's environment rather than defaulting to markdown; never wrap a URL in backticks in either case (inline-code strips the link affordance);

**(4)** next steps **only if not done** — when done and accepted, **stop** (no teeing up the next roadmap phase, no restating a pointer that already lives in its canonical place);

**(5)** presentation — any user-facing report / tracker comment / message: order **main-first** (headline result → method → detail; reproducibility / SQL / paths last); give a numeric estimate as a **confidence interval** in compact `mean ± error` form (point value never the headline), and **apply the form silently** — write the number, don't narrate the presentation instruction into the text; **highlight** load-bearing figures with **bold** or a colored callout — not emoji (they hinder readability).

## See also

- `~/.claude-agent/CLAUDE.md` § On task resolution § Outcome format — the short pointer that loads this leaf.
