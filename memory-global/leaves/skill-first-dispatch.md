---
name: skill_first_dispatch
description: Discipline for picking a skill before hand-rolling Bash for known domain operations — and the fewer-permission-prompts audit habit
type: feedback
created: 2026-05-27
last_verified: 2026-06-04
---

Before issuing a `Bash` sequence for a **known domain operation**, scan the system-reminder skill list for a matching skill and prefer it over raw CLI. The Skill tool is the cheaper, more auditable path; raw CLI is the fallback.

**The same preference applies to MCP tools.** For an operation a skill covers, prefer the **Skill** over calling an `mcp__<server>__*` tool directly: a skill is usually write-capable (many MCP servers are read-only), is a single auditable call, and bundles its own auth — whereas a loaded MCP server adds tool-schema/name overhead to context. Use MCP tools as a **fallback**: quick reads, or operations no skill covers. The concrete skill↔MCP mapping for a given environment (which local skill replaces which `mcp__*` server) is **environment-specific** and belongs in project memory, not here.

**Also applies to Python API calls via Bash** (`python3 -c "from <system>.api import ..."` / `python3 -c "from <system>.async_api import ..."`). Same rule: scan the skill list for a `<system>` namespace before composing raw Python. Example miss (DEEPAGENT-415 Stage B, 2026-05-27): used `python3 -c "from vh3.async_api import AsyncNirvanaApi; ..."` across ~5 iterations before noticing `nirvana-skill:nirvana` in the skill list, which packages the same calls with documentation. **Recurred (DEEPAGENT-415, 2026-06-03):** ~8 more Python-client iterations to read a failed Nirvana block's stderr — the Python `get_block_logs` returns `[]` for failed-block logs and has no `get_log_content`, so it is **not** a substitute for the skill's MCP flow. When the `mcp__nirvana__*` tools are not connected, use the raw log endpoint instead of grinding the Python client — see [[workflow-debug-investigation]] § Reading a failed Nirvana block's stderr.

**Why:** Audit of 9 recent transcripts in `robot/deepagent` (2026-05-27): 7 Skill invocations, **0 Agent invocations**, 482 Bash calls. Out of ~100 available skills, only 3 unique skills were used. Hand-rolled `arc add/commit/push`, `ya vault`, `arc grep`, manual Tracker REST PATCH, manual PR creation — all had matching skills in the system-reminder list that I never opened. The skill descriptions are there at session start, but **passive listing is not a trigger** — without active scanning I default to whatever Bash command came to mind first.

**How to apply:**

When you're about to issue Bash for any of these *classes* of operation, **pause and check the skill list** for a match before composing the command:

| Operation class | Look for skills like |
|---|---|
| VCS in Arcadia (commit / push / branch / PR-related) | `arc`, `arc-worktrees`, `arc-wt`, `create-pr` |
| PR review / Arcanum API (comments, checks, labels) | `arcanum`, `arcanum-client`, `code-review`, `review`, `self-review`, `security-review` |
| CI / releases / Sandbox | `ci`, `ci-releases`, `ci-jobs-logs`, `arcci-client`, `sandbox`, `sandbox-client` |
| Secrets / vault | `ya-vault` |
| Tickets / tracker / search | `tracker`, `tracker-management`, `startrek-client`, `intrasearch`, `intrasearch-client` |
| Code search in monorepo | `codesearch`, `ast-index` (Arcadia-aware, faster than recursive grep on the mount) |
| YT / YQL / analytics | `yt`, `yql`, `yql-analyst`, `ya-yql`, `chyt`, `datacatalog` |
| Nirvana / Reactor / Hitman / VH3 workflows | `nirvana`, `nirvana-skill`, `reactor`, `hitman-migration` |
| Backend logs / alerts | `monium`, `monium-client`, `monium-alerts`, `monium-metrics`, `juggler`, `jns` |
| Wiki / docs / pasta | `wiki`, `wiki-client`, `docs`, `docs-client`, `paste` |
| Roles / access / org | `idm`, `abc`, `abc-client`, `abcd-quota`, `staff-client`, `yandex-staff` |
| Forms / surveys / crowd labeling | `create-yandex-form-json`, plus see project memory for Yang/Toloka |
| App run / verify | `run`, `verify` |
| Diff simplification | `simplify` |

The Skill tool path is **single-call**: `Skill(skill="<name>", args="...")`. If the skill name has a plugin namespace, use `plugin:skill` form.

**fewer-permission-prompts audit habit.** Once per multi-session domain (or whenever the session feels click-heavy), run:

```
Skill(skill="fewer-permission-prompts")
```

It scans recent transcripts for common read-only Bash and MCP calls and emits an allowlist for `.claude/settings.json`. This is the **automated** version of the manual audit I did on 2026-05-27 for `robot/deepagent` — don't re-do that by hand next time.

**When NOT to use a skill:**
- Trivial one-off shell ops (`ls`, `cat`, `mkdir`) with no domain semantics.
- Operations the skill explicitly cannot do (see its SKILL.md scope/limits).
- When the skill is broken / outdated and a fix would take longer than the raw command.

**Domain-specific dispatch tables** belong in project memory, not here — `<cwd>/.claude/agent-memory/` is the right place to enumerate which exact skill maps to which exact operation in that project's context. This leaf is the cross-project discipline only.

Related: [[coordinator_pitfalls]] (same shape: tool exists, not invoked).
