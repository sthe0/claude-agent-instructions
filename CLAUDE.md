## Не коммитить

Этот файл — **локальные инструкции для агента** (Cursor / Claude Code). **Не добавляйте и не коммитьте его в Arc:** в тексте есть ссылки на локальную среду (`~/.venv`, `~/.claude/agents/`, субагенты **planner**, **thinker**, **developer** и опциональные роли в `~/.claude/agents/`), которые у каждого разработчика могут отличаться.

Файл перечислен в `.arcignore`. Если он всё же попал в индекс — не включайте в PR; правки держите локально или вынесите в общую документацию без machine-specific путей.

---

Try your best to avoid duplicating code. Explore adjacent files, project files, use the Depagent tool, and code search. Don't hesitate break existing functions and classes into pieces to move common code parts into separete common abstractions.
Do not add obvious or trivial comments. Prefer code expressiveness, readability and clarity over comments.
Ask deepagent tool about arcadia code projects and yandex-specific (or unknown) infrustructure. If you see an unknown term, first thing to do is to refer to deepagent tool, not code exploring. Ask deepagent tool about best implementations practices when in doubt. Deepagent tool provides best results when asked in Russian.
Use ~/.venv virtualenv to run python (except data_science CLI: always via Docker, see library/deepagent/data_science/DOCKER_RUN.md)
Use Yandex's version control system which is "arc".

## Поиск кода в Arcadia

**Никогда не делай `grep`/`rg`/`find` по `~/arcadia` целиком или по большим папкам** — Arcadia смонтирована через FUSE, рекурсивный обход подвешивает ФС и сжирает ресурсы. Вместо этого:
- **`ya tool grep`** — для текстового/regex-поиска по индексу Arcadia.
- **semantic code search** (MCP `mcp__intrasearch__semantic_code_search` или скилл `semantic_code_search`) — для поиска по описанию на естественном языке.
- **`ya tool cs`** / скилл `codesearch` — для навигации по символам и путям.
- Локальный `find`/`grep` допустим **только** внутри уже известной узкой подпапки проекта (например, `~/arcadia/path/to/project/`), не выше.

## Параллельная работа в arc

- Каждая задача с Tracker-тикетом (`[A-Z]+-\d+`) — в отдельном параллельном маунте: `~/arcadia_<TICKET>-<slug>`, ветка `<TICKET>-<slug>` (без префикса `users/<login>/` — арк добавит сам). Runbook маунта — `~/.claude/memory/INDEX.md` (скилл `using-arc-multiple-mounts` не патчить, он на симлинке).
- **`arc mount` только из `cd ~`** (не из cwd под `~/arcadia*`). Mount в фоне, ждать `[mounted]` в логе или в `arc mount --list`; не `pkill` по таймауту.
- Параллельный `arc mount`: всегда `--object-store …/objects`, `--override-object-store`, **`--allow-other`** (как основной `~/arcadia`; **обязательно** для `docker run -v ~/arcadia_*` — без флага root в контейнере не видит FUSE). Runbook: `~/.claude/memory/INDEX.md`.
- **Никогда не делай `arc checkout` в основном маунте `~/arcadia` без явного разрешения** — там работает пользователь.
- Ad-hoc вопросы / мелкие правки **без** ключа тикета — в текущем контексте, маунт не нужен. Наличие `[A-Z]+-\d+` в задаче, ветке или workspace **отменяет** это исключение.
- Маунт после задачи оставлять до явной команды на cleanup.

## Обязательный workflow: Tracker-тикет (`[A-Z]+-\d+`)

Родительский агент (Cursor / Claude Code) при задаче с ключом тикета — **до любых** `Edit`/`Write` в Arcadia, `arc checkout`, `arc commit`:

