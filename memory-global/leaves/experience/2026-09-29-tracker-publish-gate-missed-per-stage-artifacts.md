---
name: 2026-09-29-tracker-publish-gate-missed-per-stage-artifacts
description: The tracker plugin's resolution gate required plan/result/status to be published but had no notion of per-stage progress, so a stage that declared output_artifacts could pass silently unjournaled; a third-party ticket reader had to dig a buried figure out of a wall of calculations and publicly criticized the ticket's readability before the gap was even seen.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [https://github.com/sthe0/claude-agent-instructions (commit 8fab7835606e8b5da83ff1fe85637bf1f3e5d446), memory-global/leaves/artifact-complaint-dual-signal.md]
created: 2026-09-29
last_verified: 2026-10-01
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


### 2026-10-01 — replan-journal fold + internal-heading prohibition (recurrence on a sibling project ticket)
- Where it arose: agentctl engine (scripts/agentctl/plugins_tracker.py), a project ticket thread, same self-improvement cycle class as the 2026-09-29 context but on the publish_replan directive instead of the per-stage journal gate
- Working plan: the user flagged for a second time on the same ticket thread that its comments read as a bureaucratic log of internal plan-corrections and stage-transitions, after I had already fixed the artifact itself (consolidated 6 process-journaling comments into one results-focused comment). Diagnosis: publish_replan fired an unconditional "post what changed" directive on EVERY replan regardless of the engine's own kind classification (no_change/refinement/substantive) — so an internal plan correction with no reader-visible scope change still got drafted into a standalone ticket comment, and tech-writer rule 12 (fold bookkeeping into the next substantive entry) existed as prose the composing pass had to remember to apply rather than something the gate itself enforced. Same root shape as the 2026-09-29 context: a publish gate structurally present but scoped too coarsely to the content class that actually causes the complaint. Fix: branch _observe_replan on _last_replan_kind(state) — refinement/no_change now return a fold directive (record the skip via plugin-record --phase replan --skipped, fold one line into the next substantive entry) instead of a standalone-post directive; substantive keeps the original directive. Also hardened the substantive-progress directive text itself with an explicit prohibition on internal stage-number headings (a literal "### Stage N" pattern), since the fact-based publish gate proves a tech-writer pass ran, not that it caught this specific content defect. Added 4 new tests (39/39 passing) covering both branches plus the no-history fallback. Landed to origin/main as d04d2454 via land-on-main.sh after explicit push confirmation.

## Common core & variations
**Common:** Both contexts share one functional ground: a tracker-publish gate verified a FACT about the pipeline (a phase got published / a tech-writer pass ran) instead of a PROPERTY of the content (does this artifact carry reader value, did the writer actually catch this content class) — so the gate was satisfiable by a structurally-complete-but-wrong post. The durable fix pattern is to move the branching decision upstream into the directive-selection logic itself (kind-aware directive construction) rather than trusting a downstream prose rule to be remembered at composition time.

**Variations:** 2026-09-29: the gap was an entire missing obligation (no per-stage journal requirement existed at all) on the STATUS/RESULT axis. 2026-10-01: the obligation existed (publish_replan always fired) but was undifferentiated on the REPLAN axis — it could not tell a reader-irrelevant internal correction from a reader-relevant plan change, so every replan was treated as the latter.

## Cost
No dedicated `agentctl` session tracked this — it ran in-thread under Auto Mode as a self-improvement beat off a third-party ticket complaint, so there is no per-spawn ledger row to cite. Rough shape: no specialist spawns; two full-suite background pytest runs (~625s and ~624s wall-clock) to catch and then confirm-fixed a 3-test regression; one `verify-cross-refs` failure self-caught and fixed before commit; one `land-on-main.sh` staged-vs-committed mismatch self-diagnosed and worked around. Under this session's flat-rate subscription a dollar figure would be list-price telemetry, not marginal money, so no dollar figure is recorded here — the real costs were wall-clock (~25 min across the two suite runs) and one landed commit to shared trunk.

**2026-10-01 context:** no specialist spawns; the two-beat self-improvement gate (diagnosis `AskUserQuestion` → apply → a separate push-confirmation `AskUserQuestion`) ran entirely in-thread. Added 4 targeted tests (verified via the narrow test file, 39/39) rather than blocking on the full ~17-minute suite; the full suite completed later via a backgrounded task notification (7201 passed, 1 failed, 4 skipped) with the one failure self-diagnosed as caused by this session's own concurrent `git add`/`commit` activity in the same worktree (a `WRITE-SURFACE` git-porcelain-cleanliness assertion), not a regression. One `git reset --soft HEAD~1` self-correction to re-stage an already-pushed commit for `land-on-main.sh` (which lands a staged diff, not a commit). No dollar figure for the same flat-rate reason as the 2026-09-29 context.

## Self-critique of the agent system
3 tests in test_resolve_solved_marker.py initially broke because the shared _drive_to_resolved fixture drove a two-stage plan through resolution without discharging the new per-stage journal obligation — a reminder that any fixture exercising the full resolution path needs updating in lockstep with a new resolution-gate axis, not just the tests that target that axis directly.

**2026-10-01 context:** the same root cause recurred on a *different* gate axis within three days — the 2026-09-29 fix covered the per-stage progress obligation, but `publish_replan` (a sibling directive in the same plugin) still trusted downstream prose (tech-writer rule 12) to catch exactly the same bureaucratic-entry class, because nobody had generalized "verify content-class, not pipeline-fact" from the one gate it was first applied to, to the plugin's other directive-emitting observers. The standing prune question for this plugin: does `_observe_record_result`'s non-journaled branches, or any other directive emitter in `plugins_tracker.py`, carry the same undifferentiated-by-content-class gap this leaf's two contexts both found independently?
