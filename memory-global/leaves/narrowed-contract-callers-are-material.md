---
name: narrowed-contract-callers-are-material
description: a plan stage that narrows an existing contract (new required argument, new refusal) lists every caller found by a repo-wide grep as material, and every commit-hook regeneration command as a grant
type: feedback
created: 2026-10-10
last_verified: 2026-10-10
---

## Difficulty

A stage whose change makes an existing call stop working — a newly required argument, a new refusal, a removed default — breaks every caller of that contract, not only the files the order names. When the plan derives the stage's material from the order's own surface, the executor finishes the change, then meets the broken callers outside its edit grants and cannot commit. The same happens with commit-time regeneration a hook demands (a generated registry that "must not be hand-edited"): if the regeneration command is not in the stage's grants, the commit is refused after all the work is done.

Concrete instance: a stage made `critique --failure-address нормативное` require `--norm-target`. The plan listed four test suites as material; a grep found seventeen suites calling the old form, and three more pin suites enumerating stage fields. The pre-commit hook also required `scripts/gen_crutch_registry.py` to register the new gate functions. The executor stopped with a permission request; the stage went to DIAGNOSING and a replan.

## Guidance

- When a stage narrows a contract, grep the repository for the contract's call form while planning and put every hit into the stage's `material_refs` and `output_artifacts`, with one sentence on what each caller needs.
- A stage that adds a new stage/plan field also names the suites that pin the field set (contract-coverage style tests).
- Read the repository's commit hooks once while planning a code stage; any generator a hook requires goes into the stage's `allow` grants.

## See also

- [[verify-command-scope-and-frozen-revision]] — sibling authoring norm on what a stage's check may claim.
- [[plan-control-criterion-hygiene]] — control-criterion authoring norms.
