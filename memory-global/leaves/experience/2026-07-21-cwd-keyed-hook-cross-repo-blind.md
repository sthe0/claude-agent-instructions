---
name: 2026-07-21-cwd-keyed-hook-cross-repo-blind
description: hook-resolution-reminder.py's landing nudges keyed off session cwd, so direct_push_no_pr_hint went silent for Core delivery coordinated from a non-Core session — the cross-repo case where the PR-default is most likely. Fix: _delivery_repo_dir reads the active agentctl session's declared delivery tree (delivery_worktree, else repo_root, else cwd) and the probes key off that. Reusable pattern: key enforcement off the delivery TARGET the session declares, not ambient cwd, whenever the two can diverge. Follow-up audit CLOSED (commit 5b904f2): the cwd-keyed hooks were swept; hook-readme-currency-reminder was the one genuinely-affected consumer — its doc-staleness changeset now keys off effective_git_cwd (parses `cd <dir> &&` / `git -C <dir>`), extracted to a shared scripts/lib/git_cwd.py and imported by both it and hook-guard-canon-readonly (DRY).
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "Да, решена"
refs: [commit:3b352a1, commit:5b904f2, landing-discipline.md, 2026-07-09-landed-not-deployed-checkout-parked-on-feature-branch.md, 2026-07-18-plan-presentation-two-act-delivery-gate.md]
created: 2026-07-21
last_verified: 2026-07-21
---

# A resolution/enforcement hook keyed off session cwd is blind to cross-repo delivery — key off the session's declared delivery tree

## Difficulty
hook-resolution-reminder.py computed its landing nudges (landable / unpushed / merged-leftover / direct-push-no-PR) off repo_dir = payload.get('cwd') — the SESSION cwd. When Core work is coordinated from a NON-Core session (cwd = the deepagent tree), direct_push_no_pr_hint returned None because repo_dir is not under the Core root, so the anti-PR nudge went SILENT exactly on the cross-repo case where the PR-default is most likely. This mechanized a twice-repeated user correction ('why PR what you can fast-forward into main directly', 2026-07-11 + 2026-07-21): the prose rule (landing-discipline.md) already existed — the residual was a MECHANISM blind spot, not a prose gap. A cwd-keyed enforcement hook silently under-fires whenever the delivery target repo differs from where the coordinating session runs.

## Order & criterion
Fix: a pure helper _delivery_repo_dir(session_id, cwd) reads the ACTIVE agentctl session state (same config_root.resolve_agentctl_state_file + json.loads path the hook already uses for resolution_gate_open) and returns the session's DECLARED delivery tree — delivery_worktree preferred (the un-landed branch physically lives there), else repo_root — when set AND existing on disk, else cwd; whole body guarded so a non-dict JSON / non-str field degrades to cwd (the hook must never crash a resolution turn). main() keys the four probes off this value. This reuses state landed the same day (#45's [meta] delivery_worktree/repo_root on SessionState), so a Core delivery coordinated from anywhere carries the Core tree in its live session state. Ran through the full engine spine (classify substantive -> plan -> thinker plan-review verdict pass -> present essence -> approve -> execute in-thread -> verify-final). Delivered dogfooding the fix: ONE commit, fast-forward directly into main, NO PR (land-branch.py --remote-only since canon is checked out on main with parallel-session WIP), branch+worktree deleted.

**Acceptance check:** MEASURABLE: pytest scripts/tests/test_resolution_reminder_hook.py -q => 28 passed (23 pre-existing green + 5 new probe-repo-dir cases: delivery_worktree preferred over cwd; both-set -> worktree wins; repo_root-only -> repo_root; cwd fallback when unset / when declared path missing). origin/main = 3b352a1.

## Contexts

### 2026-07-21 — cwd-keyed hook cross-repo blind spot
- Where it arose: Core instructions repo (~/claude-agent-instructions), scripts/hook-resolution-reminder.py, coordinated from a deepagent (non-Core) session.
- Working plan: /home/the0/.claude-agent/plans/si-resolution-hint-repo.toml

### 2026-07-21 — audit close: readme-currency was the one genuinely-affected cwd-keyed hook; extract to shared lib
- Where it arose: the same audit's open flag. Swept scripts/hook-*.py + lib for payload.get('cwd')/os.getcwd() keying an enforcement/nudge decision. Only hook-readme-currency-reminder.py was genuinely affected: it computed its doc-staleness changeset off the ambient cwd, so the standard isolated-landing pattern `cd <worktree> && git commit` measured the changeset against the SESSION tree, not the tree the commit targets.
- Fix form (user-directed): extract the redirect-parser (already present in hook-guard-canon-readonly.py's local _effective_git_cwd, #44) into a shared scripts/lib/git_cwd.py::effective_git_cwd(command, payload_cwd) and route BOTH hooks through it — DRY, one test surface, the rule cannot drift between two copies. Default when an existing mechanism implements a rule for one path: extend that mechanism to the missing path (self-improvement SKILL.md § Structural form before prose, tie-breaker).
- Non-obvious finding worth the leaf: hook-readme-currency-reminder's COMMIT_RE = r'\b(?:git|arc)\s+commit' matches only CONTIGUOUS `git commit`/`arc commit`, so it never fires on `git -C <dir> commit` — the `-C` branch of effective_git_cwd is UNREACHABLE through this hook (it is reachable through hook-guard-canon-readonly, whose _is_git_commit tokenizes). So the hook's cross-repo cases are cd+git / cd+arc / no-redirect fallback; the `-C` branch is covered at the lib level (test_git_cwd.py). A planned `-C` hook test was swapped for this reason (plan refinement, applied in-thread).
- Acceptance: MEASURABLE pytest test_git_cwd.py + both hook suites => 61 passed. origin/main = 5b904f2.
- Working plan: /home/the0/.claude-agent/plans/readme-currency-effective-git-cwd.toml

## Cost
Flat Max subscription — no real-money figure (`agentctl resolve` reported total_cost_usd=null, spawn_count=0). Effort ~medium: in-thread execution (executor in_thread, no `claude -p` specialist spawn) + one thinker plan-review via a Task subagent (verdict revise → refine → pass) + one thinker replan-review earlier in the backlog thread. One code+test edit landed as a single fast-forward commit (3b352a1); this leaf landed as a second commit off origin/main via an isolated worktree.

## Self-critique of the agent system
OPEN FLAG CLOSED (commit 5b904f2, 2026-07-21): the follow-up cwd-keyed-hook audit ran; hook-readme-currency-reminder.py was the single genuinely-affected consumer and is fixed by routing through the extracted shared lib.git_cwd.effective_git_cwd. Note the two hooks key off DIFFERENT tree-targets by design: hook-resolution-reminder keys off the session's DECLARED delivery tree (agentctl SessionState), while these two commit-triggered hooks key off the redirect the COMMAND itself embeds (`cd`/`-C`) — same functional ground ("don't key off ambient cwd when the real target can diverge"), two mechanisms because the authoritative signal differs (a not-yet-run session-declared target vs an about-to-run command's own redirect). General pattern for reuse: an enforcement mechanism should key off the delivery TARGET, sourced from whichever authority actually names it (session state, or the command's own cd/-C), not the ambient cwd.
