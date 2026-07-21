---
name: code-links-carry-revision
description: A deliverable that cites a code identifier or path via a link pins the link to a concrete revision — the default for both the developer (who writes such a link) and the reviewer (who rejects a line-anchored link that is not revision-pinned); file/dir-level links are drift-proof and exempt
type: feedback
schema: leaf/v1
created: 2026-07-21
last_verified: 2026-07-21
---

## Difficulty

A code link that names a line or an identifier without a pinned revision rots: the branch it points at moves, the lines drift, or the target is deleted, and a future reader following the link lands on the wrong code — or nothing — with no signal that the reference went stale. The link reads as authoritative long after it stopped being true. Nothing mechanically couples the link to the bytes it was written against, so the discipline lives only in prose.

## Guidance

**The rule is symmetric — the author pins, the reviewer checks — and it scopes the hard requirement to line-anchored links.**

- **Author (developer / tech-writer / anyone citing code).** When a deliverable (PR description, ticket comment, report, doc, memory leaf) cites a code identifier or path **via a link**, pin the link to a concrete revision so it resolves to the exact bytes cited. A **line-anchored** link (`…#L120`, a specific line/range) **must** be revision-pinned — that is where drift silently corrupts the reference. A **file- or directory-level** link with no line anchor is drift-proof (the path is stable across edits) and needs no revision, though pinning it is still fine.
- **Reviewer (code-reviewer).** A line-anchored code link with no pinned revision is a **should-fix** finding — ask for the revision rather than approving on the author's say-so. A bare file/dir link is acceptable as-is.

**Org-neutral — the mechanism is a permalink, not a specific host.** Two examples, neither is *the rule*:

- Arcadia: append `?rev=rNNNNNNNN` (e.g. `…/scripts/foo.py?rev=r20395226#L42`).
- GitHub: use a commit-SHA permalink — `…/blob/<40-hex-commit-sha>/path#L42` (the `y`-shortcut in the GitHub UI rewrites a branch URL to this form).

Any VCS host with an immutable revision/commit addressing scheme qualifies; use whichever the deliverable's host provides.

## See also

- [[code-comment-discipline]] — the *in-code* reference rule (don't cite callers/issues in code); this leaf is the *deliverable-facing* reference rule.
- `~/.claude-agent/skills/specializations/tech-writer/SKILL.md` § How you write — the code-reference **formatting-tier** rule (a repo entity with a link target renders as a plain link *without* backticks; revision-pinning here and the link/backtick tiering there are two facets of one code-reference discipline).
- [[tests-accompany-code]] — the same symmetric author-writes / reviewer-rejects shape on the testing axis.
- [[leaf-schema]] — this leaf's `leaf/v1` structure.
