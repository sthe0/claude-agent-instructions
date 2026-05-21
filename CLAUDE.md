## Do not commit

This file is **local agent instructions** (Cursor / Claude Code). **Do not add or commit it to Arc:** it references machine-specific paths (`~/.venv`, `~/.claude/agents/`, subagents **planner**, **thinker**, **developer**, and optional roles in `~/.claude/agents/`) that differ per developer.

It is listed in `.arcignore`. If it still entered the index — do not include in PR; keep edits local or move shared parts to docs without machine-specific paths.

---

## Instruction language

**Default:** all instruction text in `~/claude-agent-instructions/` and `~/.claude/memory/` (local arc tree) is **English**.

**Exception:** non-English is allowed only with an adjacent note **why English cannot be used** (same paragraph or line above/below). Canonical spec: `~/.claude/memory-global/agent-instructions/instruction-language.md`.

User-facing **replies** use the **same language as the user's request**; that is not an exception to instruction language.

---

Try your best to avoid duplicating code. Explore adjacent files, project files, use the Depagent tool, and code search. Don't hesitate break existing functions and classes into pieces to move common code parts into separate common abstractions.
Do not add obvious or trivial comments. Prefer code expressiveness, readability and clarity over comments.
For **robot/deepagent** (local product): domain runbooks and MCP usage → `~/.claude/memory/deepagent/INDEX.md` via `~/.claude/memory/INDEX.md` (query language, Docker CLI, etc.). Do not duplicate deepagent-specific rules in this file.
For org-wide Arcadia/Yandex infra or unknown terms: optional consultant subagent in `~/.claude/agents/` if present, else intrasearch / wiki per task.
Use ~/.venv virtualenv to run python.
Use Yandex's version control system which is "arc".

## Code search in Arcadia

**Never run `grep`/`rg`/`find` on all of `~/arcadia` or large folders** — Arcadia is FUSE-mounted; recursive traversal hangs the FS and consumes resources. Instead:
- **`ya tool grep`** — text/regex search over the Arcadia index.
- **semantic code search** (MCP `mcp__intrasearch__semantic_code_search` or skill `semantic_code_search`) — natural-language search.
- **`ya tool cs`** / skill `codesearch` — symbols and paths.
- Local `find`/`grep` is allowed **only** inside a known narrow project subfolder (e.g. `~/arcadia/path/to/project/`), not above.

## Parallel work in arc

- Each task with a Tracker ticket (`[A-Z]+-\d+`) — separate parallel mount: `~/arcadia_<TICKET>-<slug>`, branch `<TICKET>-<slug>` (no `users/<login>/` prefix — arc adds it). Mount runbook — `~/.claude/memory/INDEX.md` (do not patch skill `using-arc-multiple-mounts`, it is on a symlink).
- **`arc mount` only from `cd ~`** (not from cwd under `~/arcadia*`). Mount in background, wait for `[mounted]` in log or `arc mount --list`; do not `pkill` on timeout.
- Parallel `arc mount`: always `--object-store …/objects`, `--override-object-store`, **`--allow-other`** (like main `~/arcadia`; **required** for `docker run -v ~/arcadia_*` — without it root in the container cannot see FUSE). Runbook: `~/.claude/memory/INDEX.md`.
- **Never `arc checkout` on main mount `~/arcadia` without explicit permission** — the user works there.
- Ad-hoc questions / small edits **without** a ticket key — current context, no mount needed. Presence of `[A-Z]+-\d+` in task, branch, or workspace **cancels** this exception.
- Leave mount after the task until explicit cleanup command.

## Mandatory workflow: Tracker ticket (`[A-Z]+-\d+`)

Parent agent (Cursor / Claude Code) on a task with a ticket key — **before any** `Edit`/`Write` in Arcadia, `arc checkout`, `arc commit`:

