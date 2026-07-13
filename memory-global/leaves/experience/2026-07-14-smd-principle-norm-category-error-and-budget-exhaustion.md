---
name: 2026-07-14-smd-principle-norm-category-error-and-budget-exhaustion
description: Correcting a category error where the plan-activity principle (element 7) was a-priori typed as is/ought; plus the recurring budget-exhaustion dispatch pattern and verify-language staged-hook / stale-worktree-path difficulties met while landing it.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev (2026-07-14, quality 4)"
tier: 1
refs: [docs/adr/0004-principle-is-always-a-norm.md, scripts/agentctl/state.py, scripts/agentctl/gates.py]
created: 2026-07-14
last_verified: 2026-07-14
---

# SMD-fidelity correction: principle is always a норма; drop a-priori statement_kind; reframe failure_address to обеспечение <!-- Language exception: SMD source-ontology terms preserved verbatim for traceability. -->

## Difficulty
A domain-authority user (SMD methodology) rejected a landed mechanization at acceptance-review: element 7 (the refutable principle a plan stage rests on) had been typed a-priori as statement_kind: сущее|должное, as if a principle could be either a знание or a норма. That is a category error — принцип is the most-general member of the norm-series, so it is ALWAYS a норма (должное) and is never truth-checked; the is/ought fault-character is a POST-HOC product of критика at difficulty closure, not an a-priori plan-time tag. Separately, R2's failure_address был ошибочно связан со StatementKind (сущее|должное); правильно — routing по тому, какое ОБЕСПЕЧЕНИЕ было неадекватным: ресурсное (материал/средство) | нормативное (норма/способ) | not_applicable («норма — тоже ресурс», нормативное обеспечение ⊂ обеспечение деятельности). <!-- Language exception: SMD source-ontology terms preserved verbatim for traceability. -->

## Order & criterion
1) reject the affected stages → engine DIAGNOSING; 2) declare→investigate(≥2 hyp)→critique with functional-ground+replanning-task; 3) author v4 ADR-0004 (supersedes ADR-0003 §1) + code changes: DROP statement_kind from state.py/plan.py/verify-plan-file.py (grandfather on load, no migration), decouple FAILURE_ADDRESS_VALUES=(ресурсное,нормативное,not_applicable) from the deleted StatementKind enum; 4) mandatory thinker plan-review bound to the exact plan hash; 5) replan (substantive → re-approval); 6) execute stages via engine dispatch; 7) verify-final; 8) LAND DIRECTLY to origin/main via SSH (owner repo — no PR). <!-- Language exception: SMD source-ontology terms preserved verbatim for traceability. -->

**Acceptance check:** acceptance-review by the SMD-authority user: element 7 no longer carries an a-priori is/ought type; failure_address routes by which обеспечение (ресурсное|нормативное|not_applicable) was inadequate, decoupled from StatementKind; verify-final PASSED; both commits on origin/main. <!-- Language exception: SMD source-ontology terms preserved verbatim for traceability. -->

## Contexts

### 2026-07-14 — SMD principle=norm category error
- Where it arose: sthe0/claude-agent-instructions (agent's own instruction/engine repo); agentctl engine; SMD/MMK activity-theory mechanization (DEEPAGENT-448 adjacent, cai-wt-smd worktrees)
- Working plan: Two executing stages of the v4 plan: Stage 1 (R4) drop statement_kind across state.py+plan.py+verify-plan-file.py, ADR-0004 supersedes ADR-0003 §1, grandfather legacy carriers on load; Stage 4 (R2) reframe failure_address values to ресурсное|нормативное|not_applicable обеспечение, decouple from StatementKind (now deleted). Stages 2&3 landed under v3, untouched. Order 1→4. Landed 0b607b1 + 6dd638a, merged to origin/main 663aefb via SSH --no-ff. <!-- Language exception: SMD source-ontology terms preserved verbatim for traceability. -->

## Cost
$42.94 across 6 spawns (v4 delta); full task incl v3 higher. Budget-exhaustion pattern cost extra unblock/verify/commit root cycles per mechanize stage.

## Self-critique of the agent system
The v3 category error should have been caught at v3 plan-review: typing element 7 a-priori contradicts the norm-series definition already stated in ADR-0003's own Context. A domain-authority acceptance-review criterion (SMD fidelity) is not substitutable by tests-green — the mechanization was internally consistent AND wrong. Record the reusable operational difficulties too: (a) budget-exhaustion dispatch — developer completes work in-worktree but exhausts $8 before committing → BLOCKED; response is unblock→verify→commit→record-result --control, NEVER re-dispatch; (b) verify-language staged pre-commit hook demands an adjacent Language-exception comment (±3 lines) OR quoting/backtick-wrapping for new Cyrillic prose — end-of-line self-exempting comment is zero-line-shift and cluster-covering, backtick-wrap is byte-minimal for a byte-ceiling-tight file like CLAUDE.md; run the WORKTREE's copy of verify-language (git worktrees have real script files → correct repo_root/index), not the main checkout's; (c) per-stage verify_commands hardcoding a since-removed worktree path (cd /home/the0/cai-wt-smd) fail verify-final after that worktree is cleaned — OD refinement re-points them; (d) a worktree-external plan file is unreadable by a spawned thinker → inline the corrected TOML blocks into the review brief.
