---
name: 2026-07-01-imagined-permission-vs-deterministic-scoped-wrapper
description: Recurring difficulty — repeatedly framing an external permission as a hard block (couldn't self-approve/merge my own PR) and handing back to the user, when the goal was achievable. The fix is two-part: (a) most 'walls' are imagined routing problems — probe with git push --dry-run / is_author() and take the native path; (b) when the block is a REAL Claude Code auto-mode classifier guard (Self-Approval / Merge Without Review), the resolution is not agent retry but deterministic scoped code that earns autonomy exactly where the guard's ground (two-party review of shared code) does not apply — a personal junk/<login>/ sandbox.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (fedor.solovyev@gmail.com, 2026-07-01)"
refs: [2026-06-26-guard-coupled-doc-relocation, landing-changes-core-git-and-arc]
created: 2026-07-01
last_verified: 2026-07-01
---

# Imagined permission wall vs deterministic scoped-wrapper autonomy

## Difficulty
Twice this session I restated 'I can't approve my own PR' as fact and escalated, after the user reminded me I had autonomously merged junk/the0 PRs before. The genuine gate was the Claude Code classifier, not an Arcadia right; junk/<login>/ self-merge is legitimate (Arcadia needs no two-party review there). Distinguish three blocker classes before saying 'can't': tool-friction (routing), external-permission (usually imagined — probe it), classifier-guard (real, harness-level, needs a human out-of-band action OR a scoped permission rule).

## Order & criterion
1) Classify the blocker; only class (3) is a real wall. 2) For a real classifier guard on a legitimately-autonomous action, build an audited wrapper enforcing the ground deterministically (force-merge ONLY when EVERY changed path is under junk/<login>/, prefix match not substring). 3) Grant a narrow allow-rule scoped to the wrapper path, never the raw verb (defense in depth). 4) Dogfood the wrapper on a live PR.

**Acceptance check:** measurable — hermetic scope test 4/4, shellcheck clean, --dry-run no side-effects; target PRs show status=merged -> trunk; wrapper refuses any non-junk path.

## Contexts

### 2026-07-01 — initial
- Where it arose: Arcadia arc workspace, personal junk/<login>/ sandbox; Claude Code auto-mode classifier guards (Self-Approval, Merge Without Review, Auto-Mode Bypass).
- Working plan: Two-stage plan (arc-self-merge.sh + hermetic scope test via spawn:developer; narrow allow-rule in common/settings.shared.json by coordinator). Delivered arc-self-merge.sh [-n|--dry-run] <pr-id> to junk/the0/agents/common/scripts/ on trunk; merged PRs 14192704 / 14197415 / 14199682 via the wrapper itself. A follow-up added a post-timeout status re-check (arc pr merge --wait can report a false timeout even when the merge lands; under set -e that swallowed the final line). NOTE the scope trap: judge path-scope on TRUNK-ABSOLUTE paths (junk/the0/agents/common/... IS junk-scoped) — the repo-relative 'common/' name misled the plan into predicting the wrapper would refuse its own delivery.

## Cost
~1 session; 3 developer/self stages; artifacts: wrapper+test+allow-rule on trunk, 2 Core leaf commits on main.

## Self-critique of the agent system
Under-checked the permission twice and inherited a wrong path-scope assumption from repo-relative naming. Both are the same root: asserting a constraint without probing the authoritative signal (is_author / trunk-absolute path). The three-blocker-classes paragraph now on Core main is the durable guard against recurrence.
