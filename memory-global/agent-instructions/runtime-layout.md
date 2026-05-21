# Runtime layout (canonical paths)

Где искать компоненты после `scripts/setup-symlinks.sh`. **Не** привязывай глобальные промпты к путям в Arcadia junk — источник локальной конфигурации задаётся на машине.

## Git (глобально, одинаково)

| Что | Где |
|-----|-----|
| Репозиторий инструкций | `~/claude-agent-instructions/` |
| Политика | `CLAUDE.md` → `~/.claude/CLAUDE.md` |
| Глобальные агенты | `agents/*.md` → `~/.claude/agents/<name>.md` |
| Глобальная memory | `memory-global/` → `~/.claude/memory-global/` |
| Cursor rule | `cursor-rules/claude-code-sync.mdc` → `~/.cursor/rules/` |

## Runtime (одинаковые имена, источник может отличаться)

| Что | Где искать |
|-----|------------|
| Все субагенты (глобальные + опциональные локальные) | `~/.claude/agents/` — по `name` в frontmatter |
| Скиллы | `~/.claude/skills/` |
| Локальная доменная memory | `~/.claude/memory/INDEX.md` |
| Глобальная memory | `~/.claude/memory-global/INDEX.md` |

Опциональные субагенты и локальная memory настраиваются на машине (отдельный arc-маунт, не основной `~/arcadia`). Симлинки задаёт `setup-symlinks.sh`; pull/commit/push — `sync-junk-agents-arc.sh` / `junk-agents-arc-commit.sh`. Runbook: `~/.claude/memory/INDEX.md` (тема the0-agents mount).

## Метаданные

| Поле | Значение |
|------|----------|
| `last_verified` | 2026-05-21 |
| `staleness_triggers` | смена `setup-symlinks.sh` |
| `revalidate` | `ls -la ~/.claude/agents ~/.claude/memory ~/.claude/memory-global` |
