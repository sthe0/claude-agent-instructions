---
name: a-defect-seen-in-an-artifact-is-fixed-at-its-writer
description: When a file or value is wrong, find what writes it before editing it: a generated or emitted artifact is rewritten by its generator, and the same defect reaches every other consumer of that generator.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:a-generated-artifact-is-overwritten-by-its-writer]
created: 2026-10-21
last_verified: 2026-10-21
---

# A defect seen in an artifact is fixed at its writer

## Principle

Because a generated artifact is overwritten by its writer ([[a-generated-artifact-is-overwritten-by-its-writer]]), for the goal of a fix that stays fixed, on finding a defect in a config file, settings value or emitted field, first locate the writer (generator, installer, emitter) and decide whether the defect lives there; fix the writer and, where the writer is additive or lossy, make it prune or pin the invariant, then add a guard so the next edit cannot reintroduce the drift. A hand edit of the artifact is only a stopgap and is named as one.

## Goal class

A defect found in a written artifact that stays fixed across later runs of the artifact's writer.

## Generality

Level 2 - a rule for any artifact that a program produces, whether config, settings, trailers or generated code.

## Grounded in

- [[a-generated-artifact-is-overwritten-by-its-writer]] — A config file, settings value or emitted field that a generator, installer or emitter writes is rewritten by that writer on its next run, so a hand edit of the artifact is undone or, with an additive writer, kept alongside the drift it was meant to remove.

## Refutation

The principle fails its goal if a defect fixed at its writer, with a guard added, reappears in the artifact after later runs of the writer.
