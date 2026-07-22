---
name: 2026-07-22-semantic-gate-regex-to-judge
description: Two Stop-hook guardians and one PreToolUse deny gate decided a semantic question (is-feedback / is-unhandled-outage-escalation) with phrase regexes and false-positive-blocked the agent's own analytical prose; demoted the regexes to high-recall prefilters behind two fail-open advisor LLM judges mirroring judge_binary_ask.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev (Да, решено)"
refs: [memory-global/leaves/regex-not-for-semantic-classification.md, scripts/agentctl/advisor.py, scripts/hook-turn-end-gate.py, scripts/hook-escalation-diagnosis-gate.py]
created: 2026-07-22
last_verified: 2026-07-22
---

# Semantic hard-block gates: regex → prefilter + fail-open LLM judge

## Difficulty
A regex conjunction deciding a SEMANTIC classification (this-is-feedback / this-is-an-unhandled-outage-escalation) for a HARD block determinizes a perceptual task at the wrong structural level, so it false-fires on any text that merely quotes/describes the pattern (analysis of CLAUDE.md), blocking the turn. The design lesson is durably captured in the reference leaf [[regex-not-for-semantic-classification]]; this leaf records the reusable PROCESS gotchas met while shipping it.

## Order & criterion
1 codify principle (reference leaf + CLAUDE.md clause + SKILL recipe) → 2 two fail-open judges in advisor.py mirroring judge_binary_ask → 3 rewire both hard-block consumers to prefilter-AND-judge (+killswitch env) → 4 full hook-suite audit → 5 green run from worktree

**Acceptance check:** measurable: both original false-positive scenarios no longer block (fake-runner NO) and a live signal blocks (YES); verify-agentctl + 5 pytest suites + lint-prose-length + verify-leaf-structure green from the worktree

## Contexts

### 2026-07-22 — shipping a semantic gate rewire
- Where it arose: editing the agent's own hooks/advisor in the claude-agent-instructions repo, delivered via an isolated git worktree landed by fast-forward
- Working plan: /home/the0/.claude-agent/plans/semantic-gate-llm-judge.toml
- Reusable process gotchas met here:
  - **`AGENT_RECURSION_DEPTH=1` leaks into a test subprocess.** A stage-4 developer saw 18 spurious pytest failures because its own spawn context (`AGENT_RECURSION_DEPTH=1`) leaked into the test's subprocess, and the Stop hook short-circuits for depth≥1 — run the hook's tests with `env -u AGENT_RECURSION_DEPTH`.
  - **`agentctl dispatch` on a large `spawn:developer` stage can be SIGTERM-killed at the 600s Bash foreground cap** — keep such stages small, or the whole dispatch is lost.
  - **A judge reading the USER buffer must be fed `strip_injected_context(text)`** — the harness replays CLAUDE.md/SKILL.md into the user buffer, and that injected text re-introduces the very false positives being removed. A judge reading ASSISTANT text is NOT stripped (no injection there).

## Cost
5 stages, ~1 session across a compaction; spawns: 1 planner-review (general-purpose), 2 code-reviewer, 2 developer

## Self-critique of the agent system
Plan-review needed one REVISE (test-integrity gaps), stage-3 dispatch was SIGTERM-killed at the 600s Bash cap (spawn:developer stage too large — keep such stages small), stage-4 audit conflated a 4th already-correct semantic site, and I defaulted to a PR to land despite an explicit prior user correction that this sole-maintained repo lands direct (recorded in [[landing-discipline]]; the recurrence prompted a CLAUDE.md § Instructions-repository disambiguation that "separate gate" means push-confirmation, not a PR).
