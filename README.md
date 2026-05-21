# Claude / Cursor agent instructions

Единый git-репозиторий глобальных инструкций для **Claude Code** и **Cursor**. Правки в репо сразу видны в обоих IDE через симлинки в `~/.claude/` и `~/.cursor/`.

Подробная схема файлов: [memory-meta/claude-code/claude-cursor-instructions.md](memory-meta/claude-code/claude-cursor-instructions.md).

## Кооперация агентов

> **Этот раздел — конспект живой модели кооперации.** При любом изменении ролей, обязательных gate или порядка делегирования обновляй его **в том же commit**, что `CLAUDE.md`, `agents/*.md` и `cursor-rules/claude-code-sync.mdc`. Детали и edge cases — в [CLAUDE.md](CLAUDE.md); здесь только устойчивые идеи.

### Понятия

| Понятие | Смысл |
|---------|--------|
| **Родительский агент** | Диалог в Cursor / Claude Code: принимает запрос пользователя, делегирует, не подменяет специалистов |
| **Субагент** | Отдельный промпт в `agents/<name>.md`; вызывается через `Task`, `subagent_type: <name>` |
| **Делегирование** | Передача подзадачи с явным prompt: контекст, ожидаемый output, ограничения |
| **Memory (глобальная)** | `~/.claude/memory-global/` — git, практики рассуждения и кросс-проектные выводы |
| **Memory (локальная)** | `~/.claude/memory/` — Arcadia `junk/the0/agents/memory-local/`, runbook'и продукта и Яндекса |
| **Инструкции** | Этот git-репо: политика, роли, симлинки; канон — `CLAUDE.md` |

### Идея

Один пользовательский запрос разбивается на **узкие роли**, каждая со своим промптом. Родитель **координирует**, а не «делает всё сам» инструментами Shell/Edit там, где уже есть субагент.

Знания разделены по слоям:

- **Поведение всех сессий** → `CLAUDE.md` + `cursor-rules/claude-code-sync.mdc`
- **Роль одного субагента** → `agents/<name>.md`
- **Конкретный домен (deepagent, Nirvana, arc)** → локальный leaf в `~/.claude/memory/`
- **Как думать / типовые ошибки координатора** → `~/.claude/memory-global/`

### Принципы

1. **Обязательность важнее «prefer».** Tracker-тикет, mount, согласование плана, self-improvement при обратной связи, manager при затруднении — не опциональные советы.
2. **Понять → согласовать → делать.** Числа и сроки в тикете без источника — вопрос или исследование **до** правок кода. План показывается пользователю до **developer** (кроме явного «делай сразу»).
3. **Код тикета — только developer** в параллельном маунте `~/arcadia_<TICKET>-*`, не родитель в `~/arcadia`.
4. **Затруднение — manager, не бесконечный retry родителя.** Повторная ошибка, блокер, расхождение с планом, 2+ корректировки процесса, новая попытка Nirvana/arc после провала → `Task` → **manager** (цикл: исследование → критика → переплан → действие). Мульти-тикет / сложная координация → **manager до planner**.
5. **Обратная связь о процессе — self-improvement в том же ходе**, до финального ответа пользователю; не только извинение и тактический фикс.
6. **Простое решение предпочтительнее.** Минимальный ретест вместо полного пайплайна; один CLI entry point вместо дублирующих скриптов; расширение существующего кода вместо нового, если уместно.
7. **Домен не вшивать в manager/planner/developer.** Runbook'и — в локальную memory; рассуждения — в memory-global; в агентах — ссылки на INDEX.

### Типовые потоки

```text
Обычный Tracker-тикет (один ключ, без затруднений):
  понимание → planner → согласование с пользователем → mount → developer

Несколько тикетов / сложная координация / затруднение:
  … → manager → (planner | developer | yandex-guru | memory | thinker)

Корректировка пользователя («не так», «зачем», «лучше X»):
  self-improvement (+ при необходимости manager, если блокер по задаче)

Вопрос по инфраструктуре / неизвестный термин:
  deepagent MCP (не субагент)
```

