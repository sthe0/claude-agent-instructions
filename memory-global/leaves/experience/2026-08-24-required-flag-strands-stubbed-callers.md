---
name: 2026-08-24-required-flag-strands-stubbed-callers
description: A gate added at the single producer (file-difficulty.py's exactly-one-of --cost/--cost-not-estimable) silently disabled an in-repo caller, and that caller's own test could not see it because the test stubbed the producer with a fake script; plus the general half — a gate binds only callers who pass through it, so the consuming surface must MARK the records that arrived by another path.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [https://github.com/sthe0/claude-agent-instructions/commit/6a86058, https://github.com/sthe0/claude-agent-instructions/commit/dd88237, https://github.com/sthe0/claude-agent-instructions/commit/6fcc3e6, https://github.com/sthe0/claude-agent-instructions/issues/169]
plan_file: /home/the0/.claude-agent/plans/cost-estimate-in-record.toml
created: 2026-08-24
last_verified: 2026-08-24
---

# A newly-required CLI flag strands the in-repo callers whose tests stub that CLI

## Difficulty
Two halves of one shape. (1) Making a flag REQUIRED on a shared CLI breaks every in-repo caller that composes its argv by hand — here scripts/self_diagnose_store.py::_default_filer, which after the gate always exited 2 on exactly the machine population the channel exists for, while route_advisory treats any non-zero filer exit as 'not filed' and skips the row: no crash, no log, the automated self-diagnose -> backlog path simply gone. The caller's own test (test_default_filer_argv_shape_and_success) passed throughout, because it stubs file-difficulty.py with a fake script that ignores the gate — a test that pins argv SHAPE cannot detect that the real callee started rejecting that shape. (2) A gate installed at one producer binds only the callers who pass through it; records reaching the same channel by another path (raw gh issue create) arrive with the gated field empty, textually identical to a record whose producer had nothing to say. Detection must therefore live at the CONSUMING surface, marking 'N of M filed outside the gate' — and that marker is a detector, not a repair.

## Order & criterion
When filing a backlog/difficulty record, besides stating the difficulty and what exactly is broken and how, estimate what the problem costs us in tokens/money/quota when such an estimate is possible — mechanized, plus the prose cross-references.

**Acceptance check:** measurable: the four scoped test files green together; both prose greps; policy.md within its 400-line ceiling; the delivered commit contained in origin/main by merge-base --is-ancestor.

## Contexts

### 2026-08-24 — initial
- Where it arose: claude-agent-instructions: scripts/file-difficulty.py, scripts/difficulty_channel/{port.py,adapters/github.py}, scripts/core-difficulty-digest.py, scripts/self_diagnose_store.py, memory-global/leaves/backlog-triage-practice.md, skills/self-improvement/policy.md
- Working plan: Three stages: (1) spawn developer — cost_estimate field in the record port, a Cost line rendered before the Evidence marker in the GitHub adapter and parsed back, an exactly-one-of --cost/--cost-not-estimable gate in file-difficulty.py, and a digest note showing cost next to the severity mass plus an 'N of M filed outside the cost gate' marker; (2) in-thread prose — the cost_of_problem term in the triage rubric and a file-through-the-script paragraph in the self-improvement policy, plus a full verify-all.py run reported without gating; (3) land via land-branch.py --remote-only and assert containment in origin/main.

## Cost
3 commits, 14 files, +340/-17; 2 developer spawns + 2 code-reviewer spawns, $5.01 list-price telemetry over ~17 min; one revise round.

## Self-critique of the agent system
I dispatched the gate without enumerating the CLI's own in-repo callers first — a required-flag change has a mechanically enumerable blast radius (grep the script name across .py/.sh/.md/.mdc), and I left that enumeration to the reviewer. I also committed once with -c core.hooksPath=.githooks, a path that does not exist in this repo, which skipped the entire pre-commit chain; caught it myself and amended, but passing a hooksPath override at all is the defect.
