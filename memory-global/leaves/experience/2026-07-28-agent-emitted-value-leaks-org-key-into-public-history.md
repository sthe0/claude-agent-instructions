---
name: 2026-07-28-agent-emitted-value-leaks-org-key-into-public-history
description: The commit-trailer helper emitted the ambient task key VERBATIM into Agent-Task, and that trailer is DESIGNED to land in the PUBLIC Core repo; an org-internal codename in the key prefix leaks, and the org-neutral commit-msg gate hard-blocks the commit. The only prior escape (unset CLAUDE_CODE_SESSION_ID) dropped BOTH trailer lines, losing the org-neutral session pointer too.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (fedor.solovyev@gmail.com)"
refs: [https://github.com/sthe0/claude-agent-instructions/issues/50, https://github.com/sthe0/claude-agent-instructions/commit/9dcabb6]
created: 2026-07-28
last_verified: 2026-07-28
---

# An agent-emitted value can leak an org-internal key into PUBLIC history — neutralize at emit-time, don't drop the field

## Difficulty
A value the agent emits into a PUBLIC artifact (a commit trailer that lands in public Core history) can carry an org-internal identifier. The org-neutral gate would hard-block it; the only prior workaround dropped the whole field, losing an org-neutral sibling value with it.

## Order & criterion
1) detect org-internal-ness with the SAME matcher that backs the public boundary gate (term_ruleset.discover_rulesets/scan) — not an ad-hoc regex, so neutralization tracks the gate; 2) on a match, neutralize DETERMINISTICALLY: Agent-Task: h:<sha12> = unsalted sha256(key)[:12] — stable across sessions/machines so commits stay groupable by task, obfuscation-to-pass-a-lint not a secret (keys are low-entropy); 3) keep the org-neutral sibling (Agent-Session) UNCONDITIONAL; 4) no ruleset / no match (external contributor) -> raw value, path unchanged; 5) wrap the whole scan fail-open (except -> raw) so the helper never raises and never blocks a commit.

**Acceptance check:** measurable: a real org-internal key hashes to h:<sha12> deterministically and passes check-org-neutral.py with no raw-key leak; no-ruleset -> raw; both paths keep the org-neutral sibling; tests green (17)

## Contexts

### 2026-07-28 — Neutralize an org-internal identifier at emit-time into public history
- Where it arose: scripts/agent_commit_trailer.py::trailers(); the same pattern applies to any runtime-emitted value that lands in a PUBLIC repo (log lines, PR bodies, generated docs)
- Working plan: Reuse the boundary matcher (term_ruleset) already behind the public gate; hash-not-drop for deterministic groupability; keep the org-neutral sibling unconditional; fail-open. Tracked by GitHub issue #50; landed commit 9dcabb6.

## Cost
1 developer spawn (~$1.67 list-price telemetry under flat Max — not real money), plus 1 thinker plan-review and 1 code-reviewer spawn (each sub-$1). 2 stages, 0 replans, 0 difficulty records. Engine-mechanics friction (not task cost): the acceptance gate needs identical `--observation` bytes in `stage-review` and `record-result`, and `record-result --code-ref` takes the raw ref (re-digested), not the review's stored `code_sha256`.

## Self-critique of the agent system
Plan research note wrongly claimed arc-land-pr.sh (a second, tested caller outside Core) was deleted; the developer caught it and correctly kept the docstring reference. Lesson: verify 'X is deleted/unused' claims against callers OUTSIDE the current repo before asserting them in a plan.
