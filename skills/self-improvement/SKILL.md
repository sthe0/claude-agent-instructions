---
name: self-improvement
description: TRIGGER when the user gives substantive correction or feedback about agent behavior — corrects/rejects/clarifies your action or conclusion, states a principle ("don't do that", "prefer X", "always Z"), evaluates agent quality, proposes changes to instructions/agents/skills/memory/repo/workflow, or reminds you that this skill should have run (a reminder counts as feedback — invoke in the current turn). Diagnose what went wrong and write concrete edits to ~/claude-agent-instructions/. SKIP for neutral confirmation ("ok", "thanks", "yes do it") and for pure questions that do not evaluate your actions.
---

# Self-improvement

You improve the **agent system as a whole**: which components exist, the quality of each, the links between them. Success: future sessions solve user tasks faster, more accurately, with fewer repeated mistakes.

You run as a skill in the main thread, so you have full conversation context — no parent hand-off needed. Read the dialogue directly.

A user reminder ("did you run self-improvement?", "yes, run it") IS feedback. Invoke in the **same turn** as the trigger, before the final reply. Do not reply with apology only.

## Per-session workflow

1. **Collect signals.** User quote(s), what went wrong, what you already did (tactical fix, commit, revert).
2. **Classify.** Reasoning error / missing tool / wrong delegation / stale or misplaced memory / noise in instructions / wrong place for the content.
3. **Locate.** Where does the change belong? Use the table in § Where to put changes.
4. **Diff.** Propose a **concrete edit** (file, section, wording) — not generalities.
5. **Apply.** Edit, commit, push. The git workflow, English-only policy, and file-structure rules are mandatory and live in [policy.md](policy.md) — read it before editing the repo.

## Source of truth

| Component | Path |
|---|---|
| Global policy | `~/.claude/CLAUDE.md` |
| Subagents | `~/claude-agent-instructions/agents/*.md` → `~/.claude/agents/` |
| Skills | `~/claude-agent-instructions/skills/<name>/` → `~/.claude/skills/<name>/` |
| Global memory | `~/.claude/memory-global/MEMORY.md` + leaves in `memory-global/leaves/` |
| Project memory (local) | `<project_cwd>/.claude/agent-memory/MEMORY.md` (symlinked from `~/.claude/projects/<cwd-hash>/memory/`) |
| Settings / hooks | `~/.claude/settings.json`, `settings.local.json` |
| Cursor sync (deferred refactor) | `cursor-rules/*.mdc` |
| Versioning | git repo `~/claude-agent-instructions/` |

Do not patch files in `~/.claude/plugins/cache/` or upstream files on symlinks — make the change in the repo so the symlinks pick it up.

## Where to put changes

| Type of change | Target |
|---|---|
| "Always / never" for all sessions | `CLAUDE.md` |
| One subagent's role or delegation rules | `agents/<name>.md` |
| One skill's behavior, triggers, internals | `skills/<name>/SKILL.md` (or its `policy.md` / leaves) |
| Cross-project fact, practice, or runbook | global memory (`memory-global/MEMORY.md` + leaf in `memory-global/leaves/`) |
| Project-only fact or runbook | project memory (`<cwd>/.claude/agent-memory/`) |
| File layout, instruction language, git sync rules | this skill's [policy.md](policy.md) |
| Cursor-only (globs, project) | `cursor-rules/*.mdc` (deferred until Cursor wiring is redone) |

### Behavioral rule vs domain fact

Classify before placing:

| Type | Signs | Where |
|---|---|---|
| **Behavioral rule** | "always/never", delegation, tool choice, cross-repo pattern with no single product tie | `CLAUDE.md` or skill prompt |
| **Domain fact / runbook** | Relaunch procedures, API/CLI contracts, data paths, ticket-specific detail, prod naming | Memory leaf (global or project) |

Do not push domain runbooks into generic agent prompts or CLAUDE.md. If the user says "remember" or "too specific for an agent" — memory only; revert agent edit.

## Improvement areas beyond text

- **Hooks** in `~/.claude/settings.json` (`PreToolUse`, `PostToolUse`, `Stop`, `UserPromptSubmit`) — for automation that must run between turns.
- **Scripts** (`verify-*`, `setup-*`, sync).
- **Memory indexing** (search, tags, SQLite) if global memory grows large.
- **Frontmatter validation** in CI for agents and skills.

Each non-trivial proposal: **problem → options → recommendation → how to verify**.

## Style

Structured report: observations → diagnosis → proposals (priority) → next step.

Reply to the user in the same language as their request. Repository text stays English per [policy.md](policy.md) § Instruction language.
