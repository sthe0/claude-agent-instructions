---
name: a-report-reaches-its-reader-as-their-client-renders-it-and-in-the-order-written
description: A report reaches its reader through the reader's client and in the order written, not as the writer sees it
type: reference
schema: regularity/v1
generality: 1
induced_from: [outcome-format]
rule_derived: "lifted from outcome-format.md (Difficulty and points 3 and 5); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-report-reaches-its-reader-as-their-client-renders-it-and-in-the-order-written

## Regularity

A report reaches its reader through the reader's own client and in the order the text is written, not as the writer sees it. A local filesystem path exists only on the writer's machine; a markdown link shows as literal text in a terminal that linkifies only bare URLs; a URL inside inline code loses its link affordance; an artifact or output-folder link stands in the place of the run link when both exist. A point estimate reads as false precision, an unformatted figure vanishes in prose, a conclusion placed after the method forces the reader to dig, and a presentation wish narrated into the text is noise.

## Generality

Level 1 — ranges over written reports, status comments and messages read by a user or a next step through a client the writer does not control. It is deliberately not claimed for text that stays inside the writer's own session and filesystem.

## Induced from

- [[outcome-format]] — contributes the reporting practice behind the statement: links in the form the reader's client renders, no local paths for external readers, intervals instead of point estimates, and main-first order.

## Refutation

A reader in a bare-URL terminal found to click a markdown-syntax link, or a reader outside the writer's machine found to open a local path; or readers found to locate a conclusion placed after the method as quickly as one placed first.
