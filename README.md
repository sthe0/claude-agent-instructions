# Claude / Cursor agent instructions

Версионируемый набор инструкций для агентов: промпты, глобальный `CLAUDE.md`, sync-правило Cursor, оглавление memory (без leaf-фактов).

**Live-пути** (куда ставит `scripts/install-to-home.sh`):

| В репозитории | Куда |
|---------------|------|
| `agents/` | `~/.claude/agents/` |
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `cursor-rules/claude-code-sync.mdc` | `~/.cursor/rules/claude-code-sync.mdc` |
| `memory-meta/` | справочно; leaf-факты остаются в `~/.claude/memory/` |

`~/.cursor/agents` — симлинк на `~/.claude/agents` (не трогаем).

## Workflow

```bash
# правки в репозитории
cd ~/claude-agent-instructions
$EDITOR agents/memory.md
git diff && git commit -am "memory agent: clarify INDEX updates"

# выкат в live
./scripts/install-to-home.sh

# подтянуть изменения, сделанные вручную в ~/.claude
./scripts/collect-from-home.sh
git diff && git commit -am "sync from live"
```

## Что не в git

- `~/.claude/memory/deepagent/*.md` и прочие leaf-факты (могут содержать внутренние ссылки; отдельное решение позже)
- `~/.claude/skills/` — симлинки в Arcadia; см. `docs/skills-symlinks.txt`
- `settings.json`, sessions, plugins cache

## Агенты

| name | Файл | Роль |
|------|------|------|
| manager | `agents/manager.md` | Координация задач и субагентов |
| memory | `agents/memory.md` | `~/.claude/memory/` |
| self-improvement | `agents/self-improvement.md` | Улучшение системы, этот репозиторий |
| planner, thinker, yandex-developer, logos-* | `agents/*.md` | Специализации |

Инициатива git-репозитория — пример улучшения от **self-improvement**.
