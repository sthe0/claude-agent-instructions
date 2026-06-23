# Scripts

Automation for the agent-instructions system: setup / symlink wiring, `verify-*` / `lint-*` policy checks, reminder & gate hooks, specialist spawning, and reporting. The table below is machine-checked against the filesystem by [verify-readme.py](verify-readme.py) (every `scripts/*.py|*.sh` and `cursor/scripts/*.py|*.sh` must appear; nothing dangling). Add a row when you add a script; `verify-readme.py --fix` reconciles the row set, then fill in any `TODO` purpose cells by hand.

<!-- inventory:scripts:begin -->
| Script | Purpose |
|---|---|
| [setup-symlinks.sh](setup-symlinks.sh) | Apply runtime symlinks for agents, skills, memory-global |
| [setup-project-memory.sh](setup-project-memory.sh) | Per-project: symlink shared agent memory into the project tree |
| [verify-instructions-sync.sh](verify-instructions-sync.sh) | Check global symlinks and drift |
| [verify-layout-contract.sh](verify-layout-contract.sh) | Compare tree to the layout in `skills/self-improvement/policy.md` |
| [verify-all.py](verify-all.py) | Run all instruction-policy checks (entry point; pre-commit hook uses `--staged`) |
| [verify-language.py](verify-language.py) | Enforce English-by-default policy with adjacent-exception rule |
| [verify-cross-refs.py](verify-cross-refs.py) | Catch broken intra-repo Markdown links and inline-code path references |
| [lint-cursor-mirror.py](../cursor/scripts/lint-cursor-mirror.py) | Detect structural drift between `skills/` and the cursor mirror (flat-skill parity, specialization parity, trigger markers) |
| [install-cursor-links.sh](../cursor/scripts/install-cursor-links.sh) | Apply Cursor-only symlinks (`~/.cursor/rules/*`, `~/.cursor/agents/*`) |
| [link-project-cursor-agents.sh](../cursor/scripts/link-project-cursor-agents.sh) | Symlink `<project>/.cursor/agents/*` → `cursor/agents/` (used by deepagent `setup-local.sh`) |
| [migrate-cursor-namespace.sh](../cursor/scripts/migrate-cursor-namespace.sh) | Migrate global + all `~/arcadia*/robot/deepagent` mounts (`--all-deepagent-mounts`) |
| [lint-permissions.py](lint-permissions.py) | Lint `permissions/*.json` schema (structure, fields, dates, duplicates) |
| [permissions-cli.py](permissions-cli.py) | CLI for workflow-level permissions: `list / check / grant / revoke / digest` |
| [spawn-specialist.py](spawn-specialist.py) | Wrap `claude -p` spawn: recursion cap, budget tier, permissions digest, marker validation, cost log |
| [coordinate-task.py](coordinate-task.py) | Drive the coordination cycle: `plan` (spawn planner, verify plan, print approval-gated run cmd) / `run --approved` (spawn developer, parse marker → exit code) |
| [spawn-cursor-specialist.py](spawn-cursor-specialist.py) | Cursor analogue: wrap `agent -p` specialization spawn — inline SKILL.md, budget→timeout, recursion cap, marker validation, cost log |
| [spawn-cursor-escape.py](spawn-cursor-escape.py) | Wrap `agent -p` overcome-difficulty escape for Cursor: recursion cap, API key, marker validation, cost log |
| [cost-report.py](cost-report.py) | Aggregate `~/.local/log/claude-spawn-costs.jsonl` (totals, by kind/tier/day, depth/marker distributions, refused events) |
| [policy-scorecard.py](policy-scorecard.py) | Per-session ledger + scorecard for the model/sub-agent policy (efficiency + effectiveness, trend vs previous window, Flags); `--ledger-only`, `rate <id> <1-5>` modes. See `memory-global/leaves/policy-effectiveness-tracking.md` |
| [memory-audit.py](memory-audit.py) | Find orphan / broken / stale memory leaves and frontmatter issues (informational; does not gate) |
| [record-experience.py](record-experience.py) | Generate / extend difficulty-centric experience leaves (`search`/`new`/`extend`/`ticket`); auto-maintains the `experience/MEMORY.md` sub-index |
| [verify-self-improvement-edit.py](verify-self-improvement-edit.py) | `commit-msg` gate: require `[self-improvement-reviewed]` in commits that touch `skills/self-improvement/` |
| [lint-prose-length.py](lint-prose-length.py) | Hard ceiling on instruction-file line counts (`CLAUDE.md`, cursor mirror, skill SKILL.md, policy.md) per `config.md` limits |
| [sync-instructions-repo.sh](sync-instructions-repo.sh) | `pull` / `push` this repo |
| [install-git-hooks.sh](install-git-hooks.sh) | Install `pre-commit` (run `verify-all.py --staged`) and `post-commit` (push reminder) |
| [hook-self-critique-reminder.py](hook-self-critique-reminder.py) | PostToolUse Write: nudge to invoke `self-improvement` after writing an experience leaf with a substantive § Self-critique |
| [hook-tracker-reminder.py](hook-tracker-reminder.py) | UserPromptSubmit: detect tracker references in the prompt and remind to invoke `tracker-management` |
| [hook-push-confirmation-reminder.py](hook-push-confirmation-reminder.py) | PreToolUse Bash: nudge to verify user push-confirmation before `git push` / `sync-instructions-repo.sh push` |
| [hook-readme-currency-reminder.py](hook-readme-currency-reminder.py) | PreToolUse Bash: before `git`/`arc commit`, list READMEs next to changed code that aren't in the changeset — verify currency |
| [hook-resolution-reminder.py](hook-resolution-reminder.py) | UserPromptSubmit: nudge to ask for explicit resolution when the user's prompt is brief gratitude |
| [hook-context-growth-reminder.py](hook-context-growth-reminder.py) | UserPromptSubmit: nudge when live context size crosses a band (reads transcript usage); throttled per band per session |
| [hook-prewrite-plan-check.py](hook-prewrite-plan-check.py) | PreToolUse Edit/Write: after 3 code edits with no plan file, one-time nudge to invoke planner |
| [hook-retry-detector.py](hook-retry-detector.py) | PreToolUse Bash: same normalized command 3+ times → nudge to invoke overcome-difficulty |
| [hook-policy-scorecard-due.py](hook-policy-scorecard-due.py) | SessionStart: weekly throttled stderr nudge to run `policy-scorecard.py` (stamp `~/.local/state/claude-policy-scorecard.stamp`); nudge only, does not auto-scan |
| [install-reminder-hooks.sh](install-reminder-hooks.sh) | Idempotently wire the canonical reminder-hook set into machine-local `settings.json` (hooks are not merged from `base.json`) |
| [set-context-cap.sh](set-context-cap.sh) | Set an arbitrary context-size cap (auto-compaction trigger) in tokens — computes `CLAUDE_CODE_DISABLE_1M_CONTEXT` + `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` into `base.json`; max ~830k (83% clamp) |
| [install-sync-cron.sh](install-sync-cron.sh) | Cron: git pull every 10 min (opt-in; not installed by `setup-symlinks.sh`) |
| [hook-arc-mount-search-guard.py](hook-arc-mount-search-guard.py) | PreToolUse `Bash`/`Grep`/`Glob`: deny recursive searches spanning ≥2 arc FUSE mounts under `$HOME` |
| [hook-state-gate.py](hook-state-gate.py) | PreToolUse Edit/Write: deny production-file edits unless agentctl state is at an execution node; weight-aware per-case reason (unclassified → classify, small-change → next-stage, substantive → approve plan, closed → reset) |
| [hook-engine-start.py](hook-engine-start.py) | UserPromptSubmit: surface the agentctl engine each turn — nudge `start`/`classify` (no state), `reset` (prior task closed), or a status+next-step hint (live); never mutates state |
| [lint-settings-base.py](lint-settings-base.py) | Lint `settings/base.json` read-only allowlist against the `classify_action` verb taxonomy in `agentctl.classify` |
| [prewrite-fallback-report.py](prewrite-fallback-report.py) | Aggregate the prewrite-plan-check fallback-firing ledger (`~/.claude/agentctl/prewrite-fallback.jsonl`); informs hook retirement |
| [skill-usage-audit.py](skill-usage-audit.py) | Audit which user-invocable skills are actually used (catalog curation) |
| [tool-usage-report.py](tool-usage-report.py) | Per-task report of specialization spawns and skill / subagent invocations (feeds the experience leaf) |
| [verify-agentctl.py](verify-agentctl.py) | Verify the agentctl engine: schema, transitions, leaves, gate↔guardian-hook consistency |
| [verify-difficulty-lead.py](verify-difficulty-lead.py) | Verify `system-knowledge/` leaves lead with the difficulty they remove |
| [verify-experience-leaf.py](verify-experience-leaf.py) | Verify experience-leaf schema (`difficulty/v1`, required `resolution_confirmed_by_user` frontmatter) |
| [verify-plan-file.py](verify-plan-file.py) | Structural validator for planner output (per `planner` SKILL.md § Plan format) |
| [verify-readme.py](verify-readme.py) | Verify the README inventory sentinels (scripts / flat skills / specializations) match the filesystem; `--fix` reconciles, `--root` for project repos |
| [apply-mcp-local.sh](apply-mcp-local.sh) | Merge `mcp-local/*.json` into `~/.claude/settings.local.json` under `mcpServers` (idempotent) |
| [apply-settings.sh](apply-settings.sh) | Merge the versioned policy base (`settings/base.json`) into machine-local `~/.claude/settings.json` (additive, idempotent) |
| [install-sync-systemd-timer.sh](install-sync-systemd-timer.sh) | User systemd timer: git pull every 10 min (fallback when crontab is denied) |
| [offload-large.sh](offload-large.sh) | Pipe high-volume command output to a head+tail digest; full bytes land in `/tmp/cc-scratch/` |
| [session-start-digest.sh](session-start-digest.sh) | SessionStart bootstrap: cwd, VCS branch / status / log, project-memory listing, in-progress markers in one digest |
| [setup-ccgram.sh](setup-ccgram.sh) | Bootstrap CCGram (Telegram bridge) on a machine; idempotent, does not touch secrets |
<!-- inventory:scripts:end -->
