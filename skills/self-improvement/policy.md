# Self-improvement policy

Mandatory rules for every edit to `~/claude-agent-instructions/`. Five areas: process-as-code, cache-aware editing, instruction language, file structure, git sync.

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

### What NOT to encode as code

Two carve-outs from the "process belongs in a script" instinct. Both
reject *speculative* machinery — code that exists in service of an
imagined future failure rather than an observed one.

- **No hard line ceilings on memory files.** `MEMORY.md` indices and
  leaves accumulate across sessions; capping them via
  `lint-prose-length.py` (or any analogue) forces pruning that deletes
  useful pointers. The truncation cliff at ~200 lines for `MEMORY.md`
  is a *signal* for the agent to curate, not a gate for a linter.
  Distinguish **instruction surfaces** (`CLAUDE.md`, cursor mirror,
  `SKILL.md`, `policy.md` — loaded into every session prompt → hard
  ceilings protect the token budget) from **content stores**
  (memory — curation by judgment).

- **No premature optimization in design proposals.** When drafting a
  Turn-1 self-improvement proposal, mark each component as either
  *solves a concrete current difficulty* or *speculative*. Drop the
  speculative items unless the user explicitly asks for them. New
  frontmatter fields, validators, helper scripts, generic interfaces:
  start without them. Add when a real failure case appears, not when
  one is imagined. One-off duplication is cheaper than premature
  abstraction; visible noise from a missing validator is cheaper than
  a validator that has no observed failure to prevent. Machinery is
  justified up front only when (a) a single failure is unrecoverable,
  (b) the failure mode has already been observed, or (c) the mechanism
  is a thin wrapper around something already needed.

---

## Ground instructions in the difficulty they remove

Every instruction here is a step in the plan for removing an arbitrary difficulty (`CLAUDE.md` head). When you write or edit one, name the difficulty it removes — its functional ground.

- **Teleological form when the *why* is non-obvious.** Prefer "to achieve X, do Y" over a bare "do Y" wherever X is not self-evident from the rule itself — the agent applies and generalizes a rule far better when it holds the X. Where X is obvious, the bare imperative is fine; an explicit X there is noise (and tokens in the cached prefix).
- **No nameable difficulty → prune candidate.** If you cannot state the difficulty a rule removes, treat that as the signal it is noise: cut or merge it, do not preserve it "just in case."
- This is the instruction-side mirror of the experience-leaf schema (difficulty as the unit) and the `system-knowledge/` rule (lead with the difficulty the component removes).

---

## Authority before a Core edit (ADR-0001)

A protected-Core artifact (`CLAUDE.md`, `config.md`, `skills/**`, `agents/**`, `cursor/**`, `*.mdc`, `scripts/agentctl/**`) can only be landed by a machine with Core commit authority. Before authoring such an edit, gate on `difficulty_channel.authority.is_author()` (a `git push --dry-run` capability probe):

- **Author** → normal spine (`core-difficulty-digest.py` to review clustered difficulties, then `planner → approval → developer`).
- **Non-author** → do **not** edit Core. Run `python3 ~/claude-agent-instructions/scripts/file-difficulty.py --target <artifact> --ground '<desired-vs-actual>' --severity <lvl>` — the machine's channel is auto-selected from `~/.claude-agent/agent-identity.local`. Submission is decoupled from push; an author lands the change later from the accumulated digest. Non-Core targets (memory leaves, project files) are exempt — they are not edit-restricted.

This is the propose-not-execute / no-veto driver: a non-author surfaces difficulties, never blocks or bypasses the human gate. SKILL.md § Non-author machines route Core difficulties to a channel is the operational beat.

---

## Routing a difficulty to its queue by tier

*Difficulty removed: a difficulty filed against the wrong tier's tracker (or with the wrong stream) is invisible to the people who can act on it — an Org-specific refinement filed as a Core GitHub Issue is read by no Yandex author; a planning item filed with the report label pollutes the difficulty digest.*

