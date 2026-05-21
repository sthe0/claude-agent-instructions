# Локальные MCP-серверы (`mcp-local/`)

Каталог **не версионируется** (кроме этого README).

Каждый файл `<name>.json` — конфиг одного MCP-сервера в формате:

```json
{
  "command": "npx",
  "args": ["-y", "@some/mcp-server"],
  "env": { "KEY": "value" }
}
```

Применить все конфиги в `~/.claude/settings.local.json`:

```bash
~/claude-agent-instructions/scripts/apply-mcp-local.sh
```

Скрипт идемпотентен: повторный запуск обновляет существующие записи, не дублирует.

На новой машине: скопируй нужные `*.json` с другой машины или восстанови из бэкапа, затем запусти `apply-mcp-local.sh`.
