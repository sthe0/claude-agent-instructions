---
name: 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan
description: An agentctl-tracked plan executed in an isolated git worktree diverges from the engine's static meta.repo_root: the engine auto-runs each stage's verify_command from repo_root (the main checkout), false-failing every stage whose commits live only in the worktree; and a refinement-class replan cannot fix it because it never reassigns verify_command/repo_root — only a substantive replan reloads state.stages+repo_root.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor (AskUserQuestion: 'Да — решено')"
created: 2026-06-29
last_verified: 2026-06-29
---

# agentctl verifies a stage in repo_root, not the worktree it ran in; only a substantive replan can move the venue

## Difficulty
Executing a substantive agentctl plan in an isolated git worktree (chosen so a parallel session's dirty tree is not entangled) silently diverges the verification venue from the engine's bookkeeping. The engine stores meta.repo_root once at submit-plan and runs every stage's verify_command as 'cd <repo_root> && <cmd>' — i.e. against the MAIN checkout, which lacks the worktree's commits — so each genuinely-green stage is recorded FAILED and routed to DIAGNOSING. Worse, the obvious fix (edit the plan's verify_command/repo_root to the worktree and replan) does NOT take: diff_plans classifies a verify_command/repo_root-only change as a 'refinement', and the refinement branch of cmd_replan reassigns only title/result/means/method/conditions/invariants — never verify_command or repo_root. Those two fields are reloaded ONLY by a 'substantive' replan (state.stages=new.stages; state.repo_root=new.meta.repo_root), and the structural signature that triggers substantive excludes verify_command — so a structural field (e.g. a stage done_criterion) must be changed to force it.

## Order & criterion
Make the engine verify in the venue that holds the commits. Either (a) keep repo_root = main checkout and accept the double-cd (verify_command itself 'cd <worktree> && ...' wins over the engine's prepended 'cd <repo_root> &&'), OR — when stages were already loaded with main-checkout verify_commands — force a SUBSTANTIVE replan: bump a structural field (a stage's done_criterion) so diff_plans returns 'substantive', which reloads state.stages (worktree verify_commands) + state.repo_root=worktree, returning to PLAN_READY for re-approval. Criterion: state.json shows repo_root=worktree and every stage verify_command points at the worktree; record-result then runs the real check green in-venue.

**Acceptance check:** agentctl status + state.json: repo_root and all stage verify_commands resolve to the worktree; each stage record-result passes its real verify_command (no rubber-stamp); verify-final green.

## Contexts

### 2026-06-29 — initial
- Where it arose: DEEPAGENT/global: memory temporal-frontmatter task. Executed the 5-stage plan in worktree /home/the0/claude-agent-instructions-mtf off clean main while a parallel session held the main checkout dirty on another branch. Engine false-failed stages 2 then 2-again; root-caused to the refinement-vs-substantive reload gap.
- Working plan: Difficulty cycle (declare/investigate/critique) localized the venue mismatch and the refinement-doesn't-reload-verify_command root cause; built a substantive replan (v3: per-stage done_criterion bump + worktree verify_commands + distinct means/method to satisfy the coverage gate's CHANGE check + the exact critique invariant string verbatim to satisfy PRESERVE), re-approved against the user's standing approval (venue-only correction, scope unchanged), drove each stage next-stage+record-result (skipping dispatch to avoid re-spawning already-done work), verify-final green.

## Cost
High for the diagnosis, low for the fix. The 5-stage plan itself was small mechanical edits; the cost concentrated in two false-FAIL difficulty cycles on stage 2 before the root cause (refinement replan does not reload `verify_command`/`repo_root` — only a substantive replan does) was isolated. Net overhead vs. a clean run: ~2 extra difficulty cycles + one full substantive replan (v2→v3) + reading `cmd_replan`/`gates.py`/`plan.py` to localize the reload gap. No spawned specialists (all stages driven in-thread via next-stage+record-result, dispatch deliberately skipped to avoid re-spawning already-done work).
