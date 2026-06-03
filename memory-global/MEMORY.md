# Global memory

Index of memories applicable across all projects on this machine. Entries are pointer lines to leaf files in `leaves/`.

Loaded into every session via `@~/.claude/memory-global/MEMORY.md` import in `CLAUDE.md`. Keep this index under ~200 lines — anything past the first 200 lines is truncated by the harness.

## How to use

- **Read:** open this file, then the relevant leaf. Do not load every leaf at session start.
- **Write:** when you learn a fact that applies across projects (user role, machine-wide tools, cross-project workflow), add a leaf in `leaves/` with the auto-memory frontmatter (`name`, `description`, `type` — `user` / `feedback` / `project` / `reference`) and add a one-line pointer here.
- **Project-only facts** belong in `<project_cwd>/.claude/agent-memory/` instead.

## Reasoning and coordination practices

- [Coordinator objective](leaves/coordinator-objective.md) — what to minimize (cost / tokens / user time and attention / clicks / resolution time) and maximize (autonomy / reliability / controllability / verifiability); how to resolve trade-offs between conflicting axes.
- [Reasoning and task solving](leaves/reasoning-and-task-solving.md) — understand before acting, plan and approval, when stuck, memory vs prompts, self-check before first production edit.
- [Typical coordinator pitfalls](leaves/coordinator-pitfalls.md) — anti-patterns to avoid as the root coordinator; signals that point to specific corrective actions.
- [Workflow debug investigation](leaves/workflow-debug-investigation.md) — ordered checklist for orchestrated pipeline failures: reference baseline, topology/causality, code delta, then infra logs; hypothesis portfolio.
- [Decomposition markers (M1–M4)](leaves/decomposition-markers.md) — when to split a substantive task into multiple PRs/tickets; applied after plan approval, before implementation.
- [Log-reading discipline](leaves/log-reading-discipline.md) — 10-line cap per tool call when reading logs; aggregate first, surface digests.
- [Acting without asking](leaves/acting-without-asking.md) — side-effect-free actions and plan-scope-declared changes are pre-authorized; 1-lookup budget for unknown tools; substantive plan changes still require approval.
- [Code comment discipline](leaves/code-comment-discipline.md) — default no comments; comment only when the *why* is non-obvious; build / config files (`ya.make`, `Dockerfile`, …) are not exceptions; concrete antipatterns from DEEPAGENT-414 PR review.
- [Skill-first dispatch](leaves/skill-first-dispatch.md) — scan the system-reminder skill list before hand-rolling Bash for known domain ops; class-of-operation → skill-family table; `fewer-permission-prompts` as the audit habit.
- [Memory hierarchy](leaves/memory-hierarchy.md) — when to spin off `<subdir>/MEMORY.md` sub-indexes (monotonic / domain-coherent / display pressure); mechanics, anti-patterns, retire procedure.
- [Systemic pattern scan](leaves/systemic-pattern-scan.md) — at resolution: scan experience for recurring friction; run overcome-difficulty against the agent-system-as-plan; route the resulting architectural proposal through self-improvement.
- [Specialization & skill usage tracking](leaves/specialization-skill-usage-tracking.md) — at resolution, generate `name | count | purpose` table for every specialization spawn and `Skill` / `Agent` invocation via `scripts/tool-usage-report.py`; feeds the experience leaf's "Cost, effort, and tool usage" section.
- [Token-economy plan](leaves/token-economy-plan.md) — living programme of context-engineering / cache-aware / static-prefix changes; self-improvement reads this before proposing new token-economy edits and updates rows in place.
- [Skill catalog curation](leaves/skill-catalog-curation.md) — discipline for keeping the user-invocable skill catalog lean; periodic audit via `scripts/skill-usage-audit.py`.
- [Large tool-output discipline](leaves/large-tool-output-discipline.md) — pipe high-volume Bash commands through `scripts/offload-large.sh` so the model gets a head+tail digest and the full bytes live in `/tmp/cc-scratch/`.
- [Plan-file split](leaves/plan-file-split.md) — for plans > ~20 KB / > 3 stages, split into index + per-stage files so later Reads pull only the active stage.

## Tooling and mechanics

- [Subagent resume and transcripts](leaves/subagent-resume-and-transcripts.md) — `SendMessage` resume mechanism (needs `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`), transcript layout under `~/.claude/projects/.../subagents/`, subagent auto-compaction, cleanup.
- [settings.json env precedence](leaves/claude-code-settings-env-precedence.md) — env in settings.json overrides shell env (`env -u` does not help); auth precedence ladder; what to do when an apiKeyHelper isn't enough.
- [Remote sudo access paths](leaves/remote-sudo-access-paths.md) — when Claude needs sudo on a remote host (user-A ssh → work in user-B's space): NOPASSWD narrow scope (default) / Defaults timestamp_type=global (short window) / direct ssh as target; Claude's `!` shell has no TTY so interactive `sudo -v` won't work.

Workflow-level permissions (separate from memory): `~/claude-agent-instructions/permissions/` + `scripts/permissions-cli.py`. Not a memory leaf — operational config.

## System knowledge

- [System-knowledge sub-index](leaves/system-knowledge/MEMORY.md) — durable facts about systems, processes, org structure, codebase architecture that aren't self-evident; recording criteria in `~/.claude/CLAUDE.md` § Memory.

## Resolved-task experience

- [Experience sub-index](leaves/experience/MEMORY.md) — chronological log of resolved-task experience leaves (one per non-trivial task — final plan, difficulties, artifacts, lessons, self-critique, cost).

## Period retrospectives

- [Session retrospective 2026-05](leaves/session-retrospective-2026-05.md) — period summary, top mistakes, ticket startup checklist, self-check gates.
