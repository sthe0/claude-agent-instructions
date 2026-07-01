---
name: 2026-07-01-additive-settings-merge-cannot-prune-install-time-invariant
description: An install-time settings value whose correct state is 'deprecated key must be absent' or 'must override a stale live value' cannot be fixed by an additive merge (live-wins / add-if-absent) — the installer must ACTIVELY prune (del) + pin (base-wins); and the invariant needs a lint guard on the shared base + a pytest driving the real installer against a dirty fixture, or the next well-meaning edit silently reintroduces the drift.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com (AskUserQuestion: Да, решена)"
refs: [scripts/apply-settings.sh, scripts/set-context-cap.sh, scripts/lint-settings-base.py, scripts/tests/test_apply_settings_autocompact.py, memory-global/leaves/autocompact-threshold-policy.md]
created: 2026-07-01
last_verified: 2026-07-01
---

# Making a config value a structural install-time invariant when the installer merge is additive

## Difficulty
Auto-compaction never fired on this macOS machine (Claude nudged /clear instead) while it worked on another host. Root cause was NOT stale base.json: apply-settings.sh's single jq merge is additive (env = base+live with live winning; autoCompactWindow = live // base), so on an already-provisioned machine it could neither delete the two deprecated env keys (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, CLAUDE_CODE_DISABLE_1M_CONTEXT) nor overwrite a stale/absent autoCompactWindow. Every machine had to be hand-fixed — the opposite of an install-time invariant.

## Order & criterion
Diagnose (additive-merge trap, not stale base) -> fix installer to actively prune+pin (base wins for the autocompact window; unconditional del of the two deprecated keys), reusing the proven recipe from set-context-cap.sh (one recipe, two call sites) rather than a second jq pass -> run the corrected installer on this machine so the local fix rides the installer (no bespoke hand-edit) -> add a structural guard: lint-settings-base.py asserts base.json's autocompact contract (fails verify-all on drift) + a pytest drives the REAL apply-settings.sh against a dirty CLAUDE_SETTINGS fixture (prune+pin+idempotent, machine keys survive).

**Acceptance check:** measurable: live ~/.claude/settings.json converges (window pinned, both deprecated keys absent) through the installer; a dirty fixture converges end-to-end; verify-all 14/14 green incl. the new lint invariant; lint returns non-zero when base.json is mutated to reintroduce a stale key. NOTE the axis boundary: the FILE-state observable is checkable immediately, but the RUNTIME observable (autocompaction actually firing) only manifests on the next session restart because env is read at session start — report that honestly rather than claiming the current session is fixed.

## Contexts

### 2026-07-01 — autocompact install-time invariant (macOS)
- Where it arose: Core repo scripts/apply-settings.sh (invoked by setup-symlinks.sh), scripts/lint-settings-base.py, scripts/tests/; autocompact policy in memory-global/leaves/autocompact-threshold-policy.md
- Working plan: ~/.claude/plans/autocompact-install-time.toml (3 stages: installer prune+pin / apply-on-this-machine / guard+pytest)

## Cost
Small. 3-stage TOML plan (planner spawned once). Implementation: stage 1 (installer jq edit) + stage 2 (live apply on this machine, in-thread under the pure-local live-state carve-out) done in-thread; stage 3 (lint guard + pytest) via one `developer` spawn (~67k sub-agent tokens, opus). Two Core commits (code `ed3036d`, this leaf). The costly part was diagnosis: an initial threshold-arithmetic dead-end before reading `apply-settings.sh` + `set-context-cap.sh` directly surfaced the additive-merge root cause.

## Self-critique of the agent system
First diagnosis sub-agent's arithmetic on the compaction trigger was internally inconsistent; the real root cause (additive merge, not stale base) came only from reading apply-settings.sh + set-context-cap.sh directly. Lesson: for a 'why doesn't this setting take effect' question, read the merge/install path before theorizing about threshold math.
