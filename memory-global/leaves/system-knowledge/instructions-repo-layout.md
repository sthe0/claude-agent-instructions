---
name: instructions-repo-layout
description: Canonical layout of ~/claude-agent-instructions/ — global tree, runtime symlinks, and project-memory symlink. Prevents layout/disk mismatch when adding, moving, or deleting files.
type: reference
schema: leaf/v1
created: 2026-06-26
last_verified: 2026-07-22
---

## Difficulty

When disk disagrees with the canonical layout — a file exists but isn't in the contract, a symlink points to the wrong target, or a new script is placed in the wrong directory — resolving the mismatch requires knowing the authoritative layout. This leaf is that authority for the global tree, runtime symlinks, and per-project memory symlink.

## Guidance

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
  ccgram-management/SKILL.md         # CCGram Telegram bridge ops (per-machine setup, daily ops, troubleshooting)
  specializations/
    planner/SKILL.md                 # specialization skill (spawned as claude -p)
    developer/SKILL.md
    thinker/SKILL.md
    yandex-cloud-expert/SKILL.md
    tech-writer/SKILL.md             # Russian technical writer / editor (README, docs, plan & comment polishing)
skills-local/                        # gitignored; machine-local single-file skills
mcp-local/                           # gitignored; applied to settings.local.json
cursor/
  README.md
  rules/
    claude-code-sync.mdc             # global Cursor rule (alwaysApply); mirrors CLAUDE.md
  agents/
    README.md
    developer-spawn.md               # Cursor-only specialization wrapper over $CLAUDE_AGENT_HOME/skills/developer/SKILL.md
    planner-spawn.md                 # Cursor-only specialization wrapper over $CLAUDE_AGENT_HOME/skills/planner/SKILL.md
    thinker-spawn.md                 # Cursor-only specialization wrapper over $CLAUDE_AGENT_HOME/skills/thinker/SKILL.md
  scripts/
    install-cursor-links.sh          # wires ~/.cursor/rules/* and ~/.cursor/agents/*
    migrate-cursor-namespace.sh      # helper for migrating other machines / project roots
