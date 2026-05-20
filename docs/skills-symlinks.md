# Skills (симлинки)

Скиллы не версионируются в этом репозитории: `~/.claude/skills/` — каталог симлинков в Arcadia (`ai/artifacts/skills/…`) и плагины.

**Актуальный список** — только из live-дерева (не коммитить снимок, он протухает):

```bash
ls -la ~/.claude/skills
```

Только имена (без `-> target`):

```bash
ls -1 ~/.claude/skills
```

Проверить, куда ведёт один скилл:

```bash
readlink -f ~/.claude/skills/arc
```

Поиск битых симлинков:

```bash
find ~/.claude/skills -maxdepth 1 -type l ! -exec test -e {} \; -print
```

После добавления/удаления скилла в `~/.claude/skills` в git ничего менять не нужно.
