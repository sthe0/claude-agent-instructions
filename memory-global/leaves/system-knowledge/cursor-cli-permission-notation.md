---
name: Cursor CLI permission rules use their own five-token notation
description: Difficulty it removes — Claude Code permission rules copied into a Cursor CLI config never match, and a project .cursor/cli.json without permissions.deny is rejected outright. Fact — Cursor CLI knows only Shell / Read / Write / WebFetch / Mcp, requires both allow and deny, and validates the project config on every invocation.
type: reference
schema: leaf/v1
created: 2026-07-31
last_verified: 2026-07-31
---

# Cursor CLI permission notation

## Difficulty

Claude Code and Cursor CLI both express permissions as `Tool(argument)` strings in a
`permissions.allow` list, so the two look interchangeable and rules get copied across
verbatim. They are not interchangeable: Cursor silently ignores every rule whose tool name
it does not know (so the agent keeps prompting for actions that look allow-listed), and a
**project** `.cursor/cli.json` whose `permissions` object omits `deny` is rejected before
the session starts, with a Zod-style `invalid_type … path: ["permissions","deny"]` dump.

## Guidance

Cursor CLI recognizes exactly five permission tokens
([docs](https://cursor.com/docs/cli/reference/permissions)):

| Cursor token | Argument | Notes |
|---|---|---|
| `Shell(commandBase)` | first token of the command line | optional `command:args` glob form, e.g. `Shell(curl:*)` |
| `Read(pathOrGlob)` | path or glob | relative paths are workspace-scoped |
| `Write(pathOrGlob)` | path or glob | covers all file mutation; there is **no** `Edit` token |
| `WebFetch(domain)` | bare domain or `*.example.com` | **no** `domain:` prefix |
| `Mcp(server:tool)` | server from `mcp.json` + tool, `*` wildcards | not the `mcp__server__tool` form |

Translating from Claude Code notation: `Bash(x)` → `Shell(x)`, `Edit(x)` and `Write(x)` →
`Write(x)`, `Read(x)` unchanged, `WebFetch(domain:x)` → `WebFetch(x)`,
`mcp__server__tool` → `Mcp(server:tool)`. Anything else (`Glob`, `Grep`, `Task`,
`WebSearch`, `TodoWrite`, …) has no Cursor equivalent — drop it loudly rather than emit a
rule that will never match.

Keep multi-token command arguments verbatim when translating (`Bash(git remote:*)` →
`Shell(git remote:*)`), do not widen to `Shell(git:*)`. Cursor documents `commandBase` as
the *first token*, so a multi-token prefix may not match — but an unmatched rule only
costs a confirmation prompt, whereas the widened rule grants every subcommand.

Two config levels, different requirements: global `~/.cursor/cli-config.json` tolerates a
missing `deny`; a project `<repo>/.cursor/cli.json` does not. Always emit
`"deny": []` when generating one.

**Cheap check:** `cursor-agent --version` run inside the project directory validates the
project config and prints the schema error, so config validity can be verified without
starting a session or spending tokens.

## See also

- [[cursor-agent-cli-spawn]] — headless `agent -p` spawn from Cursor
- [[2026-07-31-generated-config-fix-the-generator-not-the-artifact]] — the task where this surfaced; `.cursor/cli.json` is generated, so the notation fix belongs in the generator
