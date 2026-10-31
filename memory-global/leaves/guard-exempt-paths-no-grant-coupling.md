---
name: guard-exempt-paths-no-grant-coupling
description: agentctl's guard_exempt_paths stage field lifts the repo-root write-guard deny on a declared path but has no code-level check that the path is covered by an actual validated grant for that stage — a follow-up structural validator is still open.
created: 2026-10-02
last_verified: 2026-10-02
schema: regularity/v1
converted_from: leaf/v1
given: code
given_source: scripts/agentctl/dispatch.py
type: reference
---

## Difficulty

`guard_exempt_paths` (landed `claude-agent-instructions@7b0b5e90`,
`scripts/agentctl/dispatch.py` / `scripts/spawn-specialist.py`, following the
`cost_tier` precedent of an engine-consumed field kept outside the structural
stage signature) lets a plan stage declare a path that the repo-root write-guard
deny (`write_guard_deny_rules`, which otherwise unconditionally blocks `Edit` on
`.claude/**`, `.git/**`, `.git`, and any `settings*.json` reached only via
ambient/implicit write access) should not apply to.

Nothing in the landed code requires that an exempted path actually correspond
to a grant the stage separately declared and had validated (a `[stage.grants]`
entry, a `--rule`/`--add-dir` the resource model checked). The field is a bare
path string; declaring it is sufficient to lift the deny. A stage can therefore
exempt a path its own declared grants never covered, and the write-guard — the
mechanism meant to stop exactly that kind of ambient/implicit access — does not
catch it, because `guard_exempt_paths` sits outside the grant-validation path
entirely.

This was flagged independently twice on the same diff (an internal project's
plan stage, 2026-10-02): an automated code-reviewer nit ("the path guard presumably lives
in `spawn-specialist.py`... a cross-reference would help") and, separately, the
plan-review `thinker` pass for the corresponding plan-TOML correction
(`REVIEW-PASS`, digest `503b9103...4249511`) — the reviewer passed the review
because the declaration still rides the normal plan-review/approval gates like
any other plan field, but named the missing coupling explicitly as a tracked
follow-up, not a blocker.

## Regularity

In agentctl, the stage field guard_exempt_paths lifts the repo-root write-guard deny (write_guard_deny_rules, which otherwise blocks Edit on .claude/**, .git/**, .git and settings*.json reached only through ambient write access) for a declared path, but no code checks that the exempted path is covered by a grant the stage separately declared and had validated (grants.validate_rule, validate_add_dir). A stage can therefore exempt a path its own grants never covered, and only the plan-review pass stands between an unjustified exemption and the write.

## Given

Given from code: `scripts/agentctl/dispatch.py`. The statement is taken as known from that source, not induced from recorded experience.

## Refutation

Reading scripts/agentctl/dispatch.py and scripts/spawn-specialist.py (as of landing in claude-agent-instructions@7b0b5e90 or later) shows a validator that rejects a guard_exempt_paths entry not contained in the union of the stage's own validated Edit-allow grants.

## Guidance

Known, accepted gap — not yet closed. Before relying on `guard_exempt_paths` as
a *safety* mechanism (as opposed to a documented, reviewed exception each time
it's declared), add a structural validator asserting
`guard_exempt_paths ⊆ union(stage's own validated Edit-allow grants)` at the
same point `grants.validate_rule`/`validate_add_dir` already runs. Until that
validator exists, every `guard_exempt_paths` entry depends entirely on the
plan-review human/thinker pass catching an unjustified exemption — there is no
machine backstop.

## See also

- [[redispatch-continuity-check]] — another `agentctl` resource-model gap found
  the same way (empirical observation during a live dispatch, not design
  review).
- `scripts/agentctl/gates.py` — renormalization residual treats
  `guard_exempt_paths` the same as `grants` for the light-renormalization path;
  the missing piece is a *forward* check (declared exemption → actual grant),
  not the residual (grant → renormalization).
