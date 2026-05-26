---
name: plan-verify-loop
description: Hardened the plan-and-verify loop — planner now writes plan to a markdown file; each stage carries an Expected result image; the Coordination cycle Verification gate runs at every stage and again as a final end-to-end check. Code-enforced via verify-plan-file.py wired into spawn-specialist.py.
type: reference
resolution_confirmed_by_user: "Да, резолвнута"
---

# Plan-verify loop — 2026-05-26

## Final plan as executed

| # | Commit | Pack | What |
|---|---|---|---|
| 1 | `cc00469` | All six | `verify-plan-file.py` validates plan structure (4 required sections + ≥ 1 `Expected result image:` line); `spawn-specialist.py` runs it after a planner `PLAN-READY:` and tags `MALFORMED:` on rejection; `planner/SKILL.md` Plan format gains `Expected result image:` per stage and a top-level `## Final verification` section; `PLAN-READY:` marker requires a `Plan: <absolute-path>` line and the file written to `~/.claude/plans/<slug>.md`; CLAUDE.md § Coordination cycle § Verification rewritten as two mandatory layers (per-stage against image + final against done criterion); `coordinator-pitfalls.md` row for "advanced without verification". |

Pushed `57c56e9..cc00469` to `origin/main`.

## Difficulties

- **Initial commit failed silently — `[self-improvement-reviewed]` marker missing.** I touched `skills/self-improvement/policy.md` (added the new script to the layout tree) and the commit-msg hook blocked the commit with the long marker-required message. I had to re-issue the commit with the marker added. **Lesson:** when staging includes anything under `skills/self-improvement/`, append the marker before running `git commit`, not after the rejection. Should be a pre-commit reminder in the agent prompt — currently it's recall.

- **`scripts/__pycache__/` left behind by inline unit test.** I imported `spawn-specialist.py` via `importlib.util` to unit-test `validate_planner_plan()` without spawning. Python wrote `.pyc` files into `scripts/__pycache__/` which then appeared as untracked in `git status`. Fixed by adding `__pycache__/` and `*.pyc` to `.gitignore` in the same commit. Worth keeping the `.gitignore` entry as a permanent guard against this pattern.

## Artifacts

- Commit: `cc00469` on `origin/main`. 8 files changed, +171 / −5.
- New: `scripts/verify-plan-file.py`.
- Modified: `CLAUDE.md`, `skills/specializations/planner/SKILL.md`, `scripts/spawn-specialist.py`, `memory-global/leaves/coordinator-pitfalls.md`, `scripts/verify-layout-contract.sh`, `skills/self-improvement/policy.md`, `.gitignore`.
- Smoke-tested negative cases: `verify-plan-file.py` rejects missing sections / missing `Expected result image:`; `validate_planner_plan()` rejects missing `Plan:` line and bogus path.

## Lessons

- **Split between code-enforced structure and prose-enforced execution is right.** `verify-plan-file.py` enforces the *shape* of the plan (sections + at least one verification line) — a deterministic, file-level check. The *application* of the per-stage gate (manager comparing actual outcome to the image and invoking `overcome-difficulty` on mismatch) stays in prose, because it's cognitive judgment — there's no deterministic "did this stage really pass" check the harness can run. Code where it can, prose where it must. Resists the temptation to build an executable-plan runner before there is evidence the prose gate fails.

- **Plan-as-file is a small structural change with outsized reuse value.** Before today, plans were often inline in the conversation, ephemeral, and re-derived from the running transcript when needed. Forcing a file at `~/.claude/plans/<slug>.md` means the plan survives the session, can be diff'd between revisions, and gives `overcome-difficulty` a stable reference for "what the plan declared". Cost: a few lines of prose + ~10 lines in `verify-plan-file.py`. Benefit: every future planner spawn has a durable artifact.

- **Adding to `verify-all.py` CHECKS is the wiring step that's easy to miss.** `verify-plan-file.py` does *not* belong in CHECKS — it operates on per-spawn plans, not on repo files — but I almost added it reflexively. Pause before adding: does this check apply to *every commit*, or only to a specific event (write, spawn, etc.)? Answer determines wiring (CHECKS vs hook vs spawn-specialist integration vs standalone CLI). Today: spawn-specialist integration + standalone CLI, no CHECKS membership.

## Self-critique of the agent system

- **I closed the task without asking for resolution, again.** Said `На здоровье! 🙂` after the push and stopped. User had to prompt me explicitly ("Почему ты не спрашиваешь решена ли задача?"). This is the **third** instance of `missed-leaf-at-resolution` recorded in experience leaves: see `2026-05-25-code-driven-enforcement-arc.md` § Self-critique, `2026-05-26-cron-tz-user-crontab-trap.md` § Self-critique, and now. The pattern is: the work is done, the diff is pushed, the user thanks me, and I treat "thanks" as session-end instead of as the cue to ask "task resolved?" → write leaf → trigger self-improvement.

  The existing M1 hook (`hook-self-critique-reminder.py`) only fires *after* an experience leaf is written — it can't catch the case where the leaf is never written in the first place. The CLAUDE.md rule ("ask once at the end of your reply") is prose and keeps being skipped. **This needs code enforcement** — proposing in the self-improvement turn that follows this leaf.

- **`[self-improvement-reviewed]` marker is recall-dependent every time `skills/self-improvement/` is touched.** Today's edit to `policy.md` § File structure (adding `verify-plan-file.py` to the listing) triggered the marker requirement, which I forgot until the commit-msg hook blocked. A pre-flight hint — e.g. when staging touches `skills/self-improvement/`, print "remember the marker" to stderr at `git add` time — would eliminate the recall. Lower-priority than the resolution gate but in the same shape of "rule exists, recall fails, code can help".

## Cost & effort

- **Wall-clock**: ~25 min from the user's plan-verify request to the push.
- **`claude -p` spawns**: 0 — work fit the *small change* carve-out (single-file edits, the manager has all files loaded this session).
- **User interventions**: 1 substantive (the original request) + 1 correction (this very leaf-prompt — the resolution-gate miss).
