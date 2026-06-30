# Global memory

Index of memories applicable across all projects on this machine. Entries are pointer lines to leaf files in `leaves/`.

Loaded into every session via `@~/.claude/memory-global/MEMORY.md` import in `CLAUDE.md`. Keep this index under ~200 lines — anything past the first 200 lines is truncated by the harness.

## How to use

- **Read:** open this file, then the relevant leaf. Do not load every leaf at session start.
- **Write:** when you learn a fact that applies across projects (user role, machine-wide tools, cross-project workflow), add a leaf in `leaves/` with the auto-memory frontmatter (`name`, `description`, `type` — `user` / `feedback` / `project` / `reference`, plus the `created` / `last_verified` date fields — see [memory-temporal-frontmatter.md](leaves/memory-temporal-frontmatter.md)) and add a one-line pointer here.
- **Project-only facts** belong in `<project_cwd>/.claude/agent-memory/` instead.

## Reasoning and coordination practices

- [Coordinator objective](leaves/coordinator-objective.md) — what to minimize (cost / tokens / user time and attention / clicks / resolution time) and maximize (autonomy / reliability / controllability / verifiability); how to resolve trade-offs between conflicting axes.
- [Reasoning and task solving](leaves/reasoning-and-task-solving.md) — understand before acting, plan and approval, when stuck, memory vs prompts, self-check before first production edit.
- [Typical coordinator pitfalls](leaves/coordinator-pitfalls.md) — anti-patterns to avoid as the root coordinator; signals that point to specific corrective actions.
- [Workflow debug investigation](leaves/workflow-debug-investigation.md) — ordered checklist for orchestrated pipeline failures: reference baseline, topology/causality, code delta, then infra logs; hypothesis portfolio.
- [Partition markers (M1–M4)](leaves/partition-markers.md) — delivery partition: into how many independently-shippable PRs/tickets the approved plan is cut; applied after plan approval, before implementation. Distinct from the planner's step-level decomposition.
- [Log-reading discipline](leaves/log-reading-discipline.md) — 10-line cap per tool call when reading logs; aggregate first, surface digests.
- [Acting without asking](leaves/acting-without-asking.md) — side-effect-free actions and plan-scope-declared changes are pre-authorized; 1-lookup budget for unknown tools; substantive plan changes still require approval.
- [Code comment discipline](leaves/code-comment-discipline.md) — default no comments; comment only when the *why* is non-obvious; build / config files (`ya.make`, `Dockerfile`, …) are not exceptions; concrete antipatterns from DEEPAGENT-414 PR review.
- [Mirror the working caller before inventing a bypass](leaves/mirror-working-caller-before-bypass.md) — on missing ambient context inside a constrained env (job/sandbox/frozen layer), replicate how the existing working caller establishes that context before reaching for an env/quota/path shim.
- [Tests accompany code](leaves/tests-accompany-code.md) — any code change ships with tests verifying it; symmetric default for developer (writes) and reviewer (rejects a diff lacking them); named non-testable escape class; soft commit-msg backstop.
- [Docs accompany an architectural change](leaves/docs-accompany-architectural-change.md) — an ADR / new subsystem / changed coordination model is not delivered until the canonical read-first surface (README + docs index) reflects it; symmetric to tests-accompany-code on the documentation axis; named escape class; resolution gate checks the doc axis, not just tests-green.
- [Skill-first dispatch](leaves/skill-first-dispatch.md) — scan the system-reminder skill list before hand-rolling Bash for known domain ops; class-of-operation → skill-family table; `fewer-permission-prompts` as the audit habit.
- [Delegatable work patterns](leaves/delegatable-work-patterns.md) — two shapes the opus main thread must hand to a cheap-model sub-agent (A post-spawn monitoring, B initial codebase/data exploration); model-tier heuristic (haiku retrieval / sonnet search / opus reasoning); the `Agent` tool inherits opus unless `model:` is set.
- [Long-job monitoring](leaves/long-job-monitoring.md) — drive a long external job (hours/days) to terminal state yourself: detached OS poller (zero model tokens) + self-scheduled `ScheduleWakeup`/`CronCreate` wakeups report transitions; cheap `Agent` only at the DoD milestone. Anti-pattern: offloading the cadence to the user ("ping me when it's done").
- [Memory hierarchy](leaves/memory-hierarchy.md) — when to spin off `<subdir>/MEMORY.md` sub-indexes (monotonic / domain-coherent / display pressure); mechanics, anti-patterns, retire procedure.
- [Memory usage](leaves/memory-usage.md) — when to read / verify / write memory and what never to persist (reconcile mutable-state leaves against live source; cite OS/version claims; behavioral rules go to CLAUDE.md not memory); the hygiene rules behind the three-scope table.
- [Leaf schema (`leaf/v1`)](leaves/leaf-schema.md) — rigid section shape for ordinary reference/feedback/system-knowledge leaves (`## Difficulty` / `## Guidance` / `## See also`); `verify-leaf-structure.py` enforces opted-in leaves, grandfathers the rest; experience leaves use `difficulty/v1` instead.
- [Memory temporal frontmatter](leaves/memory-temporal-frontmatter.md) — two-field contract: `created`+`last_verified` required and tool-stamped (all 3 scopes, all schemas); `last_accessed` is retired — validators reject re-introduction.
- [Plan activity ontology](leaves/plan-activity-ontology.md) — the 8 elements a plan must cover (order; material+result; control criterion; means [immutable]; method; conditions+invariants; actor+capability; refutable principle) mapped to `agentctl/plan.py` + `verify-plan-file.py` fields; substantive-only; recursive sub-ordering (any unmet element → service sub-plan) as the genesis of composite plans.
- [Systemic pattern scan](leaves/systemic-pattern-scan.md) — at resolution: scan experience for recurring friction; run overcome-difficulty against the agent-system-as-plan; route the resulting architectural proposal through self-improvement.
- [Specialization & skill usage tracking](leaves/specialization-skill-usage-tracking.md) — at resolution, generate `name | count | purpose` table for every specialization spawn and `Skill` / `Agent` invocation via `scripts/tool-usage-report.py`; feeds the experience leaf's "Cost, effort, and tool usage" section.
- [Token-economy plan](leaves/token-economy-plan.md) — living programme of context-engineering / cache-aware / static-prefix changes; self-improvement reads this before proposing new token-economy edits and updates rows in place.
- [Skill catalog curation](leaves/skill-catalog-curation.md) — discipline for keeping the user-invocable skill catalog lean; periodic audit via `scripts/skill-usage-audit.py`.
- [Large tool-output discipline](leaves/large-tool-output-discipline.md) — pipe high-volume Bash commands through `scripts/offload-large.sh` so the model gets a head+tail digest and the full bytes live in `/tmp/cc-scratch/`.
- [Plan-file split](leaves/plan-file-split.md) — for plans > ~20 KB / > 3 stages, split into index + per-stage files so later Reads pull only the active stage.
- [Spawning specialists](leaves/spawning-specialists.md) — full `claude -p` spawn mechanics: spawn-template inputs, budget tiers, recursion cap, monitoring a running spawn, after-spawn `status`+`log` checks, `bypassPermissions` discipline, return markers.
- [Handling escalations](leaves/handling-escalations.md) — how the manager resolves each specialist return marker (PLAN-READY / CLARIFY / REPLAN / PERMISSION-REQUEST / ESCALATE / INCOMPLETE / COMPLETED) and the continuation-prompt templates for re-spawning.
- [Recording experience](leaves/recording-experience.md) — the execution-time how-to that follows the CLAUDE.md resolution gate: quality bar (decide whether to record), search-before (extend|new), `difficulty/v1` schema, ticket-thin leaf, required `resolution_confirmed_by_user` frontmatter, self-critique → self-improvement auto-trigger.
- [Robot-run ACL access](leaves/robot-run-acl-access.md) — when YT/Nirvana ops run under a robot identity, set a team idm-group read ACL on EVERY op (named constant, all ops not a subset) or humans can't read stderr/logs/artifacts; verify a human token actually reads a finished op.
- [Instruction-development queues](leaves/instruction-dev-queues.md) — the 3-tier tracking model (Core / Org-Yandex / Project): each tier has an internal backlog + a report inbox; collapse rule when filers==editors (Project→one queue); stream-separation by queue (Org: OOSEVEN backlog / OOSEVENREPORT reports) or by label (Core GitHub Issues: `difficulty` reports / `backlog`); per-project queue via the `agent-project.json` `instruction_queue` field.

