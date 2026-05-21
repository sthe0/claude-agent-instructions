# Deprecated: memory-meta

Память разделена:

| Было | Стало |
|------|--------|
| `memory-meta/` (git) | **`memory-global/`** в `~/claude-agent-instructions` → `~/.claude/memory-global/` |
| `~/.claude/memory/deepagent`, claude-code | **`junk/the0/agents/memory-local/`** → `~/.claude/memory/` |

Обнови симлинки: `scripts/setup-symlinks.sh`.

Старые пути в этом каталоге оставлены для ссылок из истории; не добавляй новые leaf сюда.
