# Cursor CLI config (versioned policy)

Machine-independent **policy** for Cursor CLI, analogous to
[`settings/base.json`](../../settings/base.json) for Claude Code.

| File | Role |
|---|---|
| `cli-base.json` | Policy slice: `approvalMode`, `permissions.allow` / `deny`, `sandbox` |
| `permissions.json` | Auto-run instruction block for `~/.cursor/permissions.json` |

## Base vs local

- **Base (this directory):** shared allow/deny lists, approval mode, and sandbox
  defaults that should be the same on every machine. Tracked in git.
- **Local (`~/.cursor/cli-config.json`):** machine-specific and ephemeral keys —
  `authInfo`, model selection, caches, display prefs, and any extra allow entries
  granted only on this host. Not tracked here.

Do not put auth, model, or cache fields in `cli-base.json`.

## Merge semantics

`cursor/scripts/apply-cursor-config.sh` (called from
`install-cursor-links.sh` / `setup-symlinks.sh`) merges base into the live
CLI config:

| Key | Rule |
|---|---|
| `permissions.allow` | Union: base entries first, then local-only entries |
| `permissions.deny` | Union: base ∪ local |
| `approvalMode` | Base wins when base defines it |
| `sandbox` | Base wins for keys it defines (`mode`, `networkAccess`); other sandbox keys from local are kept |
| Everything else | Untouched (auth, model, caches, display, …) |

A backup is written to `cli-config.json.bak` before each swap. Override paths
with `CURSOR_CLI_CONFIG` / `CURSOR_CLI_BASE` (tests / special installs).

## `permissions.json` symlink

When `cursor/config/permissions.json` exists, apply links
`~/.cursor/permissions.json` → that file (same refuse-if-regular-file pattern as
`install-cursor-links.sh`). Skip with `SKIP_CURSOR_PERMISSIONS_LINK=1`.

## Apply

```bash
cursor/scripts/apply-cursor-config.sh
# or via full setup:
scripts/setup-symlinks.sh
```
