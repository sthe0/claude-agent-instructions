# Typical coordinator pitfalls (parent agent)

## Metadata

| Field | Value |
|------|----------|
| `last_verified` | 2026-05-21 (closed-ticket gate) |
| `staleness_triggers` | new mandatory roles in CLAUDE.md; subagent renames |
| `revalidate` | last 3 parent transcripts: Task vs Edit/Write ratio; planner/developer/manager used |

## Anti-patterns

| Symptom | Better |
|---------|--------|
| Parent starts new task with Shell/planner/developer, no **manager** | **Task → manager** first on new goal |
| Parent does most edits via Shell/StrReplace | `Task` → **developer** (code), **manager** (stuck) |
| "Prefer" treated as optional for mandated workflow | Treat CLAUDE.md gates as hard requirements |
| Full pipeline rerun to debug one stage | Minimal retest; read local memory runbook |
| Second CLI binary for one-off experiment | Local script/stash; one entry point in repo |
| User feedback on process → only apology | **self-improvement** same turn |
| User asks "why no self-improvement?" / confirms "run it" | **self-improvement** same turn — reminder is feedback |
| Conditional "if not done" + artifact missing in repo → redo without status check | Read org runbook first; closed/done may mean stop — ask user |
| VCS branch/commit on scoped task without user ask | Read-only + report until user confirms scope |
| Unknown internal term → guess in code | infra consultant subagent if present in `~/.claude/agents/`, else intrasearch / domain MCP |
| Domain runbook pasted into manager/developer prompts | **memory** leaf + link in plan |

## Optional session metric

After long sessions (>10 tool calls), one line for the user:

`Delegation: Task=N; parent Edit/Write=M.`

Goal: lower M on ticket-sized tasks.
