---
name: feedback-no-hard-caps-on-memory
description: User preference — do not propose hard line-count ceilings on memory files (MEMORY.md indices or leaves). Memory is meant to accumulate; pruning is curation, not a linter job.
type: feedback
---

# No hard caps on memory file sizes

When proposing process-as-code enforcement (`lint-prose-length.py` ceilings, new verifiers), **exclude memory files** — `memory-global/MEMORY.md`, project `<cwd>/.claude/agent-memory/MEMORY.md`, auto-memory `~/.claude/projects/<hash>/memory/MEMORY.md`, and all `leaves/*.md`.

**Why:** Memory is the durable accumulation surface — leaves of experience, runbooks, references. Hard line ceilings would force pruning that deletes useful pointers. The user explicitly rejected a proposed `memory-md-max-lines: 200` ceiling on 2026-05-26 with "не надо жестко ограничивать память, память пусть растёт". The truncation cliff at ~200 lines for `MEMORY.md` (noted in the file itself) is a *signal* for the agent to curate, not a gate for a linter.

**How to apply:**

- In self-improvement proposals, when classifying a rule as "process / could be code", check if the target is a memory file. If yes — do not propose a size ceiling. Prose conventions, frontmatter shape checks, dead-pointer scans are still acceptable.
- Distinguish: CLAUDE.md / cursor mirror / SKILL.md / policy.md are **instruction surfaces** loaded into every session prompt — hard ceilings there protect the token budget. Memory files are **content stores** — different category.
- If the user reverses this preference for a specific scope (e.g. "MEMORY.md indices may need a cap because of truncation"), record the carve-out and adjust.
