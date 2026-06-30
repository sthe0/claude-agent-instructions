---
name: self-improvement
description: TRIGGER when the user gives substantive correction or feedback about agent behavior — corrects/rejects/clarifies your action or conclusion, states a principle ("don't do that", "prefer X", "always Z"), evaluates agent quality, proposes changes to instructions/agents/skills/memory/repo/workflow, or reminds you that this skill should have run (a reminder counts as feedback — invoke in the current turn). Diagnose what went wrong and write concrete edits to ~/claude-agent-instructions/. SKIP for neutral confirmation ("ok", "thanks", "yes do it") and for pure questions that do not evaluate your actions.
---

# Self-improvement

You improve the **agent system as a whole**: which components exist, the quality of each, the links between them. Success: future sessions solve user tasks faster, more accurately, with fewer repeated mistakes.

This work is **task work itself**, governed by the same objective function as any user task (see [coordinator-objective.md](../../memory-global/leaves/coordinator-objective.md) § Self-improvement is task work too). Every leaf, hook, or rule you propose costs across all future sessions that load or trigger it; justify each on saved future-task cost vs that loaded cost. A proposal that can't be justified that way is premature — defer.

You run as a skill in the main thread, so you have full conversation context — no parent hand-off needed. Read the dialogue directly.

A user reminder ("did you run self-improvement?", "yes, run it") IS feedback. Invoke in the **same turn** as the trigger, before the final reply. Do not reply with apology only.

## Workflow — on the standard plan-approval spine

Self-improvement edits the agent's own instructions / config — itself a **state-changing task**, gated by the agentctl spine like any other (config and instructions are production; the dedicated `si-propose`/`si-apply` two-beat gate was retired and **only memory writes are gate-exempt**). Run it as a **two-beat** task so one response never overloads diagnosis + authoring + editing + committing. The cognition below (re-read, classify, author edits, the `AskUserQuestion`) is yours; the engine holds the standard plan-approval gate around the edit.

### Beat 1 — diagnosis and proposal (same turn as the trigger)

Produce **text only** — no edits to the instructions repo yet. Goal: a reviewable proposal the user can accept, refine, or reject.

1. **Sync first.** Run `~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull` before reasoning — no background auto-pull exists, other machines may have pushed. If `pull` brought commits, reconcile per [policy.md](policy.md) § After pull before proceeding.
2. **Re-read the target files from disk.** Conversation context may hold **stale** `CLAUDE.md` / `agents/*.md` / `skills/*/SKILL.md` / `policy.md` / leaves. `Read` every file you plan to touch immediately before reasoning — on-disk content wins.
3. **Collect signals.** User quote(s), what went wrong, what you already did (tactical fix, commit, revert).
4. **Classify.** Reasoning error / missing tool / wrong delegation / stale or misplaced memory / noise in instructions / wrong place for the content — and behavioral rule vs domain fact (§ Behavioral rule vs domain fact): a rule → instructions, a fact → memory leaf.
5. **Locate.** Where does the change belong? Use the table in § Where to put changes.
6. **Propose concrete edits.** Per change: target file, section, before/after wording. No generalities. **Name the difficulty per rule** — for each added/changed behavioral rule, state the functional ground (the desired-vs-actual it removes) in "to achieve X, do Y" form per [policy.md](policy.md) § Ground instructions in the difficulty they remove; a rule whose difficulty you cannot name is a prune candidate, not a proposal.

End beat 1 with an `AskUserQuestion`: "Apply these changes?" (`Apply (Recommended)` / `Refine` / `Skip`) — and stop. Do not edit files. **This `AskUserQuestion` is the plan-approval gate** for the change; it is mandatory — free text at the apply-gate is the exact failure mode this skill most often diagnoses, so the skill must model the click-gate. For a **substantive** instruction change (multi-file, architectural, a changed coordination rule), route the proposal through `planner` and the standard plan-approval gate rather than a bare `AskUserQuestion`.

