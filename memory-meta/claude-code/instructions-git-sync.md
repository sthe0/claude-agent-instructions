# Git: репозиторий инструкций (`~/claude-agent-instructions`)

Симлинки: `~/.claude/agents`, `~/.claude/CLAUDE.md`, `~/.cursor/rules/claude-code-sync.mdc` → файлы в репо.

## Перед правкой (обязательно)

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
```

Подтянуть `origin/main`. При конфликте rebase скрипт предпочитает **входящие** изменения (`--theirs`); если не удалось — агент доразрешает вручную.

## После правки

```bash
cd ~/claude-agent-instructions
git add -A && git commit -m "…"
~/claude-agent-instructions/scripts/sync-instructions-repo.sh push
```

**Каждый** commit в этом репо сопровождается **push** в `origin` (без запроса пользователя).

Если push отклонён (remote впереди): `pull` → при необходимости поправить конфликты → `push` снова.

## Фоновая синхронизация (каждые 10 минут)

```bash
~/claude-agent-instructions/scripts/install-sync-cron.sh
```

Cron-строка (путь к репо подставляется при установке): `*/10 * * * * …/sync-instructions-repo.sh pull`.

Лог: `~/.local/log/claude-agent-instructions-sync.log`

Если `crontab` запрещён (корп. VM): `scripts/install-sync-systemd-timer.sh` — user systemd timer (pull каждые 10 мин). Дополнительно агент делает `pull` перед каждой правкой и при старте сессии, если планирует трогать репо.

## Git hooks

```bash
~/claude-agent-instructions/scripts/install-git-hooks.sh
```

`post-commit` автоматически вызывает `sync-instructions-repo.sh push` после каждого commit (дублирует явный push агента — на случай ручного commit).

## Скрипты

| Скрипт | Назначение |
|--------|------------|
| `scripts/sync-instructions-repo.sh pull` | fetch + rebase/ff-only |
| `scripts/sync-instructions-repo.sh push` | push если есть локальные коммиты |
| `scripts/sync-instructions-repo.sh sync` | pull, затем push |
| `scripts/install-sync-cron.sh` | добавить cron-строку |
| `scripts/install-sync-systemd-timer.sh` | user systemd timer (если cron недоступен) |
| `scripts/install-git-hooks.sh` | post-commit → auto-push |
