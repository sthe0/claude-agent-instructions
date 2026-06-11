---
name: 2026-06-11-coordinate-task-and-hook-sync
description: P5 coordinate-task.py (coordination cycle as code) + repaired the architecture-description sync mechanism (bidirectional hook-registration check) after the description drifted from reality
type: reference
resolution_confirmed_by_user: "Пуш + считаем решённым (Recommended)"
plan_file: /home/the0/.claude/plans/coordinate-task-and-hook-sync.md
---

# P5 coordinate-task.py + hook-architecture sync repair

User asked to do P5 (the deferred coordination-cycle-in-code script) and, separately, to verify whether the mechanism that keeps the agent-system architecture description in sync with reality actually works — citing the hooks architecture as a test case.

## Final plan as executed

Followed [plan_file](/home/the0/.claude/plans/coordinate-task-and-hook-sync.md) (3 stages) without structural change. One mid-flight expansion, in-scope: Stage 2's new bidirectional check immediately exposed **pre-existing** drift beyond the two known hooks — four already-shipped reminder hooks (self-critique / tracker / push-confirmation / resolution) were never in README either. Documenting them was required for `verify-all` to go green and is exactly what the check is meant to enforce, so it was applied without re-approval.

Implemented via a single spawned `developer` (opus, large tier) covering all three stages → one commit `ed03fac` on `main`, pushed to origin after user confirmation.

## Difficulties

- **Diagnostic finding (the user's actual question):** the "sync mechanism" (`verify-layout-contract.sh`) was a one-directional allowlist — it asserted listed files exist + forbidden files absent, but never asserted that every real `hook-*.py` is listed. So additions were structurally invisible; the 2026-06-11 hooks were registered only in live `~/.claude/settings.json`, missing from all three repo description surfaces (contract, README, `install-reminder-hooks.sh`). Answer to "does it work / are latest changes reflected": **no.** Fix = make it bidirectional.
- **Verification axis trap (caught in self-review):** first gate test passed misleadingly — I omitted a required argparse arg, so the script erred *before* reaching the approval gate, and the `EXIT=0` I read was `echo`'s code through a pipe, not python's. Re-ran with full args + real exit code → confirmed `exit 2` refusal. Lesson reinforced: when reading an exit code after a pipe, use `${PIPESTATUS[0]}`, and make the negative test actually reach the code path under test.
- **Trust-but-verify the spawn:** independently re-ran `verify-all.py`, the Stage-2 negative test (remove hook line → FAIL naming it; restore → PASS), and all `coordinate-task.py` runtime checks rather than accepting the developer's COMPLETED report.

## Artifacts

- Commit `ed03fac` (origin/main): `scripts/coordinate-task.py` (new, 273 lines), `scripts/verify-layout-contract.sh` (+bidirectional check), `scripts/install-reminder-hooks.sh` (+2 tuples), `README.md` (+7 rows: 2 new + 4 backfilled hooks + coordinate-task.py).
- `coordinate-task.py`: `plan` subcommand (spawn planner → `verify-plan-file.py` → print approval-gated run cmd) and `run --approved` subcommand (spawn developer, marker→exit-code, refuses without `--approved` = real human PLAN-READY gate). Both `--dry-run`.

## Lessons

- An allowlist-style structure contract silently tolerates additions. To keep a description in sync with reality, the check must be **bidirectional** — enumerate the real artifacts and assert each is described, not only that the described ones exist. This is the generalizable fix for "description drifted from architecture."
- New hooks/scripts must be registered in the repo's description surfaces, not just wired into machine-local `settings.json`. Now mechanically enforced for hooks at pre-commit.

## Self-critique of the agent system

- **Root cause already fixed structurally:** the friction (drift invisible to the contract) is now a hard pre-commit failure via the bidirectional check — this *is* the architectural improvement, so no further `CLAUDE.md`/skill text edit is warranted. Self-improvement is satisfied by the shipped mechanism rather than a prose rule.
- **Residual gap (not fixed, noted):** the bidirectional check covers contract + README, but **not** `install-reminder-hooks.sh` (canonical wiring). A hook documented in both surfaces but absent from the canonical wiring would still fail to install on a fresh machine and slip through. Natural follow-up: extend the check to also assert each PreToolUse/UserPromptSubmit hook appears in `install-reminder-hooks.sh`. Did not expand scope without asking.
- **Tooling friction:** `cost-report.py --since` rejects human strings ("40 minutes ago"); requires ISO. Minor, but it cost a retry. `tool-usage-report.py --since "30 minutes ago"` returned empty. Both make the post-resolution cost section harder to fill mechanically.

## Cost, effort, and tool usage

- One spawned `developer` (opus, `budget-large-usd` $8 cap, complexity high), ~7 min wall, all edits in-scope (transcript grep showed zero writes outside `~/claude-agent-instructions/`).
- Manager-side: 1 planner-discipline plan (inline, no spawn), independent runtime verification, 1 push after explicit confirmation.
- Specializations/skills: `developer` spawn ×1 (implement 3 stages). No planner/thinker spawn (plan was simple enough to draft inline with loaded context).
- Resource that drove cost: the developer spawn (the only model spend beyond the main thread). spawn-specialist `--dry-run` previewed the command before the real run — cheap insurance against a misconfigured $8 spawn.
