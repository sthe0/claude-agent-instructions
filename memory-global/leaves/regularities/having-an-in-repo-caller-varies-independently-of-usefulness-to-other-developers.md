---
name: having-an-in-repo-caller-varies-independently-of-usefulness-to-other-developers
description: Whether a task-produced file has an in-repo caller does not predict whether other developers find it useful; callerless files can be valuable and called ones noise
type: reference
schema: regularity/v1
generality: 1
induced_from: [committed-files-earn-their-place]
rule_derived: "lifted from committed-files-earn-their-place (Difficulty section); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# having-an-in-repo-caller-varies-independently-of-usefulness-to-other-developers

## Regularity

A task-produced file with no caller in the repository can still be the most useful thing another developer finds, and a file with a caller can still be task-local noise, so the presence of an in-repo caller does not predict whether a file is useful to other developers.

## Generality

Level 1 — ranges over files a task produces and considers committing to a shared product repository, such as helper scripts, data dumps and run logs. It is deliberately not claimed for the build or runtime necessity of a file, where a caller does matter.

## Induced from

- [[committed-files-earn-their-place]] — contributes the observed cases behind the statement: a job-success-assertion script committed with no caller and no usage doc, and run logs and intermediates committed because a pipeline wrote them there by default.

## Refutation

A sample of committed task-produced files in which having an in-repo caller predicted usefulness to other developers with no counterexample in either direction would show the independence false.
