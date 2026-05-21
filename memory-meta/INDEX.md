# Memory index

Краткая карта. Детали — в linked-файлах.

## deepagent (robot/deepagent)

| Тема | Файл |
|------|------|
| Prod training dataset: пайплайн, top50, instruct, повторения, 367/415 | [deepagent/datasets-prod-pipeline.md](deepagent/datasets-prod-pipeline.md) |
| Термин **dsv3** и суффиксы в путях YT | [deepagent/dsv3-judge-naming.md](deepagent/dsv3-judge-naming.md) |
| Eliza/Zeliboba: где смотреть LLM (бесплатные коммунальные vs платные внешние) | [deepagent/eliza-and-zeliboba-models.md](deepagent/eliza-and-zeliboba-models.md) |
| test_quality: полный eval, ретест compute_metrics (без launcher в Arc) | [deepagent/test-quality-retest.md](deepagent/test-quality-retest.md) |
| DEEPAGENT-416: OOM compute_metrics, ошибки агента, эфемерный ретест | [deepagent/compute-metrics-oom-de416.md](deepagent/compute-metrics-oom-de416.md) |
| Nirvana TTL (deepagent): см. [claude-code/nirvana-vh3-ttl-layers.md](claude-code/nirvana-vh3-ttl-layers.md); точка входа `_build_eval_graph` | [deepagent/nirvana-ttl-retention.md](deepagent/nirvana-ttl-retention.md) |
| train-eval-meta: republish vs convert-only vs полный meta (не перезапускать train) | [deepagent/train-eval-meta-relaunch.md](deepagent/train-eval-meta-relaunch.md) |

## claude-code (настройка агента)

| Тема | Файл |
|------|------|
| Параллельные маунты arc: `cd ~` перед mount, фон + `[mounted]` | [claude-code/arc-parallel-mounts.md](claude-code/arc-parallel-mounts.md) |
| Инструкции агента: автоматический `git commit` в `~/claude-agent-instructions/` | [claude-code/instructions-git-commit.md](claude-code/instructions-git-commit.md) |
| PreToolUse-хук: word boundaries, ложные срабатывания на `~/arcadia_*` | [claude-code/hook-word-boundaries.md](claude-code/hook-word-boundaries.md) |
| Nirvana WI: опрос статуса, несколько графов, handoff «мониторинг завершён» | [claude-code/nirvana-wi-watch.md](claude-code/nirvana-wi-watch.md) |
| Nirvana/VH3: три слоя TTL (WI retention vs operation job vs wait) | [claude-code/nirvana-vh3-ttl-layers.md](claude-code/nirvana-vh3-ttl-layers.md) |