### Beat 2 — apply (after user confirmation)

The confirmation can be terse ("да", "ok", "do it"). On confirmation:

7. **Apply + commit.** Edit the target files (git workflow, English-only, file-structure rules in [policy.md](policy.md) — read it first). One commit per coherent batch ([policy.md](policy.md) § Git sync), carrying the `[self-improvement-reviewed]` trailer (the commit-msg hook enforces it on any `skills/self-improvement/` edit).
8. **Push.** Only after a **separate** explicit confirmation (commit ≠ push). Ask via `AskUserQuestion` (`Push (Recommended)` / `Keep local`).

### When to collapse into one turn

If the user's message **already contains explicit approval to edit** ("сделай эти правки", "apply", "do it now", "сделай все"), the two beats collapse into one response — still steps 1–6 → `AskUserQuestion` (or its pre-approved equivalent) → 7–8 in order, without a stop between 6 and 7.

A bare reminder ("did you run self-improvement?") is **not** pre-approval — run beat 1 only and wait.

### Non-author machines route Core difficulties to a channel (ADR-0001)

*Difficulty removed: a machine without Core commit authority cannot land a Core edit, so proposing one there dead-ends the feedback instead of recording it where an author will see it.*

Before proposing an edit to a **protected-Core** artifact (the CODEOWNERS-guarded set: `CLAUDE.md`, `config.md`, `skills/**`, `agents/**`, `cursor/**`, `*.mdc`, `scripts/agentctl/**`), check authority with `difficulty_channel.authority.is_author()` (a `git push --dry-run` capability probe — the machine either has push rights or it does not). On a **non-author** machine, do **not** author a Core edit — instead run:

```bash
python3 ~/claude-agent-instructions/scripts/file-difficulty.py \
  --target <artifact> \
  --ground '<desired-vs-actual the feedback names>' \
  --severity <low|medium|high|critical>
```

The machine's channel is auto-selected from `~/.claude/agent-identity.local` (`difficulty_channel=startrek` or `difficulty_channel=github`); override with `--channel` if needed. An **author** runs the normal spine: `core-difficulty-digest.py` to see the accumulated + clustered difficulties, then `planner → approval → developer` for the batched Core change. Non-Core targets (memory leaves, project files) are unaffected — they are not edit-restricted. This is the propose-not-execute / no-veto rule: a non-author surfaces the difficulty, only an author lands the change.

**Tier routing (queue/stream).** The same command files to the right tier's surface — classify the target's tier, the structure carries the rest (full model: [instruction-dev-queues.md](../../memory-global/leaves/instruction-dev-queues.md), policy.md § Routing a difficulty to its queue by tier):
- **Org**-specific (Yandex, cross-project): `--channel startrek` files a report to `OOSEVENREPORT`; a planned backlog item uses `--stream backlog` (→ `OOSEVEN`).
- **Project** (e.g. `robot/deepagent`): pass a `--target` **under the project tree** — `file-difficulty.py` resolves the project's `instruction_queue` field (`agent-project.json`, e.g. `DEEPAGENT`) automatically, or pass `--queue <KEY>` explicitly. Backlog and reports collapse onto that one queue.
- **Core**: a backlog item (not a reactive report) uses `--stream backlog` so the digest's `difficulty`-label pull skips it.

## Source of truth

| Component | Path |
|---|---|
| Global policy | `~/.claude/CLAUDE.md` |
| Subagents | `~/claude-agent-instructions/agents/*.md` → `~/.claude/agents/` |
| Skills | `~/claude-agent-instructions/skills/<name>/` → `~/.claude/skills/<name>/` |
| Global memory | `~/.claude/memory-global/MEMORY.md` + leaves in `memory-global/leaves/` |
| Project memory (local) | `<project_cwd>/.claude/agent-memory/MEMORY.md` (symlinked from `~/.claude/projects/<cwd-hash>/memory/`) |
| Settings / hooks | `~/.claude/settings.json`, `settings.local.json` |
| Cursor sync | `cursor/rules/claude-code-sync.mdc` (global) and project overlays in `<project>/.claude/rules/*.mdc` |
| Versioning | git repo `~/claude-agent-instructions/` |

