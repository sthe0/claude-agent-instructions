---
name: granted-permissions
description: Persistent workflow-level permission grants. Each entry records an action the user has authorized for agent specialists, the scope (project / global), the date, and brief context. Read this leaf before spawning a specialist so the spawn prompt includes a digest of relevant grants. Update after the user grants an "always" permission in response to a PERMISSION-REQUEST.
type: reference
---

# Granted permissions (global)

Workflow-level permissions the user has granted with `always` scope. The manager checks this leaf when handling a specialist's `PERMISSION-REQUEST:` — if the requested action matches an entry here, the manager treats it as already granted instead of re-asking the user.

This is **not** the same as Claude Code's tool-call permissions in `~/.claude/settings.json`. Those gate individual tool calls at the harness level. This file records higher-level workflow permissions ("you may push to shared branches", "you may deploy to staging", etc.) that the manager honors when planning and re-spawning specialists.

Project-scope grants go in the project's own `<cwd>/.claude/agent-memory/granted-permissions.md`, with the same format. The manager consults both files (this one for global grants, the project file for project grants) before asking the user.

## Format

One row per grant. Keep `Action pattern` human-readable (a natural-language description, not a regex). The manager interprets matching loosely — close textual overlap is enough; if unsure, the manager re-asks the user.

| Date | Action pattern | Scope | Context |
|---|---|---|---|

<!-- No global grants yet. Append rows above as the user grants permissions. -->
