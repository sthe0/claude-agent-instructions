# Skills (symlinks)

Skills are not versioned in this repository: `~/.claude/skills/` is a directory of symlinks into Arcadia (`ai/artifacts/skills/…`) and plugins.

**Current list** — only from the live tree (do not commit a snapshot, it goes stale):

```bash
ls -la ~/.claude/skills
```

Names only (no `-> target`):

```bash
ls -1 ~/.claude/skills
```

Check where one skill points:

```bash
readlink -f ~/.claude/skills/arc
```

Find broken symlinks:

```bash
find ~/.claude/skills -maxdepth 1 -type l ! -exec test -e {} \; -print
```

After adding/removing a skill under `~/.claude/skills` nothing in git needs to change.
