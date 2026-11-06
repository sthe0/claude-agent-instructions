---
name: a-memory-leaf-describing-mutable-state-ages-while-its-live-source-moves
description: A memory leaf describing mutable state records it at writing time while the live source keeps moving, so presenting the leaf as current reports a stale state
type: reference
schema: regularity/v1
generality: 1
induced_from: [memory-usage]
rule_derived: "lifted from memory-usage.md (Difficulty and Guidance sections); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-memory-leaf-describing-mutable-state-ages-while-its-live-source-moves

## Regularity

A memory leaf that describes mutable state (a pull request or ticket status, the contents of a working tree, work marked pending or in progress, a session checkpoint) records that state at the moment of writing, while the live source (version control, the pull-request API) keeps changing. The leaf and the source diverge, and a reader who presents the leaf as current reports a state that no longer holds. The same holds for a path, a function name, a flag or a version-dependent claim recalled from memory: the leaf carries no mark of what it was checked against, so a stale claim reads as ground truth and costs diagnosis time.

## Generality

Level 1 — ranges over memory leaves that describe mutable state, or recall paths, names, flags and version-dependent claims, whose live source keeps changing after the leaf is written. It is deliberately not claimed for leaves recording durable facts that do not change with the live source.

## Induced from

- [[memory-usage]] — contributes the hygiene practice behind the statement: reconcile a mutable-state leaf against its live source before presenting it as current, and cite a source line for version-dependent claims.

## Refutation

A set of leaves describing mutable state, left unreconciled for weeks, found to match their live source in every sampled case; or version-dependent claims without a source line found to be no more often stale than those with one.
