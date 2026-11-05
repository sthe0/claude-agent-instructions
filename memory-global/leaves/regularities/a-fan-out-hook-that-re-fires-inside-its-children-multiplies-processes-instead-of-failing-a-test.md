---
name: a-fan-out-hook-that-re-fires-inside-its-children-multiplies-processes-instead-of-failing-a-test
description: A process-spawning hook that evaluates true again in each child multiplies processes each generation and exhausts a no-swap host in seconds rather than failing an assertion
type: reference
schema: regularity/v1
generality: 1
induced_from: [fanout-trial-under-cap]
rule_derived: "lifted from fanout-trial-under-cap (the 2026-10-07 xdist auto-enable incident); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-fan-out-hook-that-re-fires-inside-its-children-multiplies-processes-instead-of-failing-a-test

## Regularity

When a mechanism spawns processes per invocation and its enabling hook evaluates true again inside each spawned child, the process count multiplies each generation (12, then 144, then 1728 in one observed run) instead of producing a failing assertion, and on a host without swap memory is exhausted within seconds, so a wrong guard costs the machine rather than the trial.

## Generality

Level 1 — ranges over mechanisms that spawn processes per invocation, such as test parallelizers, worker pools and recursive hooks, whose enabling condition is visible inside the spawned child. It is deliberately not claimed for fan-out whose hook exits early in a child, nor for hosts with swap or enforced process limits.

## Induced from

- [[fanout-trial-under-cap]] — contributes the observed case behind the statement: an uncapped trial of a test-parallelizer auto-enable whose workers re-fired the hook (12, 144, 1728 processes) and hung a no-swap VM for about two hours.

## Refutation

A fan-out mechanism whose hook re-fires in its children and which nonetheless yields a bounded process count, or surfaces as a failing test, on a host without swap.
