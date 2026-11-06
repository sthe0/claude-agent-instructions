---
name: a-single-tracker-holding-both-backlog-and-reports-keeps-them-apart-only-by-a-label-the-digest-filters-on
description: Where one tracker holds both backlog and reports, only a label keeps them apart; a digest filtering on that label swallows or misses mislabelled items
type: reference
schema: regularity/v1
generality: 1
induced_from: [instruction-dev-queues]
rule_derived: "lifted from instruction-dev-queues.md (tier table and stream-separation section); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-single-tracker-holding-both-backlog-and-reports-keeps-them-apart-only-by-a-label-the-digest-filters-on

## Regularity

Instruction development has three tiers (Core, Org, Project), each with a proactive internal backlog of what the tier's editors plan and a reactive report inbox of difficulties filed by people without edit authority. When the filers of a tier are also its editors the two flows have one population and merge into one queue (the Project tier). Where a tier has a tracker per flow (Org: ORG-BACKLOG and ORG-REPORT) the queues themselves separate the flows; where one tracker holds both (Core, GitHub Issues) only a label separates them: reports carry the difficulty label and backlog items carry the backlog label. core-difficulty-digest.py pulls only issues with labels=difficulty and state=open, so a planning item filed under the difficulty label is swallowed into the digest as if it were a user report, and a report filed without it is invisible to the digest.

## Generality

Level 1 — ranges over instruction-development tiers whose planned work and filed reports share one tracker and are told apart by a label that a downstream digest filters on. It is deliberately not claimed for tiers that keep a separate queue per flow, where the queues themselves keep the flows apart.

## Induced from

- [[instruction-dev-queues]] — contributes the tier table and stream-separation section: two trackers separate the flows by queue, one tracker only by label, and the digest pulls only the difficulty label.

## Refutation

A planning item filed in the Core tracker under the difficulty label is observed to stay out of core-difficulty-digest.py output, or a report filed without the difficulty label is observed to appear in it; or the Project tier is found to keep a separate inbox for non-editor filers.