## Tooling and mechanics

- [Subagent resume and transcripts](leaves/subagent-resume-and-transcripts.md) — `SendMessage` resume mechanism (needs `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`), transcript layout under `~/.claude/projects/.../subagents/`, subagent auto-compaction, cleanup.
- [settings.json env precedence](leaves/claude-code-settings-env-precedence.md) — env in settings.json overrides shell env (`env -u` does not help); auth precedence ladder; what to do when an apiKeyHelper isn't enough.
- [Remote sudo access paths](leaves/remote-sudo-access-paths.md) — when Claude needs sudo on a remote host (user-A ssh → work in user-B's space): NOPASSWD narrow scope (default) / Defaults timestamp_type=global (short window) / direct ssh as target; Claude's `!` shell has no TTY so interactive `sudo -v` won't work.
- [SSH ControlMaster group cache](leaves/ssh-controlmaster-group-cache.md) — after `usermod -aG` on a remote host, a "fresh" `ssh` still shows old groups because ControlMaster/ControlPersist reuses the master session; `ssh -O exit host` to refresh.

- [Tracker write token](leaves/tracker-write-token.md) — Startrek writes need `~/.tracker-token` (OAuth); `$OAUTH_TOKEN` is read-only (403 `startrek:write`); MCP tracker tools are read-only.
- [Auto-compaction threshold policy](leaves/autocompact-threshold-policy.md) — keep the autocompact trigger comfortably ABOVE the ~90–97k post-compaction floor (a trigger at the floor thrashes — DEEPAGENT-430); verified trigger ≈ round((window−20k)·0.8), frac 0.2 server-tunable; /context "buffer" (33k) is a display reserve, NOT window−trigger; current 210k window → trigger ~152k, min safe window ~210k; `apply-settings.sh` is additive so dropping a key doesn't clear it from live.
- [Policy effectiveness tracking](leaves/policy-effectiveness-tracking.md) — standing instrument (`scripts/policy-scorecard.py` + per-session ledger + weekly SessionStart nudge) tracking the model/sub-agent policy along efficiency (tokens/$/attention) and effectiveness (resolution proxies + batch manual rating); closes the "policy → measured outcome → adjustment" loop via a Flags-fire → self-improvement → record-movement procedure.

Workflow-level permissions (separate from memory): `~/claude-agent-instructions/permissions/` + `scripts/permissions-cli.py`. Not a memory leaf — operational config.

## Layers (ADR-0001 substrate)

- [Team layer sub-index](leaves/team/MEMORY.md) — scope/authority of the Team precedence layer (EDIT, shared via the project repo) in the `Core < Team < Personal` ladder; the precedence + replace-vs-merge contract is in `docs/architecture/instruction-layering.md`, the rebase/`rerere` maintenance recipe in `docs/operations/layer-maintenance.md`.

## Principles (ADR-0001 generality tier)

- [Principles sub-index](leaves/principles/MEMORY.md) — generality-graded, provenance-rooted, refutable principles induced from recurring difficulties; the planner retrieves them at a plan's `refutable principle` element (retrieval-augmented planning). The **generality≥1 profile** of one difficulty-record model whose generality-0 profile is the experience leaf (two physically-separate sub-indexes, one model). Schema: [principle-leaf-schema.md](leaves/principle-leaf-schema.md).

## System knowledge

- [System-knowledge sub-index](leaves/system-knowledge/MEMORY.md) — durable facts about systems, processes, org structure, codebase architecture that aren't self-evident; recording criteria in `~/.claude/CLAUDE.md` § Memory.

## Resolved-task experience

- [Experience sub-index](leaves/experience/MEMORY.md) — chronological log of resolved-task experience leaves (one per non-trivial task — final plan, difficulties, artifacts, lessons, self-critique, cost).
- [Experience leaf schema](leaves/experience-leaf-schema.md) — the `difficulty/v1` leaf schema (Difficulty / Order & criterion / Contexts / Cost) + `record-experience.py` tooling for resolved-task leaves; the **generality-0 profile** of one difficulty-record model whose generality≥1 profile is the principle leaf (`principle/v1`), keyed by an optional `generality` field (implied 0).
