---
name: skill_first_dispatch
description: Discipline for picking a skill before hand-rolling Bash for known domain operations — and the fewer-permission-prompts audit habit
type: feedback
created: 2026-05-27
last_verified: 2026-06-04
---

Before issuing a `Bash` sequence for a **known domain operation**, scan the system-reminder skill list for a matching skill and prefer it over raw CLI. The Skill tool is the cheaper, more auditable path; raw CLI is the fallback.

**The same preference applies to MCP tools.** For an operation a skill covers, prefer the **Skill** over calling an `mcp__<server>__*` tool directly: a skill is usually write-capable (many MCP servers are read-only), is a single auditable call, and bundles its own auth — whereas a loaded MCP server adds tool-schema/name overhead to context. Use MCP tools as a **fallback**: quick reads, or operations no skill covers. The concrete skill↔MCP mapping for a given environment (which local skill replaces which `mcp__*` server) is **environment-specific** and belongs in project memory, not here.

**Also applies to Python API calls via Bash** (`python3 -c "from <system>.api import ..."` / `python3 -c "from <system>.async_api import ..."`). Same rule: scan the skill list for a `<system>` namespace before composing raw Python — a recurring miss pattern is grinding through several iterations of a raw client library before noticing a packaged skill covers the same calls with documentation, or falling back to a raw client for a case the packaged skill's flow genuinely doesn't cover (e.g. a client method that can't return a needed error detail) — that fallback is a correct one, not a violation of this rule. A concrete worked incident (which workflow, which client, which log-retrieval gap) is environment-specific and lives in project memory, not here.

**Why:** An audit of a batch of recent transcripts in one deployment found heavy Bash usage and near-zero `Agent`-tool usage despite a large library of available skills, with only a handful of unique skills ever invoked. Hand-rolled VCS commands, secrets-vault commands, tracker REST calls, and manual PR creation all had matching skills in the system-reminder list that were never opened. The skill descriptions are there at session start, but **passive listing is not a trigger** — without active scanning the default is whatever Bash command comes to mind first.

**How to apply:**

When you're about to issue Bash for any of these *classes* of operation, **pause and check the skill list** for a match before composing the command:

| Operation class | Look for skills like |
|---|---|
| VCS (commit / push / branch / PR-related) | a VCS skill for the repo's hosting platform |
| PR review (comments, checks, labels) | a code-review skill for the repo's hosting platform |
| CI / releases / build jobs | a CI/build-system skill |
| Secrets / vault | a secrets-manager skill |
| Tickets / tracker / search | a ticket-tracker skill |
| Code search in a large monorepo | an indexed code-search skill (faster than recursive grep on a large tree) |
| Data warehouse / analytics queries | a data-warehouse / query-engine skill |
| Job / workflow orchestration | an orchestration-platform skill |
| Backend logs / alerts | a logs/alerting skill |
| Wiki / docs / paste | a wiki, docs, or paste-sharing skill |
| Roles / access / org directory | an identity/access-management skill |
| Forms / surveys / crowd labeling | a forms or labeling-platform skill |
| App run / verify | `run`, `verify` |
| Diff simplification | `simplify` |

The exact skill names behind each row are per-deployment — enumerate them in project memory, not here (see below).

The Skill tool path is **single-call**: `Skill(skill="<name>", args="...")`. If the skill name has a plugin namespace, use `plugin:skill` form.

**fewer-permission-prompts audit habit.** Once per multi-session domain (or whenever the session feels click-heavy), run:

```
Skill(skill="fewer-permission-prompts")
```

It scans recent transcripts for common read-only Bash and MCP calls and emits an allowlist for `.claude/settings.json`. This is the **automated** version of the manual transcript audit described above — don't re-do that by hand next time.

**When NOT to use a skill:**
- Trivial one-off shell ops (`ls`, `cat`, `mkdir`) with no domain semantics.
- Operations the skill explicitly cannot do (see its SKILL.md scope/limits).
- When the skill is broken / outdated and a fix would take longer than the raw command.

**Domain-specific dispatch tables** belong in project memory, not here — `<cwd>/.claude/agent-memory/` is the right place to enumerate which exact skill maps to which exact operation in that project's context. This leaf is the cross-project discipline only.

Related: [[coordinator_pitfalls]] (same shape: tool exists, not invoked).
