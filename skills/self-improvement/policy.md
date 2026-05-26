# Self-improvement policy

Mandatory rules for every edit to `~/claude-agent-instructions/`. Four areas: process-as-code, instruction language, file structure, git sync.

---

## Process as code

A rule that reduces to a deterministic check or a fixed sequence of steps is
a **process**: it belongs in a script (plus a hook or `verify-all` wiring),
not in prose. Prose then describes intent and points at the script — one
source of truth.

**Cognition** — choosing between strategies, judging quality, restating
user intent, designing new abstractions — stays in prose (`CLAUDE.md`,
skill bodies, leaves). Cognition is what the LLM does; process is what
the harness enforces.

When formulating a new rule during self-improvement, classify first:

- "Verify property X on file Y" → a check script + `verify-all.py` (gate)
  or a stand-alone CLI (informational).
- "Always do A, B, C in order" → a workflow script (e.g. under
  `scripts/workflows/<name>.py` once that pattern exists), not a prose
  checklist.
- "Think about Z before doing W" → prose, in the narrowest file every
  session that needs Z will load.

Do not write the same procedure twice — once as "you must …" in prose and
once as `verify-X.py`. Code is the single source of truth; prose points
to it.

---

## Instruction language

### Rule

All agent instructions — prompts in `agents/`, skill prompts in `skills/`, `CLAUDE.md`, `cursor-rules/*.mdc`, `memory-global/`, `<project>/.claude/agent-memory/`, README policy sections — are written in **English** by default.

**Exception:** a non-English fragment is allowed only if **immediately next to it** (same paragraph or the adjacent line) there is an explicit note that explains **why English cannot be used** — product constraint, quoted user gate phrase, legal term, etc.

### Not covered by this rule

- **User-facing replies** — same language as the user's request. That is output, not stored instruction text.
- **Plan files** in `~/.claude/plans/<name>.md` — per-session artefacts the user reviews and approves; follow the same-language-as-user rule (they are output, not stored instructions). Plans committed *into* the instructions repo or any `.claude/agent-memory/` still follow English-by-default.
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
# Full repo scan (use this when reviewing or after large edits):
python3 ~/claude-agent-instructions/scripts/verify-language.py

# Staged-only scan (what the pre-commit hook runs):
python3 ~/claude-agent-instructions/scripts/verify-language.py --staged
```

The script strips quoted regions (`"..."`, `«...»`, `` `...` ``) and fenced
code blocks before checking — so quoted user examples and code do not need an
exception note. Any other Cyrillic prose must have an adjacent exception
comment (within 3 lines): `<!-- Language exception: ... -->` or
`> **Language exception:** ...`.

The pre-commit hook installed by `scripts/install-git-hooks.sh` runs
`verify-all.py --staged`, which includes the language check. It blocks any
commit with an unannotated violation.

---

## File structure

The repository layout below is canonical. If disk disagrees — fix **either** this document **or** the file tree and symlinks. Do not leave the two diverging.

### Global tree (`~/claude-agent-instructions/`)

```
CLAUDE.md
config.md                            # numeric coordination constants — single source of truth
README.md
agents/                              # reserved for future Task-spawned subagents
  README.md
agents-local/                        # gitignored; per-machine subagents
  README.md
skills/                              # flat skills + specializations container
  overcome-difficulty/SKILL.md       # flat skill (invoked inline)
  self-improvement/SKILL.md + policy.md
  tracker-management/SKILL.md
  specializations/
    planner/SKILL.md                 # specialization skill (spawned as claude -p)
    developer/SKILL.md
    thinker/SKILL.md
    yandex-cloud-expert/SKILL.md
skills-local/                        # gitignored; machine-local single-file skills
mcp-local/                           # gitignored; applied to settings.local.json
cursor-rules/
  claude-code-sync.mdc               # global Cursor rule (alwaysApply); mirrors CLAUDE.md
