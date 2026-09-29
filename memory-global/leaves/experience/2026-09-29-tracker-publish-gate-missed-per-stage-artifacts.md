---
name: 2026-09-29-tracker-publish-gate-missed-per-stage-artifacts
description: The tracker plugin's resolution gate required plan/result/status to be published but had no notion of per-stage progress, so a stage that declared output_artifacts could pass silently unjournaled; a third-party ticket reader had to dig a buried figure out of a wall of calculations and publicly criticized the ticket's readability before the gap was even seen.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [https://github.com/sthe0/claude-agent-instructions (commit 8fab7835606e8b5da83ff1fe85637bf1f3e5d446), memory-global/leaves/artifact-complaint-dual-signal.md]
created: 2026-09-29
last_verified: 2026-09-29
---

# Tracker publish gate covered only whole-task phases, missed per-stage artifact-declaring work

## Difficulty
The tracker publish gate (_publish_gate in plugins_tracker.py) only ever checked three whole-task phases — plan, result, status — against bag["published_phases"]. Nothing in the engine required a stage that declared output_artifacts to actually be journaled to the tracker ticket as it happened. A ticket authored end-to-end by the agent reached its reviewer with its load-bearing number (a monthly cost figure) buried inside a paragraph of derivation instead of surfaced per-stage; the reviewer needed three comments to find it and publicly questioned whether the ticket had been machine-generated without review. This is the artifact-complaint-dual-signal pattern: fix the ticket AND diagnose why the publish/tech-writer gate let a structurally-complete-but-unreadable ticket through — the structural answer was that the gate's SCOPE was too coarse, not that the tech-writer pass itself was skipped.

## Order & criterion
Fix the ticket's readability, then close the gap that let it happen: extend the tracker plugin so every PASSED stage with non-empty output_artifacts (and no ephemeral_artifacts_waiver) owes a corresponding progress:<stage index> publication before resolution, and tighten the tracker-management SKILL.md guidance so a progress entry must carry real figures/caveats/artifacts, not a one-liner, and 'file path' is no longer an acceptable artifact type.

**Acceptance check:** measurable

## Contexts

### 2026-09-29 — initial
- Where it arose: agentctl engine (scripts/agentctl/plugins_tracker.py, cli.py), an internal tracker ticket for a quota request, a self-improvement thread inside a larger project task
- Working plan: Folded a new _journal_gate (pure membership test against bag['published_phases'] for progress:<n> per PASSED artifact-declaring stage) into the existing _publish_gate as one _resolution_gate function, since Plugin.gates allows only one callable per core-gate name; added --stage to cli.py's plugin-record subcommand so multiple per-stage progress entries don't collide (composite bag key phase:stage); rewrote tracker-management SKILL.md's progress-entry guidance to require substantive entries and drop 'file path' as an acceptable artifact type.

## Cost
No dedicated `agentctl` session tracked this — it ran in-thread under Auto Mode as a self-improvement beat off a third-party ticket complaint, so there is no per-spawn ledger row to cite. Rough shape: no specialist spawns; two full-suite background pytest runs (~625s and ~624s wall-clock) to catch and then confirm-fixed a 3-test regression; one `verify-cross-refs` failure self-caught and fixed before commit; one `land-on-main.sh` staged-vs-committed mismatch self-diagnosed and worked around. Under this session's flat-rate subscription a dollar figure would be list-price telemetry, not marginal money, so no dollar figure is recorded here — the real costs were wall-clock (~25 min across the two suite runs) and one landed commit to shared trunk.

## Self-critique of the agent system
3 tests in test_resolve_solved_marker.py initially broke because the shared _drive_to_resolved fixture drove a two-stage plan through resolution without discharging the new per-stage journal obligation — a reminder that any fixture exercising the full resolution path needs updating in lockstep with a new resolution-gate axis, not just the tests that target that axis directly.
