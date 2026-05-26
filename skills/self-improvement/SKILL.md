---
name: self-improvement
description: TRIGGER when the user gives substantive correction or feedback about agent behavior — corrects/rejects/clarifies your action or conclusion, states a principle ("don't do that", "prefer X", "always Z"), evaluates agent quality, proposes changes to instructions/agents/skills/memory/repo/workflow, or reminds you that this skill should have run (a reminder counts as feedback — invoke in the current turn). Diagnose what went wrong and write concrete edits to ~/claude-agent-instructions/. SKIP for neutral confirmation ("ok", "thanks", "yes do it") and for pure questions that do not evaluate your actions.
---

# Self-improvement

You improve the **agent system as a whole**: which components exist, the quality of each, the links between them. Success: future sessions solve user tasks faster, more accurately, with fewer repeated mistakes.

You run as a skill in the main thread, so you have full conversation context — no parent hand-off needed. Read the dialogue directly.

A user reminder ("did you run self-improvement?", "yes, run it") IS feedback. Invoke in the **same turn** as the trigger, before the final reply. Do not reply with apology only.

## Two-turn workflow

This skill splits into **two distinct turns** to avoid overloading a single response with diagnosis + writing + editing + committing. Each turn has a narrow job.

### Turn 1 — diagnosis and proposal (same turn as the trigger)

In the same dialog turn the trigger fires, produce **text only**: no file edits to the instructions repo yet. The goal is a clear, reviewable proposal the user can accept, push back on, or refine.

1. **Sync first.** Run `~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull` before reasoning. There is no background cron auto-pull, and other machines may have pushed since your session started. If `pull` brought new commits, do the reconcile in [policy.md](policy.md) § After pull before proceeding.

2. **Re-read the relevant instruction files from disk.** The conversation context (system prompt, prior turns, your own memory of the dialogue) may carry **stale versions** of `CLAUDE.md`, `agents/*.md`, `skills/*/SKILL.md`, `policy.md`, or memory leaves. **Use `Read` on every file you plan to touch**, immediately before reasoning about the edit. On-disk content wins.

3. **Collect signals.** User quote(s), what went wrong, what you already did (tactical fix, commit, revert).

4. **Classify.** Reasoning error / missing tool / wrong delegation / stale or misplaced memory / noise in instructions / wrong place for the content.

5. **Locate.** Where does the change belong? Use the table in § Where to put changes.

6. **Propose concrete edits.** For each proposed change: target file, section, before/after wording (or close to it). No generalities. The user reads this and approves, refines, or rejects.

End turn 1 with an `AskUserQuestion` ask: "Apply these changes?" (options: `Apply (Recommended)` / `Refine` / `Skip`) — and stop. Do not start editing files. Per CLAUDE.md § Escalation to the user, `AskUserQuestion` is mandatory at confirmation gates — free text at the apply-gate is the exact failure mode this skill most often diagnoses for users; the skill itself must model it.

### Turn 2 — apply (next turn, after user confirmation)

The user's confirmation can be terse ("да", "ok", "do it"). On confirmation:

7. **Apply.** Edit the target files. The git workflow, English-only policy, and file-structure rules are mandatory and live in [policy.md](policy.md) — read it before editing the repo.

8. **Commit.** One commit per coherent batch (see [policy.md](policy.md) § Git sync).

9. **Push.** Only after the user explicitly confirms push (separate confirmation — commit ≠ push). Ask via `AskUserQuestion` (options: `Push (Recommended)` / `Keep local`).

### When to collapse into one turn

If the user's message **already contains explicit approval to edit** ("сделай эти правки", "apply", "do it now", "сделай все"), turns 1 and 2 collapse into a single response. Still do steps 1–9 in order — just without a stop between 6 and 7.

A bare reminder ("did you run self-improvement?") is **not** pre-approval — run turn 1 only and wait.

## Source of truth

| Component | Path |
|---|---|
| Global policy | `~/.claude/CLAUDE.md` |
| Subagents | `~/claude-agent-instructions/agents/*.md` → `~/.claude/agents/` |
| Skills | `~/claude-agent-instructions/skills/<name>/` → `~/.claude/skills/<name>/` |
| Global memory | `~/.claude/memory-global/MEMORY.md` + leaves in `memory-global/leaves/` |
| Project memory (local) | `<project_cwd>/.claude/agent-memory/MEMORY.md` (symlinked from `~/.claude/projects/<cwd-hash>/memory/`) |
| Settings / hooks | `~/.claude/settings.json`, `settings.local.json` |
| Cursor sync | `cursor-rules/claude-code-sync.mdc` (global) and `cursor-rules/project-overlay-*.mdc` (project) |
| Versioning | git repo `~/claude-agent-instructions/` |

Do not patch files in `~/.claude/plugins/cache/` or upstream files on symlinks — make the change in the repo so the symlinks pick it up.

## Where to put changes

| Type of change | Target |
|---|---|
| "Always / never" for all sessions | `CLAUDE.md` (+ mirror essentials in `cursor-rules/claude-code-sync.mdc` if Cursor must always see them) |
| One subagent's role or delegation rules | `agents/<name>.md` |
| One skill's behavior, triggers, internals | `skills/<name>/SKILL.md` (or its `policy.md` / leaves) |
| Skill trigger description | `skills/<name>/SKILL.md` frontmatter **and** the corresponding `### <skill>` block in `cursor-rules/claude-code-sync.mdc` (Cursor has no Skill tool — it relies on the embedded trigger description to know when to invoke) |
| Cross-project fact, practice, or runbook | global memory (`memory-global/MEMORY.md` + leaf in `memory-global/leaves/`) |
| Project-only fact or runbook | project memory (`<cwd>/.claude/agent-memory/`) |
| File layout, instruction language, git sync rules | this skill's [policy.md](policy.md) |
| Cursor-only (globs, project) | `cursor-rules/*.mdc` |

### Keeping Claude and Cursor in sync

`cursor-rules/claude-code-sync.mdc` is a thin mirror of `CLAUDE.md`. When you change behavior that Cursor must see:

- Edit `CLAUDE.md` first (canonical).
- If the change affects mandatory triggers / coordination cycle / skill descriptions / agent table — also update `cursor-rules/claude-code-sync.mdc` in the **same commit**.
- Heavy detail (full skill body, memory leaves) lives in the linked files; the Cursor rule only carries triggers and entry points.

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