memory-global/
  MEMORY.md                          # global memory index (auto-memory format)
  leaves/*.md                        # evergreen reference leaves
  leaves/experience/*.md             # post-resolution task experiences (see CLAUDE.md § On task resolution); named YYYY-MM-DD-<slug>.md
permissions/                         # operational workflow-level grants (not memory)
  global.json                        # cross-machine grants
  README.md                          # schema + CLI usage
docs/                                # optional documentation
scripts/
  setup-symlinks.sh
  setup-project-memory.sh
  verify-instructions-sync.sh
  verify-layout-contract.sh
  verify-all.py                        # entry point for instruction-policy checks
  verify-language.py                   # English-by-default policy
  verify-cross-refs.py                 # intra-repo link / inline-path resolution check
  lint-cursor-mirror.py                # cursor-rules vs skills/ structural parity
  verify-self-improvement-edit.py      # commit-msg gate: requires review marker for self-improvement edits
  lint-prose-length.py                 # hard ceiling on CLAUDE.md / cursor mirror / SKILL.md / policy.md
  verify-experience-leaf.py            # require `resolution_confirmed_by_user` frontmatter on `**/experience/*.md` (PreToolUse hook + verify-all)
  hook-self-critique-reminder.py       # PostToolUse Write: nudge to invoke `self-improvement` when an experience leaf has substantive § Self-critique
  hook-tracker-reminder.py             # UserPromptSubmit: detect tracker references (ticket keys, keywords) and nudge to invoke `tracker-management`
  lint-permissions.py                  # permissions JSON schema check
  permissions-cli.py                   # CLI for permissions/*.json
  spawn-specialist.py                  # `claude -p` spawn wrapper (recursion cap, budget, permissions, cost log)
  cost-report.py                       # aggregate spawn cost log
  memory-audit.py                      # informational memory leaves audit
  sync-instructions-repo.sh
  install-git-hooks.sh
  install-sync-cron.sh
  install-sync-systemd-timer.sh
  apply-mcp-local.sh
githooks/
  pre-commit                           # runs verify-all.py --staged
  commit-msg                           # runs verify-self-improvement-edit.py
  post-commit                          # push reminder
```

**Forbidden in global `scripts/`:** project-specific or machine-specific scripts (Arcadia mount helpers, deepagent runbook scripts, etc.) — those belong in the relevant project's own `.claude/scripts/` tree.

### Runtime symlinks after `setup-symlinks.sh`

| Runtime path | Source in repo |
|---|---|
| `~/.claude/CLAUDE.md` | `CLAUDE.md` |
| `~/.claude/config.md` | `config.md` |
| `~/.claude/agents/<global>.md` | `agents/<name>.md` (currently none — directory reserved) |
| `~/.claude/agents/<local>.md` | `agents-local/*.md` (gitignored) |
| `~/.claude/skills/<flat>/` | `skills/<name>/` (excluding the `specializations/` container) |
| `~/.claude/skills/<specialization>/` | `skills/specializations/<name>/` — flattened so the catalog sees them by name |
| `~/.claude/skills/<local>.md` | `skills-local/*.md` (gitignored) |
| `~/.claude/memory-global/` | `memory-global/` |
| `~/.cursor/rules/claude-code-sync.mdc` | `cursor-rules/claude-code-sync.mdc` |
| `~/.cursor/agents` | `~/.claude/agents` |

Project-specific rules / agents / skills / memory live in **each project's own** `<project_cwd>/.claude/` tree (not in this repo), and are wired by the project's own setup or by `scripts/setup-project-memory.sh` for memory.

### Project memory symlink (per project, not in this repo)

For each project where shared agent memory is desired:

```
<project_cwd>/.claude/agent-memory/        ← committed in the project's git
~/.claude/projects/<cwd-hash>/memory  →  <project_cwd>/.claude/agent-memory
```

The symlink is created by `scripts/setup-project-memory.sh`, usually invoked from `<project>/.claude/scripts/setup-local.sh`. The native Claude Code auto-memory mechanism then reads and writes through the symlink, so the actual files live in the project tree and other developers inherit them on clone.

Each product repo may ship `.claude/scripts/setup-local.sh` (Cursor symlinks, skills, memory) and `.claude/scripts/README.md` — not in this global repository.

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

Two steps, in order. Both are required — the second is not implied by the first.

**1. Pull origin/main:**

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
```

Fetches `origin/main`. On rebase conflict the script prefers **incoming** changes (`--theirs`); if that fails — resolve manually.

**2. Re-read the target files from disk** before reasoning about the edit. Conversation context can carry stale copies of any instruction file — loaded at session start or before an in-session edit. Use `Read` on each file you will touch (CLAUDE.md, the target agent/skill, this policy, memory leaves). On-disk content wins over what you remember in context; rebuild the edit on top of the current state.

### After pull (mandatory reconcile)

When `pull` brought new commits:

1. **Verify tree:** `scripts/verify-instructions-sync.sh` and `scripts/verify-layout-contract.sh` — no FAIL.
2. **Read what changed:** `git log -3 --oneline`; for non-trivial diffs `git diff HEAD@{1}..HEAD --stat`.
3. **Reconcile active work.** Compare the open plan, pending edits, and delegation choices against the new policy. If pulled rules **contradict** what you already did or planned this session:
   - stop further production edits until aligned,
   - adjust the plan or revert local tactical changes,
   - tell the user what conflicted (file / section) and which rule now applies.
4. **Do not assume** the pre-pull mental model still holds for any gate.

If cron pull is enabled (opt-in, see below), it does **not** replace this reconcile at the start of a session that will edit code or instructions.

### After editing (mandatory)

```bash
cd ~/claude-agent-instructions
git add -A && git commit -m "…"
# push only after explicit user confirmation (see below)
~/claude-agent-instructions/scripts/sync-instructions-repo.sh push
```

**Editing this skill itself.** When the staged change touches any file under
`skills/self-improvement/`, the `commit-msg` hook requires the literal marker
`[self-improvement-reviewed]` in the commit message body. This forces a
deliberate acknowledgment: editing the skill that processes user feedback
changes future invocations in the same conversation, so the change is
explicitly reviewed before it lands.

1. **Commit** locally after every edit batch (message explains the change).
2. **Prepare for push:** `git status`, `git log -1`, run verifiers if layout changed; tell the user the commit is ready and what will go to `origin/main`.
3. **Push** only after the user explicitly confirms (e.g. «push», «да, пушь», «опубликуй инструкции»). Do **not** run `sync-instructions-repo.sh push` on your own after commit.
4. If the user declines or defers push — leave the commit local; do not push.

If push is rejected (remote ahead): `pull` → resolve conflicts → ask for confirmation again → `push`.

### Background pull (opt-in, every 10 minutes)

Background auto-pull is **not installed by default**; `setup-symlinks.sh` does not enable it. Install manually only if you want it:

```bash
~/claude-agent-instructions/scripts/install-sync-cron.sh
```

Cron line (repo path substituted on install): `*/10 * * * * …/sync-instructions-repo.sh pull`.
Log: `~/.local/log/claude-agent-instructions-sync.log`.

If `crontab` is forbidden (corp VM): `scripts/install-sync-systemd-timer.sh`.

To disable later: `crontab -l | grep -v claude-agent-instructions | crontab -`.

### Git hooks

```bash
~/claude-agent-instructions/scripts/install-git-hooks.sh
```

`post-commit` only reminds that push needs user confirmation — it does **not** auto-push (see § After editing).

### Scripts

| Script | Purpose |
|---|---|
| `sync-instructions-repo.sh pull` | fetch + rebase / ff-only |
| `sync-instructions-repo.sh push` | push if local commits exist |
| `sync-instructions-repo.sh sync` | pull, then push |
| `install-sync-cron.sh` | cron line (pull every 10 min) — opt-in, run manually if desired |
| `install-sync-systemd-timer.sh` | user systemd timer (if cron unavailable) — opt-in |
| `install-git-hooks.sh` | post-commit → reminder (no auto-push) |
| `setup-symlinks.sh` | apply the runtime symlinks |
| `setup-project-memory.sh` | per-project: symlink shared agent memory into the project tree |
| `verify-instructions-sync.sh` | check symlinks and drift |
| `verify-layout-contract.sh` | tree vs the layout in this document |
