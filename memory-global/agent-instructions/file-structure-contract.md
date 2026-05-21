# Контракт файловой структуры (агентские инструкции)

**Каноническое описание.** При расхождении с диском — исправь **либо** этот документ и связанные README, **либо** дерево файлов и симлинки. Не оставляй устаревшее описание.

См. также: [runtime-layout.md](runtime-layout.md) (runtime-пути), [../../README.md](../../README.md) § «Кооперация агентов».

## Метаданные

| Поле | Значение |
|------|----------|
| `last_verified` | 2026-05-21 |
| `staleness_triggers` | новый каталог в git/arc инструкций; смена `setup-symlinks.sh`; перенос скриптов между global/local |
| `revalidate` | `~/claude-agent-instructions/scripts/verify-layout-contract.sh`; `verify-instructions-sync.sh` |

## Слои

| Слой | Версионирование | Описание дерева |
|------|-----------------|-----------------|
| **Глобальный** | git `~/claude-agent-instructions` | этот файл, § Global tree |
| **Локальный** | arc (ветка на машине) | `~/.claude/memory/INDEX.md` → leaf `the0-agents-mount`; `~/.claude/scripts-local/README.md` |

Глобальные промпты **не** ссылаются на пути arc junk — только на runtime (`~/.claude/...`).

## Global tree (`~/claude-agent-instructions/`)

```
CLAUDE.md
README.md
agents/*.md              # глобальные субагенты (developer, manager, …)
agents-local/README.md   # указатель на локальный arc, без *.md агентов
cursor-rules/
  claude-code-sync.mdc
  project-overlay-deepagent.mdc
memory-global/
  INDEX.md, README.md
  agent-instructions/    # runtime-layout, file-structure-contract, instructions-git-sync
  development/
memory-meta/README.md    # deprecated, не добавлять leaf
scripts/
  setup-symlinks.sh
  verify-instructions-sync.sh
  verify-layout-contract.sh
  sync-instructions-repo.sh
  install-git-hooks.sh
  install-sync-cron.sh
  install-sync-systemd-timer.sh
githooks/post-commit
docs/                    # опционально
```

**Запрещено в global `scripts/`:** arc-скрипты (`sync-junk-agents-arc`, `junk-agents-arc-commit`, `setup-the0-agents-mount`, …) — только в локальном `scripts/`.

## Runtime symlinks (после `setup-symlinks.sh`)

| Runtime | Источник (логический) |
|---------|------------------------|
| `~/.claude/CLAUDE.md` | `CLAUDE.md` |
| `~/.claude/agents/<global>.md` | `agents/<name>.md` |
| `~/.claude/agents/<local>.md` | локальный `agents-local/` (arc) |
| `~/.claude/memory-global/` | `memory-global/` |
| `~/.claude/memory/` | локальный `memory-local/` (arc) |
| `~/.claude/scripts-local/` | локальный `scripts/` (arc) |
| `~/.cursor/rules/claude-code-sync.mdc` | `cursor-rules/claude-code-sync.mdc` |
| `~/.cursor/agents` | `~/.claude/agents` |

## Local tree (arc, не в git инструкций)

Описание на диске машины (типовое):

```
junk/the0/agents/
  README.md
  agents-local/*.md
  memory-local/
    INDEX.md, README.md
    deepagent/, claude-code/, yandex/
  scripts/
    README.md
    setup-the0-agents-mount.sh
    sync-junk-agents-arc.sh
    junk-agents-arc-commit.sh
    install-junk-agents-sync-cron.sh
    verify-the0-agents-sync.sh
```

Runtime только через `~/.claude/memory/`, `~/.claude/scripts-local/`.

## Обязанность агента

### При изменении структуры

Любое добавление/перенос/удаление каталога, скрипта, split global/local:

1. Обнови **этот файл** (и `runtime-layout.md`, если меняются runtime-пути).
2. Обнови **README.md** § симлинки/скрипты и § «Поддержка README».
3. Локальный слой — leaf в `~/.claude/memory/` или `scripts/README.md` в arc; commit arc.
4. Глобальный слой — commit + push git.
5. Запусти `verify-layout-contract.sh` и `verify-instructions-sync.sh`.

### Регулярная сверка

| Когда | Действие |
|-------|----------|
| После правок в `~/claude-agent-instructions/` или локальном arc | `verify-layout-contract.sh` |
| Раз в несколько недель / по запросу пользователя | полная сверка: контракт ↔ `ls`/`readlink` ↔ INDEX |
| Расхождение | исправить документ **или** дерево; не оба вразнобой |

Родитель и **self-improvement** при рефакторинге инструкций включают сверку в Definition of Done.

### Расхождение: что править

| Симптом | Скорее всего |
|---------|----------------|
| Файл есть, в контракте нет | дополнить контракт (если намеренно) или удалить лишнее |
| В контракте есть, на диске нет | восстановить файл или убрать из контракта |
| Симлинк не на тот target | `setup-symlinks.sh` |
| arc-скрипт в global git | перенести в local `scripts/` |
