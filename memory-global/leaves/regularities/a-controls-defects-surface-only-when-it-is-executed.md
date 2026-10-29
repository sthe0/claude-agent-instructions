---
name: a-controls-defects-surface-only-when-it-is-executed
description: A control (verify_command, final_check, CI job, lint rule) is code; its defects — a wrong argument order, a path that never exists, a check narrower than its claim — stay invisible on reading and surface at its first real execution.
type: reference
schema: regularity/v1
generality: 2
induced_from: [2026-07-09-gate-must-execute-what-it-attests, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan, 2026-07-20-stage-verify-command-narrower-than-final-check, 2026-07-01-monitor-self-defeats-verify-its-own-run, 2026-09-03-hygiene-sweep-ownership-check-argument-order-bug, 2026-10-12-stage-control-authoring-interface-mismatch]
created: 2026-10-27
last_verified: 2026-10-27
---
# A control (verify_command, final_check, CI job, lint rule) is code

## Regularity

A control — a `verify_command`, a `final_check`, a CI job definition, a lint rule, a generated checklist — is code, and its defects behave like those of any code: a swapped argument order, a path that is never produced, an interface its author misremembered, a scope narrower than the claim it backs. Reading the control, however carefully, does not surface them; they surface at its first execution against the real environment. When that first execution is the verdict on the work, the control's defect is reported as the work's.

## Generality

Level 2 — ranges over any artefact that will issue verdicts on other work. It is not claimed for a control generated mechanically from an already-executed template whose generator is itself tested.

## Refutation

A run of controls that were reviewed but not executed before use and whose first executions surfaced no defect of the control itself would show that for that kind of control reading suffices.

## Induced from

- [[2026-07-09-gate-must-execute-what-it-attests]] — the same fault one level in: a gate that
  attests to a property it never executed the check for.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the vector catalogue of
  controls that are wrong in venue, scope, interface, suite or lifetime. Every vector in it is a
  defect that one read-only execution before `submit_plan` would have surfaced.
- [[2026-07-20-stage-verify-command-narrower-than-final-check]] — a control whose text looked right
  and whose executed scope was narrower than the final check it was supposed to anticipate.
