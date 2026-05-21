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

1. **Обязательность важнее «prefer».** Tracker-тикет, mount, согласование плана, self-improvement, manager при затруднении.
2. **Понять → согласовать → делать.** План до **developer**, кроме «делай сразу».
3. **Код тикета — developer** в `~/arcadia_<TICKET>-*`, не родитель в `~/arcadia`.
4. **Затруднение — manager** в том же ходе.
5. **Обратная связь — self-improvement** в том же ходе.
6. **Runbook'и — в memory INDEX**, не в промпты generic-агентов.
7. **Контракт файловой структуры** — описание global/local деревьев актуально; после изменений — `verify-layout-contract.sh`; расхождение → правка docs или диска.

### Типовые потоки

```text
Tracker-тикет: понимание → planner → согласование → mount → developer
Затруднение: … → manager → (planner | developer | memory | thinker | опциональные ~/.claude/agents/)
```

Глобальные антипаттерны: [memory-global/development/typical-coordinator-pitfalls.md](memory-global/development/typical-coordinator-pitfalls.md).

## Быстрый старт

```bash
git clone git@github.com:sthe0/claude-agent-instructions.git ~/claude-agent-instructions
~/claude-agent-instructions/scripts/setup-symlinks.sh
~/claude-agent-instructions/scripts/verify-instructions-sync.sh
```

Локальные агенты, memory и скрипты — arc-ветка `the0-agents`, runtime: `~/.claude/memory/`, `~/.claude/scripts-local/` (см. local INDEX / `scripts/README` в маунте).

## Симлинки (глобальное из git)

| В репо | Runtime |
|--------|---------|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `agents/*.md` | `~/.claude/agents/<name>.md` |
| `memory-global/` | `~/.claude/memory-global/` |
| `cursor-rules/claude-code-sync.mdc` | `~/.cursor/rules/` |
| — | `~/.cursor/agents` → `~/.claude/agents` |

Локальная `~/.claude/memory/` **не** в этом git — источник задаётся на машине.

## Скрипты (глобальные, git)

| Скрипт | Назначение |
|--------|------------|
| [setup-symlinks.sh](scripts/setup-symlinks.sh) | Симлинки Claude + Cursor (+ `~/.claude/scripts-local`) |
| [verify-instructions-sync.sh](scripts/verify-instructions-sync.sh) | Проверка глобальных симлинков; делегирует local verify |
| [verify-layout-contract.sh](scripts/verify-layout-contract.sh) | Сверка дерева с file-structure-contract.md |
| [sync-instructions-repo.sh](scripts/sync-instructions-repo.sh) | `pull` / `push` этого репо |
| [install-git-hooks.sh](scripts/install-git-hooks.sh) | post-commit → push |
| [install-sync-cron.sh](scripts/install-sync-cron.sh) | Cron: git pull /10 min |

Локальные (arc): `~/.claude/scripts-local/` — `sync-junk-agents-arc.sh`, `junk-agents-arc-commit.sh`, …

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

Дополнительные субагенты (инфра-консультант, ETL и т.д.) — только в `~/.claude/agents/`, если настроены на машине.

## Что не в этом репозитории

| Что | Где искать |
|-----|------------|
| Локальная memory | `~/.claude/memory/INDEX.md` |
| Доп. агенты | `~/.claude/agents/` (лишние к глобальным) |
| Скиллы | `~/.claude/skills/` |

## Поддержка README

При изменении модели — обнови § «Кооперация агентов», [CLAUDE.md](CLAUDE.md) и затронутые `agents/*.md` в **одном commit**.

При изменении **каталогов, скриптов, симлинков** (global или local):

1. Обнови [file-structure-contract.md](memory-global/agent-instructions/file-structure-contract.md) и при необходимости [runtime-layout.md](memory-global/agent-instructions/runtime-layout.md).
2. Сверь § симлинки/скрипты в этом README с фактом.
3. Запусти `scripts/verify-layout-contract.sh` и `verify-instructions-sync.sh`.
4. Локальный arc — `~/.claude/scripts-local/junk-agents-arc-commit.sh` после правок в маунте.