0. **Понимание** — прочитай тикет и комментарии. Числа, сроки, аббревиатуры («14 дней», TTL, quota, лимиты) без явного источника в тикете — **найди происхождение** (wiki, код, deepagent MCP, связанные PR) или **спроси пользователя**. Не привязывай число к случайному полю в коде и не начинай правки, пока не можешь объяснить, *откуда* взялось число и *какой* артефакт/поведение оно описывает. Read-only разведка до маунта допустима.
1. **Маунт** — отдельный параллельный маунт `~/arcadia_<TICKET>-<slug>` и ветка `<TICKET>-<slug>`. Runbook: `~/.claude/memory/INDEX.md`. **Запрещено** править код тикета в основном `~/arcadia`, пока пользователь явно не разрешил.
2. **План** — при **одном** тикете без затруднений: **planner** (`Task`, `subagent_type: planner`). При **нескольких тикетах**, мультишаговой координации или любом триггере из § «Обязательный manager» — сначала **manager** (он делегирует planner). В плане явно: интерпретация ключевых чисел/сроков и **где именно** в коде/конфиге будет правка (с обоснованием).
3. **Согласование** — покажи пользователю план (или резюме planner) и дождись явного «ок» / правок. **Запрещено** делегировать **developer** и делать `Edit`/`Write`/`arc commit`, пока план не согласован. Исключение: пользователь явно сказал «делай сразу» / «без согласования».
4. **Код** — делегируй **developer** (`Task`). Родитель **не** пишет production-код в Arcadia сам. Неизвестная орг-инфраструктура → субагент-консультант из `~/.claude/agents/` (если настроен) до или вместе с кодом.
5. **Проверка** — перед первой правкой: cwd/workspace в `~/arcadia_<TICKET>-*`, не в `~/arcadia`.

«Prefer» / «лучше использовать» для тикетных задач **не применяется** — понимание, согласование плана, делегирование и маунт обязательны.

## Claude Code и Cursor (один источник)

Оба инструмента читают **одни и те же файлы** через симлинки из `~/claude-agent-instructions/`:

