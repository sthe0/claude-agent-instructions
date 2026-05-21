# Локальные агенты (`agents-local/`)

Каталог **не версионируется** (кроме этого README). Файлы `*.md` здесь линкуются в `~/.claude/agents/` через `scripts/setup-symlinks.sh`.

## logos-* (Logos ETL)

На этой машине все `logos-*` агенты лежат только здесь, не в `agents/` репозитория.

После `git pull`, если в репо удалили logos из `agents/`, один раз:

```bash
~/claude-agent-instructions/scripts/setup-symlinks.sh
```

На новой машине: скопируй `agents-local/logos-*.md` с рабочей станции или восстанови из бэкапа, затем `setup-symlinks.sh`.
