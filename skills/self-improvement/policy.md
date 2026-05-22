# Self-improvement policy

Mandatory rules for every edit to `~/claude-agent-instructions/`. Three areas: instruction language, file structure, git sync.

---

## Instruction language

### Rule

All agent instructions — prompts in `agents/`, skill prompts in `skills/`, `CLAUDE.md`, `cursor-rules/*.mdc`, `memory-global/`, `<project>/.claude/agent-memory/`, README policy sections — are written in **English** by default.

**Exception:** a non-English fragment is allowed only if **immediately next to it** (same paragraph or the adjacent line) there is an explicit note that explains **why English cannot be used** — product constraint, quoted user gate phrase, legal term, etc.

### Not covered by this rule

- **User-facing replies** — same language as the user's request. That is output, not stored instruction text.
- **Quoted examples** of what the user might say (`"ok"`, `"do it now"`) — keep quotes literal; surrounding prose stays English.
- **Proper nouns and API identifiers** (Tracker, Arcadia, `arc`, ticket keys, model names) — not "another language".

### When editing

| Action | Requirement |
|---|---|
| New paragraph anywhere in this repo | English |
| Translating legacy non-English text | English + remove the obsolete duplicate |
| Must keep non-English text | Add `> **Language exception:** …` (markdown) or `<!-- Language exception: … -->` on the adjacent line |
| Reviewing edits | Reject new non-English prose without an exception note |

### Check

```bash
rg '[\p{Cyrillic}]' ~/claude-agent-instructions --glob '*.md'
```

Each hit must have an adjacent exception comment.

---

## File structure

The repository layout below is canonical. If disk disagrees — fix **either** this document **or** the file tree and symlinks. Do not leave the two diverging.

### Global tree (`~/claude-agent-instructions/`)

```
CLAUDE.md
README.md
agents/*.md                # global subagents (developer, planner, thinker, ...)
agents-local/*.md          # gitignored on non-Arcadia machines; on Arcadia: pointer to arc tree
skills/<name>/SKILL.md     # global skills (overcome-difficulty, self-improvement)
skills/<name>/<extra>.md   # skill-private policy or reference files
skills-local/*.md          # gitignored; non-Arcadia machine-local skills
mcp-local/*.json           # gitignored; applied to settings.local.json via apply-mcp-local.sh
cursor-rules/
  claude-code-sync.mdc
  project-overlay-deepagent.mdc
memory-global/
  MEMORY.md                # global memory index (auto-memory format)
  leaves/*.md              # global memory entries
docs/                      # optional; deferred refactor notes go under docs/deferred/
scripts/
  setup-symlinks.sh
  setup-project-memory.sh
  verify-instructions-sync.sh
  verify-layout-contract.sh
  sync-instructions-repo.sh
  install-git-hooks.sh
  install-sync-cron.sh
  install-sync-systemd-timer.sh
  apply-mcp-local.sh
githooks/post-commit
```

**Forbidden in global `scripts/`:** arc-specific scripts (`sync-junk-agents-arc`, `setup-the0-agents-mount`, …) — they belong in the local `scripts/` tree on the machine.

### Runtime symlinks after `setup-symlinks.sh`

| Runtime path | Source in repo |
|---|---|
| `~/.claude/CLAUDE.md` | `CLAUDE.md` |
| `~/.claude/agents/<global>.md` | `agents/<name>.md` |
| `~/.claude/agents/<local>.md` | `agents-local/*.md` (gitignored fallback) or arc agents-local |
| `~/.claude/skills/<global>/` | `skills/<name>/` (directory symlink) |
| `~/.claude/skills/<local>.md` | `skills-local/*.md` (gitignored) |
| `~/.claude/memory-global/` | `memory-global/` |
| `~/.cursor/rules/claude-code-sync.mdc` | `cursor-rules/claude-code-sync.mdc` |
| `~/.cursor/agents` | `~/.claude/agents` |

