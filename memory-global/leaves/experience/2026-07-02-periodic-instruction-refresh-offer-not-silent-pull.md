---
name: 2026-07-02-periodic-instruction-refresh-offer-not-silent-pull
description: Delivering 'once a day both Core and project layers should offer to pull fresh instructions' — the naive build is a background timer/cron that auto-pulls silently; that is what silently rebased THIS session's tree mid-task and re-triggered a stash-pop conflict. The correct shape is an agent-driven OFFER: a UserPromptSubmit throttle hook (stdout -> enters model context, unlike SessionStart whose stderr only reaches the user) that, once per calendar day, does a fail-open behind-check and prints a nudge instructing the agent to offer the pull via AskUserQuestion — never pull itself. Also: only surface the offer when the layer is actually behind upstream.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev"
refs: [scripts/hook-instructions-refresh-due.py, scripts/tests/test_hook_instructions_refresh.py, scripts/install-reminder-hooks.sh, docs/architecture/instruction-layering.md]
plan_file: /home/the0/.claude-agent/plans/daily-instruction-refresh-prompt.toml
created: 2026-07-02
last_verified: 2026-07-02
---

# Periodic instruction refresh: agent OFFER via UserPromptSubmit hook, not silent background pull

## Difficulty
A recurring 'periodically do X' requirement invites a silent background automator (cron/systemd timer/auto-pull), but silent state-mutation of a shared working tree collides with in-flight sessions (mid-task rebase/stash-pop conflicts, exactly what bit this task twice). The second trap is hook-channel choice: an offer must reach the AGENT (to gate through AskUserQuestion), so it must ride UserPromptSubmit (stdout -> turn context); SessionStart stderr only reaches the human and cannot drive an agent action. Third: an unconditional daily nudge is noise — gate on an actual behind-count so silence == up-to-date.

## Order & criterion
1) replace the silent 10-min auto-pull timer with an explicit offer (delete/deprecate the cron+systemd installers); 2) author UserPromptSubmit hook: once-per-day stamp throttle, fail-open git fetch + rev-list --count behind-check on Core (origin/main) and the project's own tracked .claude layer (@{upstream}); 3) emit nudge only when behind, instructing OFFER-via-AskUserQuestion; 4) register in install-reminder-hooks.sh, add hermetic pytest (local git remotes, no network).

**Acceptance check:** measurable: 7 hermetic pytest cases green (behind->nudge, throttled->silent, up-to-date->silent, non-git->fail-open, project-layer nudge, project-without-.claude ignored, stamp-gates-second-run); install dry-run materializes the tuple with timeout 10; all 14 pre-commit checks green.

## Contexts

### 2026-07-02 — agent-driven periodic offer over silent background automation
- Where it arose: any 'do X every N days' agent requirement where X mutates shared state or needs agent judgement — choose an OFFER (UserPromptSubmit stdout nudge -> AskUserQuestion) over a silent automator; and any hook that must drive an agent action must use stdout-channel events, not SessionStart stderr.
- Working plan: planner-approved 4-stage TOML plan (daily-instruction-refresh-prompt.toml); developer spawned for stage 1 (hook), stages 2-4 in-thread (tests, registration, docs); verify-final green; land: commit 9ee4d4b + push to origin/main.

## Cost
1.89 USD; one developer spawn (medium, 1.33 USD) for the hook, rest in-thread

## Self-critique of the agent system
Nearly built the redundant silent timer before surfacing that a 10-min auto-pull already existed; caught it by inspecting install-sync-* before adding a parallel mechanism. Lesson generalizes: before adding a 'periodic' mechanism, grep for an existing one and prefer replacing over stacking.
