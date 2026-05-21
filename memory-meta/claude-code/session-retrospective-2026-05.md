# Ретроспектива сессий агента (2026-05-17 — 2026-05-21)

Источник: agent transcripts проекта `robot/deepagent` (13 сессий, ~2.9 MB). Не дублировать доменные факты deepagent — они в leaf `deepagent/*`.

## Метаданные

| Поле | Значение |
|------|----------|
| `last_verified` | 2026-05-21 |
| `staleness_triggers` | смена обязательного workflow в CLAUDE.md; новые массовые паттерны ошибок в transcripts |
| `revalidate` | выборочно 3 последних parent-transcript; счётчик Task vs Edit; есть ли нарушения «план до кода» |

## Сводка периода

| Сессия (uuid prefix) | Тикеты | Тип |
|----------------------|--------|-----|
| e7175906 | DEEPAGENT-416 | OOM compute_metrics, Nirvana, Docker |
| 085f3aed | DEEPAGENT-421 | TTL; ошибка «14 дней» без уточнения |
| 593d90aa | DEEPAGENT-419 | cleanup auto_eval; сначала без маунта |
| 9d95383c, 8f0c5353 | DEEPAGENT-402 | FTE-alert; orphan mount → Warming up |
| 6f052210 | 220, 403 | manager, train→eval |
| 8f8b2055 | — | git sync инструкций, agents-local |

**Делегирование (агрегат):** `Task` ~22 vs `Shell` ~1250, `StrReplace` ~370. Субагенты: self-improvement 12, yandex-developer 10, planner 4, manager 6. Родитель делает слишком много сам.

## Топ ошибок (не повторять)

| # | Симптом | Правильно |
|---|---------|-----------|
| 1 | Код/поиск в `~/arcadia` до маунта и без planner | pull instructions → tracker → **planner** → **согласование** → mount → **yandex-developer** |
| 2 | Число в тикете привязано к «похожей» константе (421: 14 дней) | Источник или вопрос пользователю **до** правок |
| 3 | Полный `run_quality` / train→eval при отладке одного кубика | memory: `test-quality-retest.md`, `train-eval-meta-relaunch.md` |
| 4 | Новый `console_scripts` вместо Fire-подкоманды | Один entry point; one-off — stash, не Arc |
| 5 | Self-improvement после 2-й корректировки или только извинение | **Task → self-improvement в том же ходе** до ответа |
| 6 | WI запущен — пользователь спрашивает «ты следишь?» | Сразу poll всех WI → таблица «мониторинг завершён» (`nirvana-wi-watch.md`) |
| 7 | Runbook Nirvana/VH3 в `manager.md` / `yandex-developer.md` | Только **memory** leaf + ссылка в плане |
| 8 | Маунт без `--allow-other` → Docker не видит FUSE | `arc-parallel-mounts.md` |
| 9 | Маунт не снят после тикета → Warming up | `arc unmount` по завершении |
| 10 | Правки инструкций без push / без pull перед edit | `instructions-git-sync.md` |

Детали инцидента 416: `deepagent/compute-metrics-oom-de416.md`.

## Чеклист старта Tracker-тикета (P0)

Используют **родитель**, **manager**, **planner** — не пропускать шаги.

1. `scripts/sync-instructions-repo.sh pull`
2. Прочитать тикет + комментарии + links
3. Неясные числа/сроки/TTL → источник (wiki, код, deepagent MCP) **или вопрос пользователю**
4. `memory/INDEX.md` — релевантные leaf (Nirvana relaunch, retest, mount…)
5. **planner** → markdown-план с «Проблема и критерий решения»
6. Показать план → **явное «ок»** (кроме «делай сразу»)
7. Параллельный маунт `~/arcadia_<TICKET>-<slug>` (`--allow-other`)
8. **yandex-developer** в маунте — не родитель
9. После Nirvana launch → WI watch до терминала
10. Закрытие: PR/тикет, **unmount**, при обучении — **memory** / **self-improvement**

## Gate самопроверки (родитель)

Перед первым `Edit`/`Write`/`arc commit` в Arcadia по тикету:

- [ ] В этом диалоге было сообщение пользователю с планом и подтверждением (или «делай сразу»)
- [ ] cwd — `~/arcadia_<TICKET>-*`, не `~/arcadia`
- [ ] Нет дублирующего полного пайплайна, если цель — ретест одной стадии

После корректировки пользователя о поведении агента:

- [ ] Запущен **self-improvement** в **том же** ходе (до финального ответа)

## Метрика длинной сессии (опционально)

В конце сессии с >10 tool calls — одна строка в итоге пользователю:

`Делегирование: Task=N; правки родителем (Edit/Write)=M.`

Цель: снижать M на тикетных задачах.

## Что уже закрыто инструкциями (2026-05-21)

- Workflow: понимание → план → согласование → mount → yandex-developer
- Обязательный self-improvement в том же ходе
- `agents-local/` для logos-*; git pull/commit/push + hooks
- `nirvana-wi-watch.md`, TTL layers, memory revalidate в `memory.md`

## Приоритет улучшений

1. **Исполнение** чеклиста (не новые правила).
2. Читать memory **до** запуска Nirvana CLI.
3. Домен → memory leaf с `revalidate`, не промпты агентов.
