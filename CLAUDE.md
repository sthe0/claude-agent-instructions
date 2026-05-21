## Do not commit

This file is **local agent instructions** (Cursor / Claude Code). **Do not add or commit it to Arc:** it references machine-specific paths (`~/.venv`, `~/.claude/agents/`, subagents **planner**, **thinker**, **developer**, and optional roles in `~/.claude/agents/`) that differ per developer.

It is listed in `.arcignore`. If it still entered the index — do not include in PR; keep edits local or move shared parts to docs without machine-specific paths.

---

Try your best to avoid duplicating code. Explore adjacent files, project files, use the Depagent tool, and code search. Don't hesitate break existing functions and classes into pieces to move common code parts into separate common abstractions.
Do not add obvious or trivial comments. Prefer code expressiveness, readability and clarity over comments.
Ask deepagent tool about arcadia code projects and yandex-specific (or unknown) infrastructure. If you see an unknown term, first thing to do is to refer to deepagent tool, not code exploring. Ask deepagent tool about best implementations practices when in doubt. Deepagent tool provides best results when asked in Russian.
Use ~/.venv virtualenv to run python (except data_science CLI: always via Docker, see library/deepagent/data_science/DOCKER_RUN.md)
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

0. **Understanding** — read ticket and comments. Numbers, deadlines, abbreviations ("14 days", TTL, quota, limits) without explicit source in the ticket — **find origin** (wiki, code, deepagent MCP, related PRs) or **ask the user**. Do not tie a number to a random code field and do not start edits until you can explain *where* the number came from and *which* artifact/behavior it describes. Read-only exploration before mount is OK.
1. **Mount** — separate parallel mount `~/arcadia_<TICKET>-<slug>` and branch `<TICKET>-<slug>`. Runbook: `~/.claude/memory/INDEX.md`. **Forbidden** to edit ticket code on main `~/arcadia` unless the user explicitly allows.
2. **Plan** — for **one** ticket without difficulties: **planner** (`Task`, `subagent_type: planner`). For **multiple tickets**, multi-step coordination, or any trigger from § Mandatory manager — **manager** first (it delegates planner). Plan must state interpretation of key numbers/deadlines and **where exactly** in code/config will change (with justification).
3. **Approval** — show user the plan (or planner summary) and wait for explicit OK / edits. **Forbidden** to delegate **developer** and do `Edit`/`Write`/`arc commit` until plan is approved. Exception: user explicitly said "do it now" / "no approval".
4. **Code** — delegate **developer** (`Task`). Parent **does not** write production code in Arcadia itself. Unknown org infra → consultant subagent from `~/.claude/agents/` (if configured) before or with code.
5. **Check** — before first edit: cwd/workspace in `~/arcadia_<TICKET>-*`, not `~/arcadia`.

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

1. **Before any edit** — `~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull` (fetch `origin/main`).
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

Parent **must not** self-coordinate on difficulties (Shell/Grep/transcript-only loops instead of delegation — anti-pattern). **In the same turn** run **Task** → **manager** if **any** trigger applies:

- **repeated failure** — same command/branch/run failed twice;
- **blocker** — no access, unclear next step, OOM/CI/WI FAILED without a ready runbook;
- **plan mismatch** — fact ≠ expectation, checklist step skipped, wrong pipeline relaunch;
- **2+ user process corrections** on one topic (not only code);
- **before another attempt** at Nirvana WI, `arc mount`, bundled CLI after failure — manager first (transcript research + replan);
- **session review**, retrospective, **multiple tickets** or multi-step coordination — **manager before planner** (manager routes planner/developer).

Run is **mandatory** — "prefer delegate manager" **does not apply**. In prompt pass: quote/symptom, what was tried, current plan, expected output (diagnosis → replan → action).

**Forbidden** for parent to edit `agents/manager.md` for "how to work when stuck" — delegate **manager** (run the cycle) or **self-improvement** (system rule in `CLAUDE.md` / sync-rule). Domain runbooks — **memory** only.

### Nirvana: after launching WI

After starting a graph (CLI, Nirvana API, docker) — **immediately** report WI id/URL and **poll** until terminal for **all** tracked instances (do not wait for explicit "watch"). WI runbook — `~/.claude/memory/INDEX.md`. End with "monitoring complete" table in the same turn.

Domain runbooks — only leaves via `~/.claude/memory/INDEX.md`, not in generic agent prompts.

## Agents

Delegation — **Task** with `subagent_type` = `name` from `~/.claude/agents/*.md`. For Tracker tickets see § Mandatory workflow above.

- **manager** — **mandatory** on difficulties (§ above); multi-step tasks and session review; routes planner + developer; investigate→critique→replan→act cycle.
- **planner** — **mandatory** for Tracker ticket decomposition (plan before code edits).
- **thinker** — verify reasoning.
- **memory** — `~/.claude/memory/`.
- **self-improvement** — **mandatory** on corrections and feedback (see above).
- **developer** — **mandatory** for Arcadia code edits on tickets; parent does not write code itself.
- **Optional subagents** — only if present in `~/.claude/agents/` (`name` + `description`); do not invent roles missing on the machine.

Tracker ticket startup checklist — leaf in `~/.claude/memory/INDEX.md`; global practices — `~/.claude/memory-global/development/` (see **planner**, **manager**).