### Project memory symlink (per project, not in this repo)

For each project where shared agent memory is desired:

```
<project_cwd>/.claude/agent-memory/        ← committed in the project's git
~/.claude/projects/<cwd-hash>/memory  →  <project_cwd>/.claude/agent-memory
```

The symlink is created by `scripts/setup-project-memory.sh`. The native Claude Code auto-memory mechanism then reads and writes through the symlink, so the actual files live in the project tree and other developers inherit them on clone.

### On structure change

Any add/move/delete of directory, script, or split:

1. Update this document.
2. Update `README.md` § symlinks / scripts.
3. Run `scripts/verify-layout-contract.sh` and `scripts/verify-instructions-sync.sh`.
4. Resolve the mismatch (fix doc **or** disk — not both diverging).

### Mismatch handling

| Symptom | Likely fix |
|---|---|
| File exists, not in contract | Extend contract (if intentional) or remove extra |
| In contract, missing on disk | Restore file or remove from contract |
| Symlink wrong target | `setup-symlinks.sh` |

---

## Git sync (instructions repo)

The repo at `~/claude-agent-instructions/` is the single source of truth. Symlinks under `~/.claude/` and `~/.cursor/` point at it.

### Before editing (mandatory)

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
```

Fetches `origin/main`. On rebase conflict the script prefers **incoming** changes (`--theirs`); if that fails — resolve manually.

### After pull (mandatory reconcile)

When `pull` brought new commits:

1. **Verify tree:** `scripts/verify-instructions-sync.sh` and `scripts/verify-layout-contract.sh` — no FAIL.
2. **Read what changed:** `git log -3 --oneline`; for non-trivial diffs `git diff HEAD@{1}..HEAD --stat`.
3. **Reconcile active work.** Compare the open plan, pending edits, and delegation choices against the new policy. If pulled rules **contradict** what you already did or planned this session:
   - stop further production edits until aligned,
   - adjust the plan or revert local tactical changes,
   - tell the user what conflicted (file / section) and which rule now applies.
4. **Do not assume** the pre-pull mental model still holds for any gate.

Background cron pull does **not** replace this reconcile at the start of a session that will edit code or instructions.

### After editing (mandatory)

```bash
cd ~/claude-agent-instructions
git add -A && git commit -m "…"
~/claude-agent-instructions/scripts/sync-instructions-repo.sh push
```

**Every** commit is followed by **push** to `origin` without asking the user.

If push is rejected (remote ahead): `pull` → resolve conflicts → `push` again.

### Background pull (every 10 minutes)

```bash
~/claude-agent-instructions/scripts/install-sync-cron.sh
```

Cron line (repo path substituted on install): `*/10 * * * * …/sync-instructions-repo.sh pull`.
Log: `~/.local/log/claude-agent-instructions-sync.log`.

If `crontab` is forbidden (corp VM): `scripts/install-sync-systemd-timer.sh`.

### Git hooks

```bash
~/claude-agent-instructions/scripts/install-git-hooks.sh
```

`post-commit` runs `sync-instructions-repo.sh push` automatically. Duplicates the explicit agent push, in case the user committed manually.

### Scripts

| Script | Purpose |
|---|---|
| `sync-instructions-repo.sh pull` | fetch + rebase / ff-only |
| `sync-instructions-repo.sh push` | push if local commits exist |
| `sync-instructions-repo.sh sync` | pull, then push |
| `install-sync-cron.sh` | cron line (pull every 10 min) |
| `install-sync-systemd-timer.sh` | user systemd timer (if cron unavailable) |
| `install-git-hooks.sh` | post-commit → auto-push |
| `setup-symlinks.sh` | apply the runtime symlinks |
| `setup-project-memory.sh` | per-project: symlink shared agent memory into the project tree |
| `verify-instructions-sync.sh` | check symlinks and drift |
| `verify-layout-contract.sh` | tree vs the layout in this document |