0. **Manager first** — parent runs **Task → manager** in the same turn (§ Mandatory manager). **Forbidden** to start with **planner**, **developer**, or self-coordination (Shell/Grep/transcripts) on a new ticket task. **manager** routes the rest of this checklist.
1. **Understanding** — **manager** / **planner**: read ticket and comments. Numbers, deadlines, abbreviations without explicit source — **find origin** (wiki, code, domain MCP per local memory) or **ask the user** before edits. Read-only exploration before mount is OK.
2. **Mount** — separate parallel mount `~/arcadia_<TICKET>-<slug>` and branch `<TICKET>-<slug>`. Runbook: `~/.claude/memory/INDEX.md`. **Forbidden** to edit ticket code on main `~/arcadia` unless the user explicitly allows.
3. **Plan** — **manager** delegates **planner** (`Task`, `subagent_type: planner`). Plan must state interpretation of key numbers/deadlines and **where exactly** in code/config will change (with justification).
4. **Approval** — show user the plan (or planner summary) and wait for explicit OK / edits. **Forbidden** to delegate **developer** and do `Edit`/`Write`/`arc commit` until plan is approved. Exception: user explicitly said "do it now" / "no approval".
5. **Code** — **manager** delegates **developer** (`Task`). Parent **does not** write production code in Arcadia itself.
6. **Check** — before first edit: cwd/workspace in `~/arcadia_<TICKET>-*`, not `~/arcadia`.

"Prefer" / "better to use" for ticket tasks **does not apply** — understanding, plan approval, delegation, and mount are mandatory.

## Claude Code and Cursor (one source)

Both tools read **the same files** via symlinks from `~/claude-agent-instructions/`:

