# Claude / Cursor agent instructions

Единый git-репозиторий **глобальных** инструкций для **Claude Code** и **Cursor**. Правки в репо видны в обоих IDE через симлинки в `~/.claude/` и `~/.cursor/`.

Контракт файловой структуры: [memory-global/agent-instructions/file-structure-contract.md](memory-global/agent-instructions/file-structure-contract.md). Runtime-пути: [runtime-layout.md](memory-global/agent-instructions/runtime-layout.md).

## Кооперация агентов

> **Этот раздел — конспект живой модели кооперации.** При любом изменении ролей, обязательных gate или порядка делегирования обновляй его **в том же commit**, что `CLAUDE.md`, `agents/*.md` и `cursor-rules/claude-code-sync.mdc`. Детали — в [CLAUDE.md](CLAUDE.md).

### Понятия

| Понятие | Смысл |
|---------|--------|
| **Родительский агент** | Диалог в Cursor / Claude Code: делегирует, не подменяет специалистов |
| **Субагент** | Промпт в `~/claude-agent-instructions/agents/` или дополнительный файл в `~/.claude/agents/`; вызов `Task`, `subagent_type: <name>` |
| **Memory (глобальная)** | `~/.claude/memory-global/` — как думать, координация, git sync |
| **Memory (локальная)** | `~/.claude/memory/INDEX.md` — runbook'и продукта и среды (вне этого git) |
| **Инструкции** | Этот репо → `~/.claude/CLAUDE.md` |

### Принципы

1. **Обязательность важнее «prefer».** Согласование плана, self-improvement при обратной связи, manager при затруднении (детали тикетного workflow — [CLAUDE.md](CLAUDE.md)).
2. **Понять → согласовать → делать.** План до **developer**, кроме явного «делай сразу».
3. **Код по тикету — developer** в изолированной рабочей копии VCS, не родитель в общей default-копии (см. [CLAUDE.md](CLAUDE.md)).
4. **Затруднение — manager** в том же ходе.
5. **Обратная связь — self-improvement** в том же ходе.
6. **Runbook'и — в memory INDEX**, не в промпты generic-агентов.
7. **Контракт файловой структуры** — описание global/local деревьев актуально; после изменений — `verify-layout-contract.sh`; расхождение → правка docs или диска.

### Типовые потоки

```text
Задача с планом: понимание → planner → согласование → developer
Затруднение: … → manager → (planner | developer | memory | thinker | опциональные ~/.claude/agents/)
```

Глобальные антипаттерны: [memory-global/development/typical-coordinator-pitfalls.md](memory-global/development/typical-coordinator-pitfalls.md).

## Быстрый старт

```bash
git clone git@github.com:sthe0/claude-agent-instructions.git ~/claude-agent-instructions
~/claude-agent-instructions/scripts/setup-symlinks.sh
~/claude-agent-instructions/scripts/verify-instructions-sync.sh
```

Опциональная локальная конфигурация (доп. агенты, memory, скрипты): `~/.claude/memory/INDEX.md` после `setup-symlinks.sh`.

## Симлинки (глобальное из git)

| В репо | Runtime |
|--------|---------|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `agents/*.md` | `~/.claude/agents/<name>.md` |
| `memory-global/` | `~/.claude/memory-global/` |
| `cursor-rules/claude-code-sync.mdc` | `~/.cursor/rules/` |
| — | `~/.cursor/agents` → `~/.claude/agents` |

Локальные `~/.claude/memory/` и `~/.claude/scripts-local/` **не** в этом git — источник настраивается на машине (`setup-symlinks.sh`).

## Скрипты (глобальные, git)

| Скрипт | Назначение |
|--------|------------|
| [setup-symlinks.sh](scripts/setup-symlinks.sh) | Симлинки Claude + Cursor (+ локальные runtime-пути) |
| [verify-instructions-sync.sh](scripts/verify-instructions-sync.sh) | Проверка глобальных симлинков; делегирует local verify |
| [verify-layout-contract.sh](scripts/verify-layout-contract.sh) | Сверка дерева с file-structure-contract.md |
| [sync-instructions-repo.sh](scripts/sync-instructions-repo.sh) | `pull` / `push` этого репо |
| [install-git-hooks.sh](scripts/install-git-hooks.sh) | post-commit → push |
| [install-sync-cron.sh](scripts/install-sync-cron.sh) | Cron: git pull /10 min |

Локальные скрипты: `~/.claude/scripts-local/` (см. README в этом каталоге после `setup-symlinks.sh`).

## Git workflow

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
# правки → commit → push (post-commit hook)
```

Runbook: [memory-global/agent-instructions/instructions-git-sync.md](memory-global/agent-instructions/instructions-git-sync.md).

## Агенты в репозитории (`agents/`)

| name | Файл |
|------|------|
| manager | [agents/manager.md](agents/manager.md) |
| planner | [agents/planner.md](agents/planner.md) |
| developer | [agents/developer.md](agents/developer.md) |
| thinker | [agents/thinker.md](agents/thinker.md) |
| memory | [agents/memory.md](agents/memory.md) |
| self-improvement | [agents/self-improvement.md](agents/self-improvement.md) |
| yandex-cloud-expert | [agents/yandex-cloud-expert.md](agents/yandex-cloud-expert.md) |

Дополнительные субагенты — только файлы в `~/.claude/agents/`, которых нет в `agents/` этого репо.

## Что не в этом репозитории

| Что | Где искать |
|-----|------------|
| Локальная memory | `~/.claude/memory/INDEX.md` |
| Доп. агенты | `~/.claude/agents/` |
| Локальные скрипты | `~/.claude/scripts-local/` |
| Скиллы | `~/.claude/skills/` |

## Поддержка README

При изменении модели — обнови § «Кооперация агентов», [CLAUDE.md](CLAUDE.md) и затронутые `agents/*.md` в **одном commit**.

При изменении **каталогов, скриптов, симлинков** (global или local):

1. Обнови [file-structure-contract.md](memory-global/agent-instructions/file-structure-contract.md) и при необходимости [runtime-layout.md](memory-global/agent-instructions/runtime-layout.md).
2. Сверь § симлинки/скрипты в этом README с фактом.
3. Запусти `scripts/verify-layout-contract.sh` и `verify-instructions-sync.sh`.
4. Локальный слой — по runbook в `~/.claude/memory/INDEX.md` и `~/.claude/scripts-local/`.