После запуска Nirvana WI родитель **сам** ведёт опрос до терминала (таблица «мониторинг завершён»); доменные процедуры ретеста — из memory.

### Роли (кратко)

| Субагент | Когда |
|----------|--------|
| **manager** | Затруднения, мультишаг, разбор сессий, маршрутизация |
| **planner** | План по Tracker-тикету |
| **developer** | Production-код (любой стек; в Arcadia — по политике mount) |
| **yandex-guru** | Консультации по инфраструктуре Яндекса (локальный агент) |
| **thinker** | Сомнительная цепочка выводов |
| **memory** | Запомнить/найти доменный факт |
| **self-improvement** | Улучшить инструкции после обратной связи |

Полный список глобальных агентов — таблица ниже. Локальные (`yandex-guru`, `logos-*`) — `~/arcadia/junk/the0/agents/agents-local/`.

### Антипаттерны

- Родитель пишет production-код и обходит mount / planner / согласование.
- Shell/Grep/transcripts вместо `Task` → manager при застое.
- Правка `manager.md` вместо вызова manager или self-improvement.
- Полный перезапуск train→eval при отладке одного кубика.
- Дублирование глобальной политики в project `.cursor/rules` (только overlay).

Runbook типовых ошибок (локально): `~/.claude/memory/claude-code/session-retrospective-2026-05.md`. Глобальные практики: `memory-global/development/`.

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
| `junk/the0/agents/agents-local/*.md` | `~/.claude/agents/<name>.md` |
| `cursor-rules/claude-code-sync.mdc` | `~/.cursor/rules/claude-code-sync.mdc` |
| `memory-global/` | `~/.claude/memory-global/` |
| `junk/the0/agents/memory-local/` | `~/.claude/memory/` |
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

Роли и потоки — § [Кооперация агентов](#кооперация-агентов) выше.

| name | Файл |
|------|------|
| manager | [agents/manager.md](agents/manager.md) |
| planner | [agents/planner.md](agents/planner.md) |
| developer | [agents/developer.md](agents/developer.md) |
| yandex-cloud-expert | [agents/yandex-cloud-expert.md](agents/yandex-cloud-expert.md) |
| thinker | [agents/thinker.md](agents/thinker.md) |
| memory | [agents/memory.md](agents/memory.md) |
| self-improvement | [agents/self-improvement.md](agents/self-improvement.md) |

## Локальные агенты и память (эта машина)

Версионируются в Arcadia: **`~/arcadia/junk/the0/agents/`** (`agents-local/`, `memory-local/`). Симлинки настраивает `setup-symlinks.sh` (`JUNK_AGENTS_ROOT` при необходимости).

| name | Файл |
|------|------|
| yandex-guru | `junk/the0/agents/agents-local/yandex-guru.md` |
| logos-* | `junk/the0/agents/agents-local/logos-*.md` |

Каталог `agents-local/` в git репо инструкций — только [README-заглушка](agents-local/README.md); содержимое на диске — в junk.

## Что не в этом репозитории

| Что | Где |
|-----|-----|
| Локальная memory (deepagent, Nirvana, arc) | `junk/the0/agents/memory-local/` → `~/.claude/memory/` |
| Глобальная memory | `memory-global/` → `~/.claude/memory-global/` |
| Скиллы | `~/.claude/skills/` — симлинки в Arcadia ([docs/skills-symlinks.md](docs/skills-symlinks.md)) |
| Сессии, plugins cache, `settings.json` | `~/.claude/` локально |

## Поддержка README в актуальном состоянии

При любом изменении модели работы агентов **в первую очередь** обнови § [Кооперация агентов](#кооперация-агентов), затем [CLAUDE.md](CLAUDE.md) и затронутые `agents/*.md` — **в одном commit**.

Чеклист перед push:

- § «Кооперация агентов» согласован с `CLAUDE.md` (workflow, manager, self-improvement)
- Таблица скриптов = `ls scripts/`
- Таблица агентов = `ls agents/*.md`
- Нет ссылок на удалённые пути

Агенты: `pull` → правка → `commit` → `push` ([instructions-git-sync.md](memory-meta/claude-code/instructions-git-sync.md)).