Do not patch files in `~/.claude/plugins/cache/` or upstream files on symlinks — make the change in the repo so the symlinks pick it up.

## Where to put changes

| Type of change | Target |
|---|---|
| "Always / never" for all sessions | `CLAUDE.md` (+ mirror essentials in `cursor/rules/claude-code-sync.mdc` if Cursor must always see them) |
| One subagent's role or delegation rules | `agents/<name>.md` |
| One skill's behavior, triggers, internals | `skills/<name>/SKILL.md` (or its `policy.md` / leaves) |
| Skill trigger description | `skills/<name>/SKILL.md` frontmatter **and** the corresponding `### <skill>` block in `cursor/rules/claude-code-sync.mdc` (Cursor has no Skill tool — it relies on the embedded trigger description to know when to invoke) |
| Cross-project fact, practice, or runbook | global memory (`memory-global/MEMORY.md` + leaf in `memory-global/leaves/`) |
| Project-only fact or runbook | project memory (`<cwd>/.claude/agent-memory/`) |
| File layout, instruction language, git sync rules | this skill's [policy.md](policy.md) |
| Cursor-only (globs, project) | `cursor/rules/*.mdc` |

### Keeping Claude and Cursor in sync

`cursor/rules/claude-code-sync.mdc` is a thin mirror of `CLAUDE.md`. When you change behavior that Cursor must see:

- Edit `CLAUDE.md` first (canonical).
- If the change affects mandatory triggers / coordination cycle / skill descriptions / agent table — also update `cursor/rules/claude-code-sync.mdc` in the **same commit**.
- Heavy detail (full skill body, memory leaves) lives in the linked files; the Cursor rule only carries triggers and entry points.

### Behavioral rule vs domain fact

Classify before placing:

| Type | Signs | Where |
|---|---|---|
| **Behavioral rule** | "always/never", delegation, tool choice, cross-repo pattern with no single product tie | `CLAUDE.md` or skill prompt |
| **Domain fact / runbook** | Relaunch procedures, API/CLI contracts, data paths, ticket-specific detail, prod naming | Memory leaf (global or project) |

Do not push domain runbooks into generic agent prompts or CLAUDE.md. If the user says "remember" or "too specific for an agent" — memory only; revert agent edit.

**Structural form before prose — for a deterministically-decidable rule.** When the behavioral rule is decidable from observable inputs (an ordering, a classification, a gate, a validation, a dispatch — its *rule* part, as opposed to a *perception* part only the model can supply), do not settle for adding prose. Per the CLAUDE.md root principle *"Separate rule from perception; determinize the rule at its proper structural level"*, propose mechanizing the rule part at its proper structural level (engine state / state machine / typed contract / gate / hook) and keep prose only for the perception the model genuinely owns — and **propose that structural form yourself**, do not wait for the user to ask. A local hook or script is a stopgap until a structural home exists; a one-off crutch when a reusable primitive fits is a prune candidate, not the target.

## Improvement areas beyond text

- **Hooks** in `~/.claude/settings.json` (`PreToolUse`, `PostToolUse`, `Stop`, `UserPromptSubmit`) — for automation that must run between turns.
- **Scripts** (`verify-*`, `setup-*`, sync).
- **Memory indexing** (search, tags, SQLite) if global memory grows large.
- **Frontmatter validation** in CI for agents and skills.

Each non-trivial proposal: **problem → options → recommendation → how to verify**.

## Style

Structured report: observations → diagnosis → proposals (priority) → next step.

Reply to the user in the same language as their request. Repository text stays English per [policy.md](policy.md) § Instruction language.
