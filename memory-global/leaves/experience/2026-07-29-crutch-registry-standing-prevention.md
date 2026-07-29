---
name: 2026-07-29-crutch-registry-standing-prevention
description: The regex-crutch principle was named once and fixed at three sites (2026-07-22, a fourth already judge-backed), then recurred anyway because naming a crutch does not prevent a new one from landing; replaced hand-audit with a mechanical two-domain enumerator (regex-driven hard-outcome code sites widened past the three-contract boundary, plus a never-before-audited prose-crutch domain of decidable rules left as prose), a generated classification registry, and a standing verifier wired into verify-all.py plus an advisory self-diagnose scan.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "the user (fedor.solovyev) — confirmed at the resolution gate 2026-07-29; approved the fast-forward land to public trunk, accepted the agent-proposed 4/5 quality"
refs: [memory-global/leaves/regex-not-for-semantic-classification.md, docs/operations/crutch-registry.md, scripts/crutch-inventory.py, scripts/crutch_registry.toml, scripts/gen_crutch_registry.py, scripts/verify-semantic-gates.py, scripts/self-diagnose.py]
plan_file: /home/the0/.claude-agent/plans/anti-crutch-audit-and-registry.toml
created: 2026-07-29
last_verified: 2026-07-29
---

# Standing prevention for regex-crutch and prose-crutch recurrence: mechanical enumeration + generated registry + re-check

## Difficulty
A design principle documented once in a memory leaf (regex classifying free-text meaning to drive a hard block) does not prevent recurrence: the fix that landed first for the ORIGINAL difficulty was itself a semantic regex, and a hand-built audit table closed with a universal claim ("no further semantic hard-block exists") that its own bounded domain (three enforcement contracts: PreToolUse-deny, Stop-block, exit(2)) could not support, because a regex driving a hard BEHAVIOUR rather than one of those three contracts is invisible to a grep built around them. A second, symmetric anti-pattern (a deterministically-decidable rule left as instruction prose instead of mechanized) had no enumeration at all. Both are instances of the same root failure: a rule prose-documents a boundary but nothing re-checks the boundary on every future change, so a new violation lands silently.

## Order & criterion
1 build a zero-classification enumerator over both domains (crutch-inventory.py: AST walk for code sites, modal-keyword heading-scoped extraction for prose sites) -> 2 classify every enumerated site as generated data (gen_crutch_registry.py -> crutch_registry.toml: partition table + named per-id overrides, reproducible byte-identical on re-run) -> 3 wire a standing verifier (verify-semantic-gates.py) into verify-all.py that fails on an unregistered site, a stale entry, or a silently-reverted judge guard -> 4 add an advisory self-diagnose arm (scan_crutch_regressions) flagging overdue defer entries -> 5 publish the scope (this stage): rewrite the frozen-table leaf to point at the live registry, document the mechanism, publish the exact deferral list, state honest limits, record this leaf

**Acceptance check:** measurable: python3 scripts/crutch-inventory.py --check and python3 scripts/verify-semantic-gates.py both exit 0 against the live tree (0 semantic-unguarded sites, 0 unregistered sites); python3 scripts/verify-leaf-structure.py, scripts/lint-prose-length.py, and scripts/check-org-neutral.py green on the touched leaf

## Contexts

### 2026-07-29 — initial
- Where it arose: Extending the agent's own hooks/scripts in the claude-agent-instructions repo (delivered via the wt-anti-crutch worktree, branch wt-anti-crutch), as a follow-on standing-prevention task after the original semantic-gate fix (2026-07-22) proved that naming the principle once was insufficient
- Working plan: /home/the0/.claude-agent/plans/anti-crutch-audit-and-registry.toml

## Cost
7-stage plan; this stage (5, tech-writer, spawned): leaf rewrite + new docs page + 2 doc-index edits + this experience leaf + self-reference-loop re-check

Shape chosen as the lightest that actually prevents — one script on the existing `verify-all.py` runner + one checked-in registry + one advisory arm in the existing `self-diagnose.py` scan. Rejected as heavier without proportional gain: a pre-commit hook (duplicates verify-all, second install surface), a CI service (this repo's checks are local by design), a new plugin/registry framework (costs more, loaded, than the crutches it prevents). Recorded so the "lightest form that prevents" choice is auditable rather than assumed.

## Self-critique of the agent system
The original leaf's closing claim was written confidently past what its own grep-based domain could prove — a lesson for every future audit-table leaf: state the enumeration METHOD next to a universal claim, not just the count, so a narrower method is visible at read time rather than only discoverable when someone tries to mechanize it later.
