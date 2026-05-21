# Claude Code и Cursor: один источник инструкций

## Метаданные

| Поле | Значение |
|------|----------|
| `last_verified` | 2026-05-21 |
| `revalidate` | `~/claude-agent-instructions/scripts/verify-instructions-sync.sh` без FAIL |

## Архитектура

```text
~/claude-agent-instructions/     ← git (единственный источник правды)
├── CLAUDE.md
├── agents/*.md
├── agents-local/*.md            ← gitignored (опционально, см. agents-local/README.md)
├── cursor-rules/
│   ├── claude-code-sync.mdc     ← глобально для Cursor
│   └── project-overlay-deepagent.mdc  ← шаблон overlay для robot/deepagent
└── memory-meta/INDEX.md

~/.claude/CLAUDE.md              → symlink
~/.claude/agents/<agent>.md      → symlink (по файлу)
~/.claude/memory/INDEX.md        → symlink
~/.cursor/agents                 → symlink на ~/.claude/agents
~/.cursor/rules/claude-code-sync.mdc → symlink

<project>/.cursor/rules/         ← только overlay (не дублировать глобальную политику)
<project>/CLAUDE.md              → опционально symlink на ~/.claude/CLAUDE.md
```

## Кто что читает

| Инструмент | Глобальная политика | Агенты | Memory INDEX |
|------------|---------------------|--------|--------------|
| **Claude Code** | `~/.claude/CLAUDE.md` | `~/.claude/agents/*.md` | `~/.claude/memory/INDEX.md` |
| **Cursor** | `~/.cursor/rules/claude-code-sync.mdc` + тот же `CLAUDE.md` в проекте (если symlink) | `~/.cursor/agents` (= `.claude/agents`) | тот же INDEX |

**Канонический текст** глобальных правил — `CLAUDE.md` в репозитории. `claude-code-sync.mdc` дублирует обязательные gate для Cursor (`alwaysApply`) и отсылает к `CLAUDE.md` при расхождении.

## Синхронизация между машинами и IDE

1. **Git:** `pull` перед правкой, `commit` + `push` после (`instructions-git-sync.md`).
2. **Симлинки:** `scripts/setup-symlinks.sh` после clone/pull на новой машине.
3. **Фон:** systemd timer или cron — `pull` каждые 10 мин.
4. **Проверка:** `scripts/verify-instructions-sync.sh` — симлинки, отсутствие устаревших копий.

## Правила правок (агент)

| Что менять | Где |
|------------|-----|
| Глобальная политика, workflow, manager/self-improvement | `CLAUDE.md` + зеркало в `cursor-rules/claude-code-sync.mdc` |
| Роль одного агента | `agents/<name>.md` |
| Только Cursor (globs, project) | `cursor-rules/project-overlay-*.mdc` |
| Только deepagent домен | `~/.claude/memory/deepagent/` (не в git репо инструкций) |

**Запрещено:** полная копия `claude-code-sync.mdc` внутри проекта Arcadia — только **overlay** из шаблона `project-overlay-deepagent.mdc`.

## robot/deepagent

- `CLAUDE.md` → `~/.claude/CLAUDE.md` (symlink)
- `.cursor/rules/deepagent-project.mdc` — overlay (permissions, deepagent memory), не замена глобального rule
