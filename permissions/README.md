# Workflow-level permissions

Persistent `always`-scope permissions the user has granted for agent specialists.
Consulted by the manager before spawning a specialist; an action that matches
an entry is treated as already granted (no re-ask).

Not the same as Claude Code's tool-call permissions in `~/.claude/settings.json`
— those gate individual tool calls at the harness level. This directory records
**higher-level workflow actions** ("you may push to shared branches", "you may
deploy to staging", etc.).

## Files

- `global.json` — cross-machine grants tracked in this git repo.
- `<project_cwd>/.claude/agent-memory/permissions.json` — project-scope grants
  in each project's own git (not in this repo). Same schema.

## Schema

```json
{
  "permissions": [
    {
      "pattern": "arc push origin/main",
      "granted_at": "2026-05-25",
      "context": "Routine work-machine flow"
    }
  ]
}
```

Field rules:

- `pattern` — short string describing the action. Glob characters (`*`, `?`)
  are honored for matching; otherwise the requested action is matched if it
  case-insensitively contains the pattern.
- `granted_at` — ISO date `YYYY-MM-DD`. The day the user authorized the grant.
- `context` — one-line reason. Why this is safe, or the original PERMISSION-REQUEST
  it resolved.

## CLI

```bash
scripts/permissions.py list                    # all global grants
scripts/permissions.py check "arc push main"   # exit 0 if matched, 1 otherwise
scripts/permissions.py grant "arc push *" \
    --context "Routine work-machine flow"      # append (idempotent on pattern)
scripts/permissions.py revoke "arc push *"     # remove by exact pattern match
scripts/permissions.py digest                  # human-readable summary for prompts
```

All commands accept `--file PATH` to operate on a non-default file (e.g. a
project's `permissions.json`).

## Validation

`scripts/verify-permissions.py` is wired into `scripts/verify-all.py` and the
`pre-commit` hook. It checks the JSON is well-formed, each entry has the
required fields, dates parse, and patterns are not duplicated.
