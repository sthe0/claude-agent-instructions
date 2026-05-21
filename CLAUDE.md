# Global agent instructions (Claude Code + Cursor)

Synced via `~/claude-agent-instructions/` → `~/.claude/CLAUDE.md`. **Org-specific** rules (arc, Arcadia mounts, Tracker, Nirvana) live in **`~/.claude/memory/`** and **`~/.cursor/rules/org-yandex.mdc`** (local arc tree) — not duplicated here.

---

## Instruction language

**Default:** all instruction text in `~/claude-agent-instructions/` and `~/.claude/memory/` (local tree) is **English**.

**Exception:** non-English only with an adjacent note **why English cannot be used**. Spec: `~/.claude/memory-global/agent-instructions/instruction-language.md`.

User-facing **replies** use the **same language as the user's request**.

---

## Development habits

Try your best to avoid duplicating code. Explore adjacent files, use project search tools and skills. Prefer extending shared abstractions over copy-paste.

Do not add obvious or trivial comments. Prefer clear code over comments.

Use `~/.venv` for Python unless a **local memory** runbook says otherwise.

## Org workflow (local — read INDEX)

On this machine, Yandex/Arcadia/Tracker/Nirvana/arc procedures:

1. Start at **`~/.claude/memory/INDEX.md`** (§ claude-code).
2. Cursor also applies **`~/.cursor/rules/org-yandex.mdc`** (short gates + links to leaves).

**robot/deepagent** product rules → `~/.claude/memory/deepagent/` (not in this file).

For unknown org infra: optional consultant subagent in `~/.claude/agents/` if present, else intrasearch / wiki.

---

## Claude Code and Cursor (one source)

| Repo / local | Runtime |
|--------------|---------|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `agents/*.md` | `~/.claude/agents/`; `~/.cursor/agents` → same |
| `memory-global/` | `~/.claude/memory-global/` |
| `cursor-rules/claude-code-sync.mdc` | `~/.cursor/rules/` |
| Local memory (arc) | `~/.claude/memory/` |
| Local org gates (arc) | `~/.cursor/rules/org-yandex.mdc` |

Setup: `scripts/setup-symlinks.sh`, `scripts/verify-instructions-sync.sh`, `scripts/verify-layout-contract.sh`. Contract: `~/.claude/memory-global/agent-instructions/file-structure-contract.md`.

**Policy edits** — global in this git repo; org overlay in local arc (`junk/the0/agents/`). Project `robot/deepagent` — `deepagent-project.mdc` overlay only.

---

## File structure contract

`~/.claude/memory-global/agent-instructions/file-structure-contract.md` — keep in sync after layout changes; run verify scripts.

---

## Instructions git repository

`~/.claude/memory-global/agent-instructions/instructions-git-sync.md`

1. **Before edit** — `scripts/sync-instructions-repo.sh pull`, then **reconcile** (§ After pull).
2. **After edit** — commit + mandatory `push`.
3. Background cron — `pull` every 10 min.

---

## Memory and self-improvement

- **memory** — `~/.claude/memory/INDEX.md`, `~/.claude/memory-global/INDEX.md`
- **self-improvement** — agents, this repo; commit + push after changes

### Mandatory self-improvement (parent)

**In the same dialog turn** when the user gave substantive feedback, **run** **self-improvement** (`Task`), even if you already made a tactical fix.

Run is **mandatory** if the user message:

- corrects, rejects, or clarifies **your** action, conclusion, plan, or tool choice;
- states a principle or policy ("don't do that", "prefer X", "why Y", "always Z");
- evaluates agent quality (remark, disagreement, process wish);
- proposes changing instructions, agents, memory, repo, skills, workflow.

**Not mandatory** only for neutral confirmation without new info ("ok", "yes do it", "thanks") and for a pure question **without** evaluating or correcting your actions.

Pass to self-improvement: user quote, what you did, what you already changed, expected output (diagnosis + edits in `~/claude-agent-instructions/`).

**Do not end the turn** with only a tactical fix — **Task → self-improvement** first. Repeated correction on the same topic — run again in the **same** turn.

### Mandatory manager (parent)

**manager is the mandatory entry agent** for substantive work. Parent **must not** self-coordinate or call **planner** / **developer** first on a new goal.

#### A. New user task

1. **First delegation** — **Task → manager** (before planner, developer, isolated mount, or broad code search).
2. **manager** routes next steps (typically planner → approval → developer).

**Exceptions:** bare "ok"/"thanks"; trivial one-line answer; user says skip manager / direct to planner|developer.

#### B. Difficulty

**Task → manager** again on: repeated failure; blocker; plan mismatch; 2+ process corrections; before retrying external workflow/VCS/mount/CLI after failure; session review.

**Continuing** an approved plan in the same session — no second manager unless scope changes or B triggers.

Domain runbooks — **memory** only, not generic agent prompts.

### Long-running jobs

After starting an external workflow/job graph — report ids/URLs and monitor until terminal per **local memory** (e.g. Nirvana WI). Do not wait for the user to ask. Details — `~/.claude/memory/INDEX.md`.

---

## Agents

Delegation — **Task**, `subagent_type` from `~/.claude/agents/*.md`.

**Ticket / Arcadia production work** — follow `~/.claude/memory/claude-code/tracker-ticket-workflow.md` (manager routes).

| Agent | Role |
|-------|------|
| **manager** | **First** on new substantive tasks; again on difficulties; routes others |
| **planner** | Decomposition and plan (via manager for tickets) |
| **developer** | Production code in isolated worktree (via manager) |
| **thinker** | Reasoning check |
| **memory** | Domain facts and runbooks |
| **self-improvement** | Instruction and policy fixes |
| Optional | Only if present in `~/.claude/agents/` |

Global practices: `~/.claude/memory-global/development/`.
