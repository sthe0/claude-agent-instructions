## Не коммитить

Этот файл — **локальные инструкции для агента** (Cursor / Claude Code). **Не добавляйте и не коммитьте его в Arc:** в тексте есть ссылки на локальную среду (`~/.venv`, `~/.claude/agents/`, субагенты **planner**, **thinker**, **yandex-developer** и др.), которые у каждого разработчика свои.

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

- Каждая задача с Tracker-тикетом (`[A-Z]+-\d+`) — в отдельном параллельном маунте: `~/arcadia_<TICKET>-<slug>`, ветка `<TICKET>-<slug>` (без префикса `users/<login>/` — арк добавит сам). Команда маунта и отличия от upstream-скилла — в `~/.claude/memory/claude-code/arc-parallel-mounts.md` (скилл `using-arc-multiple-mounts` не патчить, он на симлинке).
- **`arc mount` только из `cd ~`** (не из cwd под `~/arcadia*`). Mount в фоне, ждать `[mounted]` в логе или в `arc mount --list`; не `pkill` по таймауту.
- Параллельный `arc mount`: всегда `--object-store …/objects`, `--override-object-store`, **`--allow-other`** (как основной `~/arcadia`; нужно для Docker и чужого uid).
- **Никогда не делай `arc checkout` в основном маунте `~/arcadia` без явного разрешения** — там работает пользователь.
- Ad-hoc вопросы / мелкие правки без тикета — в текущем контексте, маунт не нужен.
- Маунт после задачи оставлять до явной команды на cleanup.

## Git-репозиторий инструкций

Правки в `~/claude-agent-instructions/` (симлинки на `~/.claude/`) — **сразу `git commit`**, без запроса; и после правок агента, и после правок пользователя. См. `~/claude-agent-instructions/CLAUDE.md`.

## Memory и self-improvement

- **memory** — доменные факты в `~/.claude/memory/` (INDEX → leaf).
- **self-improvement** — правила, агенты, репозиторий `~/claude-agent-instructions/`; после правок — commit.

### Обязательный self-improvement (родительский агент)

**В тот же момент диалога**, когда пользователь дал содержательную обратную связь, **запусти** субагент **self-improvement** (`Task`), даже если ты уже внёс тактический фикс.

Запуск **обязателен**, если сообщение пользователя:

- исправляет, отвергает или уточняет **твоё** действие, вывод, план или выбор инструмента;
- задаёт принцип или политику («так не надо», «лучше X», «зачем Y», «всегда Z»);
- оценивает качество работы агента (замечание, несогласие, пожелание по процессу);
- предлагает изменить инструкции, агентов, memory, репозиторий, скиллы, workflow.

**Не обязателен** только для нейтрального подтверждения без новой информации («ок», «да, делай», «спасибо») и для чистого вопроса **без** оценки или правки твоих действий.

В prompt для self-improvement передай: цитату пользователя, что ты сделал, что уже изменил, ожидаемый output (диагноз + предложения правок в `~/claude-agent-instructions/`).

## Agents

- **manager** — сложные мультишаговые задачи; следит, чтобы при обратной связи вызывался **self-improvement**.
- **planner** — декомпозиция Tracker-тикетов.
- **thinker** — проверка рассуждений.
- **memory** — `~/.claude/memory/`.
- **self-improvement** — **обязателен** при корректировках и обратной связи (см. выше).
- **yandex-developer** — код в Arcadia.
- **logos-*** — только Logos ETL.
