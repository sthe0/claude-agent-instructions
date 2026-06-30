---
name: 2026-06-30-validator-tightening-newly-enforces-latent-violations
description: Difficulty — you ship a stricter shared validator (linter, schema check, gate) and it immediately flags PRE-EXISTING latent violations in OTHER authors files that the old looser check tolerated. Tightening a shared check newly-enforces the rule across the whole corpus at once: a scoped change becomes an unplanned corpus-wide fix, or breaks CI for unrelated work. Mitigation: run the new check corpus-wide first, then either a fix-neighbours pass or mode-differentiated severity (advisory in full-corpus scan, fatal only at write/commit) to grandfather legacy debt while blocking new debt.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (inherited from parent 2026-06-29-org-portable-core-internal-coupling-opt-in; design re-applied live this session)"
refs: [2026-06-29-org-portable-core-internal-coupling-opt-in.md, 2026-06-26-guard-coupled-doc-relocation.md]
created: 2026-06-30
last_verified: 2026-06-30
---

# Tightening a shared validator newly-enforces latent violations in neighbours artifacts

## Difficulty
A tightened shared validator newly-enforces pre-existing latent violations in adjacent authors artifacts, turning a scoped change into an unplanned corpus-wide fix or a broken CI.

## Order & criterion
Before tightening a shared check: (1) run the new check over the WHOLE corpus to see whom it newly-fails; (2) choose — fix-neighbours pass now, or mode-differentiated severity (advisory in full scan / fatal at write+commit) to grandfather legacy debt while blocking new debt; (3) then land.

**Acceptance check:** Run the tightened check across all existing artifacts before landing: the count of newly-failing legacy items is the unplanned scope it would otherwise add.

## Contexts

### 2026-06-30 — 2026-06-29 — leaf validator newly-enforced latent neighbour defects
- Where it arose: claude-agent-instructions: the just-fixed leaf validator then newly-enforced the WIP authors own latent date-less leaves, adding an unplanned fix-the-neighbours pass
- Working plan: Ran the fix-neighbours pass on the surfaced latent defects; recognized the tightening was not free.

## Cost
~1 unplanned neighbour-validator pass.

## Self-critique of the agent system
Did not run the tightened validator corpus-wide before landing; the newly-failing-legacy count should be measured pre-land.
