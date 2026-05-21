# Claude / Cursor agent instructions

Единый git-репозиторий глобальных инструкций для **Claude Code** и **Cursor**. Правки в репо сразу видны в обоих IDE через симлинки в `~/.claude/` и `~/.cursor/`.

Подробная схема: [memory-meta/claude-code/claude-cursor-instructions.md](memory-meta/claude-code/claude-cursor-instructions.md).

## Быстрый старт

```bash
git clone git@github.com:sthe0/claude-agent-instructions.git ~/claude-agent-instructions
~/claude-agent-instructions/scripts/setup-symlinks.sh
~/claude-agent-instructions/scripts/verify-instructions-sync.sh
```

Повторный `setup-symlinks.sh` безопасен (`ln -sfn`).

## Симлинки

| Файл / каталог в репо | Куда линкуется |
|----------------------|----------------|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `agents/*.md` | `~/.claude/agents/<name>.md` |
| `agents-local/*.md` (если есть) | `~/.claude/agents/<name>.md` |
| `cursor-rules/claude-code-sync.mdc` | `~/.cursor/rules/claude-code-sync.mdc` |
| `memory-meta/INDEX.md`, `README.md` | `~/.claude/memory/` |
| — | `~/.cursor/agents` → `~/.claude/agents` |

Копирование в home **не используется** — только симлинки.

Проект `robot/deepagent`: overlay [cursor-rules/project-overlay-deepagent.mdc](cursor-rules/project-overlay-deepagent.mdc) → `.cursor/rules/deepagent-project.mdc` (настраивает `setup-symlinks.sh`).

## Скрипты

| Скрипт | Назначение |
|--------|------------|
| [setup-symlinks.sh](scripts/setup-symlinks.sh) | Симлинки Claude + Cursor; overlay deepagent |
| [verify-instructions-sync.sh](scripts/verify-instructions-sync.sh) | Проверка симлинков и drift |
| [sync-instructions-repo.sh](scripts/sync-instructions-repo.sh) | `pull` / `push` / `sync` / `status` |
| [install-git-hooks.sh](scripts/install-git-hooks.sh) | `post-commit` → auto-push |
| [install-sync-cron.sh](scripts/install-sync-cron.sh) | Cron: pull каждые 10 мин |
| [install-sync-systemd-timer.sh](scripts/install-sync-systemd-timer.sh) | То же через user systemd, если cron недоступен |

## Git workflow (агент и человек)

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull   # перед правкой
# правки в ~/claude-agent-instructions/
git add -A && git diff --staged && git commit -m "…"
# push: post-commit hook или явно:
~/claude-agent-instructions/scripts/sync-instructions-repo.sh push
```

Runbook: [memory-meta/claude-code/instructions-git-sync.md](memory-meta/claude-code/instructions-git-sync.md).

## Агенты в репозитории (`agents/`)

| name | Роль |
|------|------|
| **manager** | Координация, затруднения, разбор сессий |
| **planner** | Декомпозиция Tracker-тикетов |
| **yandex-developer** | Код в Arcadia |
| **yandex-cloud-expert** | Yandex Cloud |
| **thinker** | Проверка рассуждений |
| **memory** | `~/.claude/memory/` |
| **self-improvement** | Улучшение инструкций и процесса |

Делегирование: `Task` с `subagent_type` = `name` из frontmatter. Глобальная политика — [CLAUDE.md](CLAUDE.md).

## Локальные агенты (`agents-local/`)

Каталог **в `.gitignore`** (кроме [agents-local/README.md](agents-local/README.md)). Для промптов, которые не должны быть в общем git (другая машина, другой набор агентов).

На отдельных машинах здесь могут лежать дополнительные `*.md` (например Logos ETL) — см. README в каталоге. После добавления файлов: `setup-symlinks.sh`.

## Что не в этом репозитории

| Что | Где |
|-----|-----|
| Доменная memory (deepagent, runbook'и) | `~/.claude/memory/` — leaf вне git |
| Скиллы | `~/.claude/skills/` — симлинки в Arcadia ([docs/skills-symlinks.md](docs/skills-symlinks.md)) |
| Сессии, plugins cache, `settings.json` | `~/.claude/` локально |

## Поддержка README в актуальном состоянии

При изменении структуры репозитория (новый скрипт, агент, симлинк, overlay) **обнови этот README в том же commit**, что и сами файлы. Проверка: оглавление скриптов совпадает с `ls scripts/`; таблица агентов — с `ls agents/*.md`; нет ссылок на удалённые пути.

Агенты после правок инструкций: `pull` → правка → `commit` → `push` (см. [CLAUDE.md](CLAUDE.md)).
