---
name: a-stage-bundling-several-independent-units-of-work-costs-more-than-any-single-cost-tier-prices
description: A stage bundling an empirical measurement or several independent engineering slices costs more than any single cost tier prices
type: reference
schema: regularity/v1
generality: 1
induced_from: [plan-cost-tier-empirical-stage-underestimate]
rule_derived: "lifted from plan-cost-tier-empirical-stage-underestimate.md (Difficulty and both risk classes); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-stage-bundling-several-independent-units-of-work-costs-more-than-any-single-cost-tier-prices

## Regularity

A stage whose material is a real empirical measurement (a multi-variant campaign run against a live or in-process harness, waiting on results, clearing its own acceptance-judge or plan-review rounds) costs an order of magnitude more active wall-clock than a stage that edits code and runs a fast unit test, though both look like one plan step. A stage that bundles several independent engineering slices across files and subsystems, plus a from-scratch cross-cutting test suite, is several stages under one cost-tier label: the tier ladder is calibrated for one coherent unit of work, a larger label buys a larger budget window but does not compress the work, and the slice scoped first consumes the window. Priced at the ordinary tier, either stage manufactures a spurious effort-divergence firing once the real number lands.

## Generality

Level 1 — ranges over plan stages priced by a cost tier that bundle either an empirical measurement campaign or several independent engineering slices with a cross-cutting test suite. It is deliberately not claimed for stages that are one coherent unit of ordinary code work, which the tier ladder is calibrated for.

## Induced from

- [[plan-cost-tier-empirical-stage-underestimate]] — contributes the observed cases of an empirical measurement stage priced medium and a multi-slice engine rewrite whose tier bump did not compress the work, each producing a spurious effort-divergence firing.

## Refutation

Empirical-measurement stages priced medium found, in a sample of plans, to finish within the medium-tier estimate; or multi-slice stages tiered large found to complete in one dispatch as often as single-slice large stages.
