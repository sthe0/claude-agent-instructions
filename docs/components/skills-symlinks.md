# Skills (symlinks)

Skills are not versioned in this repository: `~/.claude-agent/skills/` is a directory of symlinks into Arcadia (`ai/artifacts/skills/…`) and plugins.

**Current list** — only from the live tree (do not commit a snapshot, it goes stale):

```bash
ls -la ~/.claude-agent/skills
```

Names only (no `-> target`):

```bash
ls -1 ~/.claude-agent/skills
```

Check where one skill points:

```bash
readlink -f ~/.claude-agent/skills/arc
```

Find broken symlinks:

```bash
find ~/.claude-agent/skills -maxdepth 1 -type l ! -exec test -e {} \; -print
```

After adding/removing a skill under `~/.claude-agent/skills` nothing in git needs to change.
