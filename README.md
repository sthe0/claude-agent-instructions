# Claude / Cursor agent instructions

Git-репозиторий инструкций. **Правки здесь сразу видны** в Claude Code и Cursor через симлинки (`~/.claude/`, `~/.cursor/`). См. [memory-meta/claude-code/claude-cursor-instructions.md](memory-meta/claude-code/claude-cursor-instructions.md).

## Симлинки (основной режим)

| Репозиторий | Симлинк |
|-------------|---------|
| `agents/*.md` + `agents-local/*.md` | `~/.claude/agents/<name>.md` (по файлу) |
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `cursor-rules/claude-code-sync.mdc` | `~/.cursor/rules/claude-code-sync.mdc` |
| `~/.cursor/agents` | → `~/.claude/agents` (каталог) |
| `memory-meta/INDEX.md` | `~/.claude/memory/INDEX.md` |
| `memory-meta/README.md` | `~/.claude/memory/README.md` |

Проверка симлинков: `scripts/verify-instructions-sync.sh`.

Первичная настройка на новой машине:

```bash
git clone git@github.com:sthe0/claude-agent-instructions.git ~/claude-agent-instructions
~/claude-agent-instructions/scripts/setup-symlinks.sh
```

Повторный запуск `setup-symlinks.sh` безопасен (`ln -sfn`).

## Workflow

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull   # перед правкой
cd ~/claude-agent-instructions
$EDITOR agents/memory.md    # или правка через ~/.claude/agents — то же самое
git add -A && git diff --staged && git commit -m "..."
~/claude-agent-instructions/scripts/sync-instructions-repo.sh push   # после каждого commit
```

**Агент:** pull → правка → commit → push (без запроса). Одна логическая правка — один commit.

**Фон:** `scripts/install-sync-cron.sh` — pull каждые 10 минут; `scripts/install-git-hooks.sh` — push после commit. См. [memory-meta/claude-code/instructions-git-sync.md](memory-meta/claude-code/instructions-git-sync.md).

Копирование **не нужно**: `~/.claude/agents/foo.md` — симлинк на файл в `agents/` или `agents-local/`.

## Что не в git

- `agents-local/*.md` (кроме `agents-local/README.md`) — машино-специфичные агенты; на этой машине сюда входят **logos-***
- `~/.claude/memory/deepagent/` и другие leaf-факты
- `~/.claude/skills/` — симлинки в Arcadia; актуальный список: `ls -la ~/.claude/skills` (см. [docs/skills-symlinks.md](docs/skills-symlinks.md))
- `settings.json`, sessions, plugins cache

## Агенты

| name | Файл | Роль |
|------|------|------|
| manager | `agents/manager.md` | Координация задач и субагентов |
| memory | `agents/memory.md` | `~/.claude/memory/` |
| self-improvement | `agents/self-improvement.md` | Улучшение системы, этот репозиторий |
| planner, thinker, yandex-developer, yandex-cloud-expert | `agents/*.md` | Специализации (git) |
| logos-* | `agents-local/logos-*.md` | Logos ETL (локально) |

## Устаревшие скрипты

`install-to-home.sh` и `collect-from-home.sh` — для режима **копирования**; при симлинках не используются.