A difficulty's **tier** decides its destination; the destination's mechanics are structural (the 3-tier model is in [instruction-dev-queues.md](../../memory-global/leaves/instruction-dev-queues.md)). Classifying the tier is **cognition** (the model reads the target and decides); the queue/label **lookup and routing** is **structure** (the per-project field + `file-difficulty.py --queue/--stream` from that leaf's wiring) — do not hand-encode a binding the field already carries.

- **Core** (org-neutral artifact) → the difficulty-channel (GitHub Issues), per § Authority above. Reports carry the `difficulty` label; a planned backlog item carries `backlog` (`--stream backlog`) so the digest never pulls it.
- **Org** (Yandex-specific but cross-project) → Startrek: a **report** to `OOSEVENREPORT` (default `--stream report`), a planned **backlog** item to `OOSEVEN` (`--stream backlog`).
- **Project** (e.g. `robot/deepagent`) → the single queue named by the project's `instruction_queue` field (`agent-project.json`), resolved automatically from the target path by `file-difficulty.py`; backlog and reports collapse onto it because project participants edit project instructions directly.

The model classifies the tier; `--queue`/`--stream` (or the resolved project field) carry it to the right surface.

### Working a queued difficulty: verify actuality first

*Difficulty removed: a queued difficulty records the desired-vs-actual as of filing time; the configuration keeps evolving, so planning work from the ticket text alone spends a full plan-approval-develop cycle on a divergence that may no longer exist.*

Before planning any work on a backlog/report item (any tier), re-verify the recorded desired-vs-actual against the **current** artifact — a cheap subagent returning a `file:line` verdict per item is enough. Already-fixed → close the item citing the evidence (commit / current line), with no plan; still-present → the verdict becomes the plan's triage material. (User-stated rule, 2026-07-03.)

### Author machine: fix-first, backlog-second

*Difficulty removed: an author machine that routes its own core-improvement ideas to the backlog by default converts immediately-actionable fixes into queue latency — the queue exists for machines that CANNOT edit, not as a deferral device for those that can.*

On a machine with Core edit authority (`is_author()` true), the **first** proposal for a core-improvement idea is the immediate fix — now, or right after the current task when the idea is non-critical to it. Routing to the backlog is the explicitly-offered **second** choice ("fix now / queue it?"), never the silent default. The deterministic part is mechanized: `file-difficulty.py` refuses a core-tier filing from an author machine unless the queue choice is explicit (`--queue`). (User-stated rule, 2026-07-03.)

---

## Cache-aware editing

Anthropic prompt caching is **strict-prefix**: any byte change in a file that sits in the cached prompt prefix forces `cache_create` on every byte that follows. Observed cost in the 2026-05-27 deepagent sessions was 1.5M–2.8M `cache_create` tokens per long session, traced largely to mid-task edits of `CLAUDE.md` and `MEMORY.md`. See [token-economy-plan.md](../../memory-global/leaves/token-economy-plan.md).

### Files that count as the cached prefix

These load into every session's prompt; any mid-session edit invalidates downstream cache:

- `~/.claude-agent/CLAUDE.md`
- `~/.claude-agent/config.md`
- `~/.claude-agent/memory-global/MEMORY.md` (auto-imported by `CLAUDE.md`)
- `<project>/.claude/CLAUDE.md` (project)
- `<project>/.claude/agent-memory/MEMORY.md` (auto-loaded via the per-project memory symlink)
- This `policy.md` and the `SKILL.md` files in `skills/<name>/` are loaded only when the skill triggers — but skill **catalog** lines (frontmatter `description`) are in every session's system reminder, so edits to frontmatter are prefix-invalidating.

Leaves under `memory-global/leaves/**` (other than the sub-index `MEMORY.md` files) are loaded on demand and **do not** invalidate the prefix when edited.

### Rule

- **Prefer end-of-task** for any edit to a cached-prefix file. The two-beat workflow already biases toward this: beat 1 = proposal, beat 2 = apply after user confirmation. Land the user-confirmed edit at the close of the task, not in the middle.
- **If the edit must happen mid-task** (a blocking rule the active task itself depends on), batch all related cached-prefix edits into a single `Edit`/`Write` burst so the cache is invalidated at most once, not per change.
- **Leaf-first when possible.** If the proposed change is content that does not have to live in the prefix, write it as a leaf under `memory-global/leaves/**` and update only the one pointer line in the relevant `MEMORY.md` index. The pointer line is small; the leaf body lives off the cached prefix.
- **Volatile content goes to the bottom of `MEMORY.md` indices.** When a `MEMORY.md` mixes stable runbook pointers with volatile pointers (session checkpoints, in-progress tickets), put the volatile section at the end so its frequent edits don't force re-create of the stable section.

This rule is **process discipline**, not a verifier check — placement of "volatile" is judgement-based and not worth coding. The token-economy plan tracks observed regressions and lands new items here.

---

## Instruction language

### Rule

All agent instructions — prompts in `agents/`, skill prompts in `skills/`, `CLAUDE.md`, `cursor/rules/*.mdc`, `memory-global/`, `<project>/.claude/agent-memory/`, README policy sections — are written in **English** by default.

**Exception:** a non-English fragment is allowed only if **immediately next to it** (same paragraph or the adjacent line) there is an explicit note that explains **why English cannot be used** — product constraint, quoted user gate phrase, legal term, etc.

### Not covered by this rule

- **User-facing replies** — same language as the user's request (the language the user writes in). That is output, not stored instruction text. This explicitly includes **technical / design narratives, analyses, and the question + option-label text of every `AskUserQuestion`** — structured or technical content is **not** an exemption.
- **Plan files** in `~/.claude-agent/plans/<name>.md` — per-session artefacts the user reviews and approves; follow the same-language-as-user rule (they are output, not stored instructions). Plans committed *into* the instructions repo or any `.claude/agent-memory/` still follow English-by-default.
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

The repository layout is canonical and lives in one leaf. If disk disagrees — fix **either** the leaf **or** the file tree and symlinks. Do not leave the two diverging.

The full contract is `memory-global/leaves/system-knowledge/instructions-repo-layout.md`:

- **§ Global tree (`~/claude-agent-instructions/`)** — the annotated directory listing + the "forbidden in global `scripts/`" rule.
- **§ Runtime symlinks after `setup-symlinks.sh`** — the runtime-path → repo-source table.
- **§ Project memory symlink (per project, not in this repo)** — the per-project `agent-memory/` symlink wiring.

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

The repo at `~/claude-agent-instructions/` is the single source of truth. Symlinks under `~/.claude-agent/` and `~/.cursor/` point at it.

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
5. **Stash-pop conflicts.** `pull` stashes uncommitted work first; `stash pop` may hit a **modify/delete** or content conflict when your local work touches a file an incoming commit also changed. If your local work *deletes/moves* a file (e.g. a rename/migration) that upstream *edited* — resolve by **porting the upstream edit into the file's new location**, then `git rm` the old path and `git stash drop`. Do not blindly keep "theirs" (loses your migration) or "ours" (loses the upstream edit).

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

**Author-machine carve-out (skip the PR + the second confirm for low-risk content).** On a machine with direct push rights (`difficulty_channel.authority.is_author()` true), an edit that is (a) already **content-approved** in the dialogue and (b) confined to the **memory-leaf / docs-only** class (`memory-global/leaves/**`, `docs/**`, README prose — content, not a loaded behavioral rule) lands **directly to `main`**: do not open a PR (that path is for machines *without* push rights, or when a review round is deliberately wanted), and **fold the push into the same content-approval** — the single "Apply?" click authorizes landing, so steps 2–3's separate push-confirm does not apply. The **behavioral / executable surface** (`CLAUDE.md`, `skills/`, `agents/`, hooks, settings, scripts) keeps both the default push-path and the separate push-confirm of steps 1–4: those changes are higher-stakes and the deliberate second gate earns its cost. *Difficulty removed: a PR detour plus a second confirm on a cheap, already-approved, author-owned content edit spends the user's attention on ceremony the author's own merge rights make redundant.*

If push is rejected (remote ahead): `pull` → resolve conflicts → ask for confirmation again → `push`.

**No push rights to the canonical repo.** Not every operator of these
instructions can push to `origin/main` (read-only clone, untrusted machine, or
simply not a collaborator on `sthe0/claude-agent-instructions`). This is a
supported mode, not an error:

1. Everything up to **commit** is identical — self-improvement edits land as
   **local** commits and the system keeps working from them; push rights are
   never required to *use* the instructions.
2. `sync-instructions-repo.sh push` detects a permission/access failure and
   **degrades to a graceful skip** (logs the reason, keeps the local commit)
   rather than aborting — so a no-rights operator is never left with a cryptic
   crash. Only genuine "remote moved ahead" failures still ask for a `pull`.
3. To get local improvements **upstream** without push rights: fork
   `sthe0/claude-agent-instructions`, push the commit to your fork, and open a
   PR — or hand the commit/patch to someone who can push. Tell the user this is
   the path instead of reporting the push as failed.

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
