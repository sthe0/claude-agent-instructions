# Claude / Cursor agent instructions

Git-репозиторий инструкций. **Правки здесь сразу видны** Cursor и Claude Code через симлинки в `~/.claude/`.

## Симлинки (основной режим)

| Репозиторий | Симлинк |
|-------------|---------|
| `agents/` | `~/.claude/agents` |
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `cursor-rules/claude-code-sync.mdc` | `~/.cursor/rules/claude-code-sync.mdc` |
| `memory-meta/INDEX.md` | `~/.claude/memory/INDEX.md` |
| `memory-meta/README.md` | `~/.claude/memory/README.md` |

`~/.cursor/agents` → `~/.claude/agents` (как было).

Первичная настройка на новой машине:

```bash
git clone <remote> ~/claude-agent-instructions   # или уже есть локально
~/claude-agent-instructions/scripts/setup-symlinks.sh
```

Повторный запуск `setup-symlinks.sh` безопасен (`ln -sfn`).

## Workflow

```bash
cd ~/claude-agent-instructions
$EDITOR agents/memory.md    # или правка через ~/.claude/agents — то же самое
git add -A && git diff --staged && git commit -m "..."
```

**Агент коммитит автоматически** после любой правки в этом репо (своей или пользователя) — не ждёт «можно закоммитить?». Одна логическая правка — один commit.

Копирование **не нужно**: `~/.claude/agents/foo.md` — это файл в репозитории.

## Что не в git

- `~/.claude/memory/deepagent/` и другие leaf-факты
- `~/.claude/skills/` — симлинки в Arcadia; актуальный список: `ls -la ~/.claude/skills` (см. [docs/skills-symlinks.md](docs/skills-symlinks.md))
- `settings.json`, sessions, plugins cache

## Агенты

| name | Файл | Роль |
|------|------|------|
| manager | `agents/manager.md` | Координация задач и субагентов |
| memory | `agents/memory.md` | `~/.claude/memory/` |
| self-improvement | `agents/self-improvement.md` | Улучшение системы, этот репозиторий |
| planner, thinker, yandex-developer, logos-* | `agents/*.md` | Специализации |

## Устаревшие скрипты

`install-to-home.sh` и `collect-from-home.sh` — для режима **копирования**; при симлинках не используются.