| Repo file | Claude Code | Cursor |
|-----------|-------------|--------|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` | same path in project (symlink) + rule below |
| `agents/*.md` | `~/.claude/agents/` | `~/.cursor/agents` → `.claude/agents` |
| `memory-global/` | `~/.claude/memory-global/` | same |
| `cursor-rules/claude-code-sync.mdc` | — | `~/.cursor/rules/claude-code-sync.mdc` |
| Local memory (outside git) | `~/.claude/memory/` | same |

Setup and verify: `scripts/setup-symlinks.sh`, `scripts/verify-instructions-sync.sh`, `scripts/verify-layout-contract.sh`. Tree contract (global/local): `~/.claude/memory-global/agent-instructions/file-structure-contract.md`.

**Policy edits** — in the repo; after `commit` **push** is mandatory (see below). Project `robot/deepagent` — only overlay `.cursor/rules/deepagent-project.mdc`, not a copy of the global rule.

## File structure contract

Canonical description of layers (git global, arc local, runtime symlinks): **`~/.claude/memory-global/agent-instructions/file-structure-contract.md`**.

**Keep up to date:** on any move/add of directories, scripts, or global/local split — update contract, `runtime-layout.md`, README § symlinks/scripts in **the same commit** (git and/or arc).

**Reconcile regularly** fact vs description:

1. `~/claude-agent-instructions/scripts/verify-layout-contract.sh` (and `verify-instructions-sync.sh`).
2. On mismatch — fix **doc or tree**, do not leave drift.
3. After instruction refactor — Definition of Done includes passing verify.

Delegate heavy reconcile of local arc layer to **memory** or **self-improvement**; parent does not skip verify after own edits in `~/claude-agent-instructions/`.

## Instructions git repository

Edits in `~/claude-agent-instructions/` (symlinks to `~/.claude/` and `~/.cursor/`). Details: `~/.claude/memory-global/agent-instructions/instructions-git-sync.md`.

1. **Before any edit** — `~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull` (fetch `origin/main`), then **reconcile** session work with pulled instructions (see `instructions-git-sync.md` § After pull).
2. **After edit** — `git add` + `git commit` (without asking the user) + **mandatory** `scripts/sync-instructions-repo.sh push`.
3. **Background** — cron every 10 minutes runs `pull`; on rebase conflict script prefers incoming, else resolve manually.

## Memory and self-improvement

- **memory** — facts: locally `~/.claude/memory/INDEX.md`, globally `~/.claude/memory-global/INDEX.md`.
- **self-improvement** — rules, agents, repo `~/claude-agent-instructions/`; after edits — commit + push (see § Instructions git repository).

### Mandatory self-improvement (parent agent)

**In the same dialog turn** when the user gave substantive feedback, **run** subagent **self-improvement** (`Task`), even if you already made a tactical fix.

Run is **mandatory** if the user message:

- corrects, rejects, or clarifies **your** action, conclusion, plan, or tool choice;
- states a principle or policy ("don't do that", "prefer X", "why Y", "always Z");
- evaluates agent quality (remark, disagreement, process wish);
- proposes changing instructions, agents, memory, repo, skills, workflow.

**Not mandatory** only for neutral confirmation without new info ("ok", "yes do it", "thanks") and for a pure question **without** evaluating or correcting your actions.

In the self-improvement prompt pass: user quote, what you did, what you already changed, expected output (diagnosis + proposed edits in `~/claude-agent-instructions/`).

**Do not end the turn** with only a tactical fix or apology — first **Task** → **self-improvement**. Repeated correction on the same topic (including "why was self-improvement not run") — run again in the **same** turn.

### Mandatory manager (parent agent)

**manager is the mandatory entry agent** for substantive work. Parent **must not** self-coordinate (Shell/Grep/transcript-only loops, or calling **planner** / **developer** first on a new goal — anti-pattern).

#### A. New user task (every time)

When the user **opens or assigns** substantive work — implementation, investigation, fix, ticket, pipeline, refactor, multi-step goal, or a new requirement on an existing topic — parent **in the same turn**:

1. **First delegation** — **Task → manager** (before **planner**, **developer**, mount, or broad code search for that goal).
2. **manager** states need, resources, and who executes next (typically **planner** → approval → **developer**).

**Not a new task** (manager optional): bare "ok" / "thanks"; trivial one-line answer with no tools; user explicitly says skip manager / "direct to planner" / "direct to developer".

#### B. Difficulty (in addition to A if work already started)

Run **Task → manager** again if **any** applies:

- **repeated failure** — same command/branch/run failed twice;
- **blocker** — no access, unclear next step, OOM/CI/WI FAILED without a ready runbook;
- **plan mismatch** — fact ≠ expectation, checklist step skipped, wrong pipeline relaunch;
- **2+ user process corrections** on one topic (not only code);
- **before another attempt** at Nirvana WI, `arc mount`, bundled CLI after failure;
- **session review** or retrospective.

**Continuing** an already-approved plan in the same session (e.g. **developer** executing agreed steps) — no second **manager** unless a trigger in B fires or the user changes scope.

Run is **mandatory** — "prefer delegate manager" **does not apply**. Prompt: user goal or symptom, what was tried, current plan, expected output (for new tasks: routing + plan; for difficulty: diagnosis → replan → action).

**Forbidden** for parent to edit `agents/manager.md` instead of invoking **manager** — delegate **manager** or **self-improvement** for system rules. Domain runbooks — **memory** only.

### Nirvana: after launching WI

After starting a graph (CLI, Nirvana API, docker) — **immediately** report WI id/URL and **poll** until terminal for **all** tracked instances (do not wait for explicit "watch"). WI runbook — `~/.claude/memory/INDEX.md`. End with "monitoring complete" table in the same turn.

Domain runbooks — only leaves via `~/.claude/memory/INDEX.md`, not in generic agent prompts.

## Agents

Delegation — **Task** with `subagent_type` = `name` from `~/.claude/agents/*.md`. For Tracker tickets see § Mandatory workflow above.

- **manager** — **mandatory first** on every new substantive task (§ above); again on difficulties; routes planner + developer + memory; investigate→critique→replan→act.
- **planner** — **mandatory** for Tracker ticket decomposition (invoked by **manager**, plan before code edits).
- **thinker** — verify reasoning.
- **memory** — `~/.claude/memory/`.
- **self-improvement** — **mandatory** on corrections and feedback (see above).
- **developer** — **mandatory** for Arcadia code edits on tickets; parent does not write code itself.
- **Optional subagents** — only if present in `~/.claude/agents/` (`name` + `description`); do not invent roles missing on the machine.

Tracker ticket startup checklist — leaf in `~/.claude/memory/INDEX.md`; global practices — `~/.claude/memory-global/development/` (see **planner**, **manager**).
