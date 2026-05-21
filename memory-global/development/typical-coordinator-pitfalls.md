# Typical coordinator pitfalls (parent agent)

## Метаданные

| Поле | Значение |
|------|----------|
| `last_verified` | 2026-05-21 |
| `staleness_triggers` | новые обязательные роли в CLAUDE.md; смена имён субагентов |
| `revalidate` | 3 последних parent transcripts: Task vs Edit/Write ratio; были ли planner/developer/manager |

## Anti-patterns

| Symptom | Better |
|---------|--------|
| Parent does most edits via Shell/StrReplace | `Task` → **developer** (code), **manager** (stuck) |
| «Prefer» treated as optional for mandated workflow | Treat CLAUDE.md gates as hard requirements |
| Full pipeline rerun to debug one stage | Minimal retest; read local memory runbook |
| Second CLI binary for one-off experiment | Local script/stash; one entry point in repo |
| User feedback on process → only apology | **self-improvement** same turn |
| Unknown internal term → guess in code | **yandex-guru** or intrasearch / deepagent MCP |
| Domain runbook pasted into manager/developer prompts | **memory** leaf + link in plan |

## Optional session metric

After long sessions (>10 tool calls), one line for the user:

`Delegation: Task=N; parent Edit/Write=M.`

Goal: lower M on ticket-sized tasks.