memory-global/
  MEMORY.md                          # global memory index (auto-memory format)
  leaves/*.md                        # evergreen reference leaves
  leaves/experience/*.md             # post-resolution task experiences (see CLAUDE.md § On task resolution); named YYYY-MM-DD-<slug>.md
  leaves/system-knowledge/*.md       # durable facts about systems/processes/components (see CLAUDE.md § Memory § system-knowledge); slug-only filenames
permissions/                         # operational workflow-level grants (not memory)
  global.json                        # cross-machine grants
  README.md                          # schema + CLI usage
docs/                                # optional documentation
scripts/
  setup-symlinks.sh
  setup-project-memory.sh
  setup-ccgram.sh                      # bootstrap CCGram on a new machine (uv + ccgram + autostart + hooks)
  doctor.sh                            # new-user readiness preflight ("am I ready to start?"): claude CLI, $CLAUDE_AGENT_HOME/CLAUDE.md symlink, engine hooks in settings.json, agentctl, git hooks — read-only
  verify-instructions-sync.sh
  verify-layout-contract.sh
  verify-all.py                        # entry point for instruction-policy checks
  verify-language.py                   # English-by-default policy
  verify-cross-refs.py                 # intra-repo link / inline-path resolution check
  # Cursor mirror lint moved to cursor/scripts/lint-cursor-mirror.py
  verify-self-improvement-edit.py      # commit-msg gate: requires review marker for self-improvement edits
  lint-prose-length.py                 # hard ceiling on CLAUDE.md / cursor mirror / SKILL.md / policy.md
  verify-experience-leaf.py            # require `resolution_confirmed_by_user` + (for schema:difficulty/v1) the difficulty-centric sections on `**/experience/*.md` (PreToolUse hook + verify-all)
  verify-leaf-structure.py             # verify non-experience leaves: schema:leaf/v1 enforces ## Difficulty/Guidance/See also; grandfathered SK leaves keep the difficulty-lead baseline (subsumes verify-difficulty-lead.py)
  record-experience.py                 # generate / extend difficulty-centric experience leaves (search/new/extend/ticket); auto-maintains the experience/MEMORY.md sub-index (see memory-global/leaves/experience-leaf-schema.md)
  hook-self-critique-reminder.py       # PostToolUse Write: nudge to invoke `self-improvement` when an experience leaf has substantive § Self-critique
  hook-tracker-reminder.py             # UserPromptSubmit: detect tracker references (ticket keys, keywords) and nudge to invoke `tracker-management`
  hook-push-confirmation-reminder.py   # PreToolUse Bash: nudge to verify user push-confirmation before `git push` / `sync-instructions-repo.sh push`
  hook-readme-currency-reminder.py     # PreToolUse Bash: before git/arc commit, list READMEs next to changed code that aren't in the changeset — verify currency
  hook-resolution-reminder.py          # UserPromptSubmit: nudge when user reply is brief gratitude — do NOT treat as resolution confirmation
  hook-context-growth-reminder.py      # UserPromptSubmit: nudge when live context size crosses a band (reads transcript usage); throttled per band per session
  install-reminder-hooks.sh            # idempotently wire the canonical reminder-hook set into machine-local settings.json (hooks are not merged from base.json)
  set-context-cap.sh                   # set an arbitrary context-size cap (auto-compaction trigger) by computing CLAUDE_CODE_DISABLE_1M_CONTEXT + CLAUDE_AUTOCOMPACT_PCT_OVERRIDE into base.json
  lint-permissions.py                  # permissions JSON schema check
  permissions-cli.py                   # CLI for permissions/*.json
  spawn-specialist.py                  # `claude -p` spawn wrapper (recursion cap, budget, permissions, cost log)
  cost-report.py                       # aggregate spawn cost log
  tool-usage-report.py                 # aggregate Skill / Agent / spawn invocations per task — feeds experience leaf § Cost, effort, and tool usage
  memory-audit.py                      # informational memory leaves audit
  skill-usage-audit.py                 # informational: which user-invocable skills are actually invoked vs only catalog-loaded (see memory-global/leaves/skill-catalog-curation.md)
  offload-large.sh                     # pipe-through wrapper for Bash outputs > N bytes → /tmp/cc-scratch/ + head+tail digest (see memory-global/leaves/large-tool-output-discipline.md)
  session-start-digest.sh              # bootstrap aggregator: cwd + arc/git state + agent-memory listing in one call (replaces 4–5 separate startup Bash calls)
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

`$CLAUDE_AGENT_HOME` is the isolated config root — `~/.claude-agent` by default (`scripts/lib/config-root.sh` / `lib/config_root.py`); the personal `~/.claude` is only a legacy fallback read on not-yet-migrated machines. Runtime state (`agentctl/`, `plans/`, `projects/`, `projects.d/`) lives beside these symlinks under the same root.

| Runtime path | Source in repo |
|---|---|
| `$CLAUDE_AGENT_HOME/CLAUDE.md` | `CLAUDE.md` |
| `$CLAUDE_AGENT_HOME/config.md` | `config.md` |
| `$CLAUDE_AGENT_HOME/agents/<global>.md` | `agents/<name>.md` (currently none — directory reserved) |
| `$CLAUDE_AGENT_HOME/agents/<local>.md` | `agents-local/*.md` (gitignored) |
| `$CLAUDE_AGENT_HOME/skills/<flat>/` | `skills/<name>/` (excluding the `specializations/` container) |
| `$CLAUDE_AGENT_HOME/skills/<specialization>/` | `skills/specializations/<name>/` — flattened so the catalog sees them by name |
| `$CLAUDE_AGENT_HOME/skills/<local>.md` | `skills-local/*.md` (gitignored) |
| `$CLAUDE_AGENT_HOME/memory-global/` | `memory-global/` |
| `~/.cursor/rules/claude-code-sync.mdc` | `cursor/rules/claude-code-sync.mdc` |
| `~/.cursor/agents/<name>.md` | `cursor/agents/<name>.md` |

Project-specific rules / agents / skills / memory live in **each project's own** `<project_cwd>/.claude/` tree (not in this repo), and are wired by the project's own setup or by `scripts/setup-project-memory.sh` for memory.

### Project memory symlink (per project, not in this repo)

For each project where shared agent memory is desired:

```
<project_cwd>/.claude/agent-memory/        ← committed in the project's git
$CLAUDE_AGENT_HOME/projects/<cwd-hash>/memory  →  <project_cwd>/.claude/agent-memory
```

The symlink is created by `scripts/setup-project-memory.sh`, usually invoked from `<project>/.claude/scripts/setup-local.sh`. The native Claude Code auto-memory mechanism then reads and writes through the symlink, so the actual files live in the project tree and other developers inherit them on clone.

Each product repo may ship `.claude/scripts/setup-local.sh` (Cursor symlinks, skills, memory) and `.claude/scripts/README.md` — not in this global repository.

## See also

- `skills/self-improvement/policy.md` § File structure — repo layout intro and `### On structure change` procedure.
- `scripts/verify-layout-contract.sh` — enforces this contract at commit time.
