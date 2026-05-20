# Memory index

Краткая карта. Детали — в linked-файлах.

## deepagent (robot/deepagent)

| Тема | Файл |
|------|------|
| Prod training dataset: пайплайн, top50, instruct, повторения, 367/415 | [deepagent/datasets-prod-pipeline.md](deepagent/datasets-prod-pipeline.md) |
| Термин **dsv3** и суффиксы в путях YT | [deepagent/dsv3-judge-naming.md](deepagent/dsv3-judge-naming.md) |
| Eliza/Zeliboba: где смотреть LLM (бесплатные коммунальные vs платные внешние) | [deepagent/eliza-and-zeliboba-models.md](deepagent/eliza-and-zeliboba-models.md) |
| test_quality: полный vs минимальный ретест (eval_baskets / compute_metrics) | [deepagent/test-quality-retest.md](deepagent/test-quality-retest.md) |

## claude-code (настройка агента)

| Тема | Файл |
|------|------|
| Параллельные ветки в `arc` через несколько маунтов (нет worktree, есть workaround + плагин edadeal) | [claude-code/arc-parallel-mounts.md](claude-code/arc-parallel-mounts.md) |
| PreToolUse-хук в `~/.claude/settings.json`: word boundaries, ложные срабатывания на `~/arcadia_*` | [claude-code/hook-word-boundaries.md](claude-code/hook-word-boundaries.md) |

## Meta

| Тема | Файл |
|------|------|
| Как устроена память (в т.ч. актуальность, revalidate) | [README.md](README.md) |
| Git-репозиторий промптов агентов (`~/claude-agent-instructions`) | вне memory — см. README репозитория |