| Файл в репо | Claude Code | Cursor |
|-------------|-------------|--------|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` | тот же путь в проекте (symlink) + правило ниже |
| `agents/*.md` | `~/.claude/agents/` | `~/.cursor/agents` → `.claude/agents` |
| `memory-global/` | `~/.claude/memory-global/` | тот же |
| `cursor-rules/claude-code-sync.mdc` | — | `~/.cursor/rules/claude-code-sync.mdc` |
| Локальная memory (вне git) | `~/.claude/memory/` | тот же |

Настройка и проверка: `scripts/setup-symlinks.sh`, `scripts/verify-instructions-sync.sh`, `scripts/verify-layout-contract.sh`. Контракт деревьев (global/local): `~/.claude/memory-global/agent-instructions/file-structure-contract.md`.

**Правки политики** — в репозитории; после `commit` обязателен `push` (см. ниже). Проект `robot/deepagent` — только overlay `.cursor/rules/deepagent-project.mdc`, не копия глобального rule.

## Контракт файловой структуры

Каноническое описание слоёв (git global, arc local, runtime-симлинки): **`~/.claude/memory-global/agent-instructions/file-structure-contract.md`**.

**Поддерживай актуальным:** при любом переносе/добавлении каталогов, скриптов или split global/local — обнови контракт, `runtime-layout.md`, README § симлинки/скрипты в **том же commit** (git и/или arc).

**Регулярно сверяй** факт с описанием:

1. `~/claude-agent-instructions/scripts/verify-layout-contract.sh` (и `verify-instructions-sync.sh`).
2. При расхождении — исправь **документ или дерево**, не оставляй расхождение.
3. После рефакторинга инструкций — Definition of Done включает прохождение verify.

Делегирование тяжёлой сверки локального arc-слоя — **memory** или **self-improvement**; родитель не пропускает verify после своих правок в `~/claude-agent-instructions/`.

## Git-репозиторий инструкций

Правки в `~/claude-agent-instructions/` (симлинки на `~/.claude/` и `~/.cursor/`). Детали: `~/.claude/memory-global/agent-instructions/instructions-git-sync.md`.

1. **Перед любой правкой** — `~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull` (подтянуть `origin/main`).
2. **После правки** — `git add` + `git commit` (без запроса пользователя) + **обязательный** `scripts/sync-instructions-repo.sh push`.
3. **Фон** — cron каждые 10 минут делает `pull`; конфликты rebase: скрипт предпочитает входящие, иначе доразрешить вручную.

## Memory и self-improvement

- **memory** — факты: локально `~/.claude/memory/INDEX.md`, глобально `~/.claude/memory-global/INDEX.md`.
- **self-improvement** — правила, агенты, репозиторий `~/claude-agent-instructions/`; после правок — commit + push (см. § «Git-репозиторий инструкций»).

### Обязательный self-improvement (родительский агент)

**В тот же момент диалога**, когда пользователь дал содержательную обратную связь, **запусти** субагент **self-improvement** (`Task`), даже если ты уже внёс тактический фикс.

Запуск **обязателен**, если сообщение пользователя:

- исправляет, отвергает или уточняет **твоё** действие, вывод, план или выбор инструмента;
- задаёт принцип или политику («так не надо», «лучше X», «зачем Y», «всегда Z»);
- оценивает качество работы агента (замечание, несогласие, пожелание по процессу);
- предлагает изменить инструкции, агентов, memory, репозиторий, скиллы, workflow.

**Не обязателен** только для нейтрального подтверждения без новой информации («ок», «да, делай», «спасибо») и для чистого вопроса **без** оценки или правки твоих действий.

В prompt для self-improvement передай: цитату пользователя, что ты сделал, что уже изменил, ожидаемый output (диагноз + предложения правок в `~/claude-agent-instructions/`).

**Не заканчивай ход** тактическим фиксом или извинением — сначала **Task** → **self-improvement**. Повторная корректировка по той же теме (в т.ч. «почему не было self-improvement») — снова запуск в **том же** ходе.

### Обязательный manager (родительский агент)

Родитель **не** играет координатора при затруднениях сам (Shell/Grep/transcript-read вместо делегирования — антипаттерн). **В тот же ход** запусти **Task** → **manager**, если выполняется **хотя бы один** триггер:

- **повторная ошибка** — та же команда/ветка/запуск упали второй раз;
- **блокер** — нет доступа, неясный следующий шаг, OOM/CI/WI FAILED без готового runbook;
- **расхождение с планом** — факт ≠ ожидание, пропущен шаг чеклиста, неверный relaunch пайплайна;
- **2+ корректировки пользователя по процессу** в одной теме (не только по коду);
- **перед ещё одной попыткой** Nirvana WI, `arc mount`, bundled CLI после неуспеха — сначала manager (исследование transcripts + перепланирование);
- **разбор сессии**, ретроспектива, **несколько тикетов** или мультишаговая координация — **manager до planner** (manager маршрутизирует planner/developer).

Запуск **обязателен** — «prefer delegate manager» **не применяется**. В prompt передай: цитату/симптом, что уже пробовали, текущий план, ожидаемый output (диагноз → переплан → действие).

**Запрещено** родителю править `agents/manager.md` ради «как работать при затруднении» — делегируй **manager** (отработать цикл) или **self-improvement** (системное правило в `CLAUDE.md` / sync-rule). Доменные runbook'и — только **memory**.

### Nirvana: после запуска WI

Запустил граф (CLI, Nirvana API, docker) — **сразу** сообщи WI id/URL и **опрашивай** до терминала у **всех** отслеживаемых инстансов (не жди явного «следи»). Runbook WI — `~/.claude/memory/INDEX.md`. Итог — таблица «мониторинг завершён» в том же ходе.

Доменные runbook'и — только leaf по `~/.claude/memory/INDEX.md`, не в промпты generic-агентов.

## Agents

Делегирование — через **Task** с `subagent_type` = `name` из `~/.claude/agents/*.md`. Для Tracker-тикета см. § «Обязательный workflow» выше.

- **manager** — **обязателен** при затруднениях (§ выше); мультишаговые задачи и разбор сессий; маршрутизация planner + developer; цикл исследование→критика→переплан→действие.
- **planner** — **обязателен** для декомпозиции Tracker-тикетов (план до правок кода).
- **thinker** — проверка рассуждений.
- **memory** — `~/.claude/memory/`.
- **self-improvement** — **обязателен** при корректировках и обратной связи (см. выше).
- **developer** — **обязателен** для правок кода в Arcadia по тикету; родитель не пишет код сам.
- **Опциональные субагенты** — только если есть в `~/.claude/agents/` (`name` + `description`); не выдумывай роли, которых нет на машине.

Чеклист старта Tracker-тикета — leaf в `~/.claude/memory/INDEX.md`; глобальные практики — `~/.claude/memory-global/development/` (см. **planner**, **manager**).
