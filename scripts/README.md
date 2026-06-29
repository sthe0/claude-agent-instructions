# Scripts

Automation for the agent-instructions system: setup / symlink wiring, `verify-*` / `lint-*` policy checks, reminder & gate hooks, specialist spawning, and reporting. The table below is machine-checked against the filesystem by [verify-readme.py](verify-readme.py) (every `scripts/*.py|*.sh` and `cursor/scripts/*.py|*.sh` must appear; nothing dangling). Add a row when you add a script; `verify-readme.py --fix` reconciles the row set, then fill in any `TODO` purpose cells by hand.

<!-- inventory:scripts:begin -->
| Script | Purpose |
|---|---|
| [install-cursor-links.sh](../cursor/scripts/install-cursor-links.sh) | Apply Cursor-only symlinks (`~/.cursor/rules/*`, `~/.cursor/agents/*`) |
| [link-project-cursor-agents.sh](../cursor/scripts/link-project-cursor-agents.sh) | Symlink `<project>/.cursor/agents/*` → `cursor/agents/` (used by deepagent `setup-local.sh`) |
| [lint-cursor-mirror.py](../cursor/scripts/lint-cursor-mirror.py) | Detect structural drift between `skills/` and the cursor mirror (flat-skill parity, specialization parity, trigger markers) |
| [migrate-cursor-namespace.sh](../cursor/scripts/migrate-cursor-namespace.sh) | Migrate global + all `~/arcadia*/robot/deepagent` mounts (`--all-deepagent-mounts`) |
| [apply-mcp-local.sh](apply-mcp-local.sh) | Merge `mcp-local/*.json` into `~/.claude/settings.local.json` under `mcpServers` (idempotent) |
| [apply-settings.sh](apply-settings.sh) | Merge the versioned policy base (`settings/base.json`) into machine-local `~/.claude/settings.json` (additive, idempotent) |
| [configure-identity.sh](configure-identity.sh) | Idempotent: create `~/.claude/agent-identity.local` with an auto-detected `difficulty_channel` (`github` off-corp, `startrek` on internal Yandex signals) if the file is absent; called by `setup-symlinks.sh` |
| [consensus-synthesizer.py](consensus-synthesizer.py) | Active synthesizer (ADR-0001 Variant D): normalize → cluster (reuses the digest) → detect-conflict → induce-invariant (critique primitive: commonality/difference) → ranked menu → promote-to-layer. PROPOSES only — no veto, no auto-edit; Core stays byte-unchanged until a human approves via planner → approval → developer |
| [coordinate-task.py](coordinate-task.py) | Drive the coordination cycle: `plan` (spawn planner, verify plan, print approval-gated run cmd) / `run --approved` (spawn developer, parse marker → exit code) |
| [core-difficulty-digest.py](core-difficulty-digest.py) | Pull difficulty records from every configured channel, cluster by functional ground (reusing `record-experience.py` search), compute mass = Σseverity, and flag clusters ≥ `core-difficulty-mass-threshold` or any critical (ADR-0001 difficulty-accumulation); flags only — never edits Core |
| [file-difficulty.py](file-difficulty.py) | Submit a Core difficulty record to the configured channel (Startrek or GitHub Issues); non-author machines use this instead of editing Core directly (ADR-0001 non-author path) |
| [cost-report.py](cost-report.py) | Aggregate `~/.local/log/claude-spawn-costs.jsonl` (totals, by kind/tier/day, depth/marker distributions, refused events) |
| [doctor.sh](doctor.sh) | Readiness preflight ("am I ready to start?") for a new user: checks `claude` CLI on PATH, `~/.claude/CLAUDE.md` symlink into the repo, engine hooks (state-gate + engine-start) wired in settings.json, `agentctl` importable, git hooks installed; read-only, exits non-zero on any hard failure |
| [hook-arc-mount-search-guard.py](hook-arc-mount-search-guard.py) | PreToolUse `Bash`/`Grep`/`Glob`: deny recursive searches spanning ≥2 arc FUSE mounts under `$HOME` |
| [hook-context-growth-reminder.py](hook-context-growth-reminder.py) | UserPromptSubmit: nudge when live context size crosses a band (reads transcript usage); throttled per band per session |
| [hook-engine-start.py](hook-engine-start.py) | UserPromptSubmit: keep the agentctl engine the default control path — auto-start a session via `start --if-absent` (no state) then steer to `classify`, `reset` line (prior task closed), or a status+next-step hint (live); only mutates via idempotent start, never classify |
| [hook-experience-record-reminder.py](hook-experience-record-reminder.py) | UserPromptSubmit: state-aware nudge (reads the agentctl `experience` plugin bag) when the experience leaf flow is incomplete; loudest at RESOLUTION where `resolve` is blocked, silent when the plugin is inactive or the flow is complete |
| [hook-language-reminder.py](hook-language-reminder.py) | UserPromptSubmit: advisory — when the prompt is meaningfully Cyrillic, remind that user-facing replies (incl. AskUserQuestion text) must match the user's language; never blocks |
| [hook-long-job-arm.py](hook-long-job-arm.py) | PreToolUse Bash: advisory — when a command looks like a long external-job launch (`nohup`, orchestrator launch verb), nudge to arm the detached-poller + ScheduleWakeup monitoring recipe; fires once per session, never blocks |
| [hook-memory-accessed-stamp.py](hook-memory-accessed-stamp.py) | PostToolUse Read: stamp `last_accessed: <today>` onto a memory leaf when it is opened with Read (the only writer of the temporal-frontmatter usage signal); idempotent, day-granular, frontmatter-less files untouched, always exits 0 / never blocks |
| [hook-memory-consistency.py](hook-memory-consistency.py) | PreToolUse Write/Edit: non-blocking reminder (exit 0 always) when a memory leaf being written has missing or invalid frontmatter (`name`, `description`, `type`, and temporal `created`/`last_verified`/`last_accessed`); covers all three memory scopes (global, project, personal) |
| [hook-policy-scorecard-due.py](hook-policy-scorecard-due.py) | SessionStart: weekly throttled stderr nudge to run `policy-scorecard.py` (stamp `~/.local/state/claude-policy-scorecard.stamp`); nudge only, does not auto-scan |
| [hook-prewrite-plan-check.py](hook-prewrite-plan-check.py) | PreToolUse Edit/Write: after 3 code edits with no plan file, one-time nudge to invoke planner |
| [hook-push-confirmation-reminder.py](hook-push-confirmation-reminder.py) | PreToolUse Bash: nudge to verify user push-confirmation before `git push` / `sync-instructions-repo.sh push` |
| [hook-readme-currency-reminder.py](hook-readme-currency-reminder.py) | PreToolUse Bash: before `git`/`arc commit`, list READMEs next to changed code that aren't in the changeset — verify currency |
| [hook-resolution-reminder.py](hook-resolution-reminder.py) | UserPromptSubmit: nudge to ask for explicit resolution when the user's prompt is brief gratitude |
| [hook-retry-detector.py](hook-retry-detector.py) | PreToolUse Bash: same normalized command 3+ times → nudge to invoke overcome-difficulty |
| [hook-self-critique-reminder.py](hook-self-critique-reminder.py) | PostToolUse Write: nudge to invoke `self-improvement` after writing an experience leaf with a substantive § Self-critique |
| [hook-self-improvement-reminder.py](hook-self-improvement-reminder.py) | UserPromptSubmit: precision-first two-tier scan for agent-behavior feedback in the prompt (explicit self-improvement mention; strong imperatives; corrective patterns gated on an agent-ref) → nudge to invoke `self-improvement` |
| [hook-skill-first.py](hook-skill-first.py) | PreToolUse Bash: advisory — when a command hand-rolls a known domain op (arc VCS, ya vault, arc grep, tracker REST, …) nudge to prefer the matching Skill; fires once per operation-class per session, never blocks |
| [hook-state-gate.py](hook-state-gate.py) | PreToolUse Edit/Write: deny production-file edits unless agentctl state is at an execution node; weight-aware per-case reason (unclassified → classify, small-change → next-stage, substantive → approve plan, closed → reset) |
| [hook-sigma-sentinel-due.py](hook-sigma-sentinel-due.py) | SessionStart: 30-day throttled stderr nudge to run `sigma-sentinel.py` and check the σ build-trigger (stamp `~/.local/state/claude-sigma-sentinel.stamp`); nudge only, does not auto-scan |
| [hook-tracker-publish-reminder.py](hook-tracker-publish-reminder.py) | UserPromptSubmit: state-aware nudge (reads the agentctl `tracker` plugin bag) when mandatory ticket publications are unrecorded; loudest at RESOLUTION, silent when the tracker plugin is inactive or complete |
| [hook-tracker-reminder.py](hook-tracker-reminder.py) | UserPromptSubmit: detect tracker references in the prompt and remind to invoke `tracker-management` |
| [install-git-hooks.sh](install-git-hooks.sh) | Install `pre-commit` (run `verify-all.py --staged`) and `post-commit` (push reminder) |
| [install-reminder-hooks.sh](install-reminder-hooks.sh) | Idempotently wire the canonical reminder-hook set into machine-local `settings.json` (hooks are not merged from `base.json`) |
| [install-sync-cron.sh](install-sync-cron.sh) | Cron: git pull every 10 min (opt-in; not installed by `setup-symlinks.sh`) |
| [install-sync-systemd-timer.sh](install-sync-systemd-timer.sh) | User systemd timer: git pull every 10 min (fallback when crontab is denied) |
| [lint-hooks-executable.py](lint-hooks-executable.py) | Verify every `hook-*.py` carries the executable bit on disk and in git (a non-+x hook fails silently with "Permission denied") |
| [lint-permissions.py](lint-permissions.py) | Lint `permissions/*.json` schema (structure, fields, dates, duplicates) |
| [lint-prose-length.py](lint-prose-length.py) | Hard ceiling on instruction-file line counts (`CLAUDE.md`, cursor mirror, skill SKILL.md, policy.md) per `config.md` limits |
| [lint-settings-base.py](lint-settings-base.py) | Lint `settings/base.json` read-only allowlist against the `classify_action` verb taxonomy in `agentctl.classify` |
| [memory-audit.py](memory-audit.py) | Find orphan / broken / stale memory leaves and frontmatter issues (informational; does not gate) |
| [memory_dates.py](memory_dates.py) | Shared temporal-frontmatter validator (importable module): parse/validate `created`/`last_verified`/`last_accessed` ISO dates, enforce `last_verified >= created`; reused by the verifiers and the consistency hook |
| [offload-large.sh](offload-large.sh) | Pipe high-volume command output to a head+tail digest; full bytes land in `/tmp/cc-scratch/` |
| [permissions-cli.py](permissions-cli.py) | CLI for workflow-level permissions: `list / check / grant / revoke / digest` |
| [policy-scorecard.py](policy-scorecard.py) | Per-session ledger + scorecard for the model/sub-agent policy (efficiency + effectiveness, trend vs previous window, Flags); `--ledger-only`, `rate <id> <1-5>` modes. See `memory-global/leaves/policy-effectiveness-tracking.md` |
| [prewrite-fallback-report.py](prewrite-fallback-report.py) | Aggregate the prewrite-plan-check fallback-firing ledger (`~/.claude/agentctl/prewrite-fallback.jsonl`); informs hook retirement |
| [proc_tree.py](proc_tree.py) | Stdlib process-tree supervision (POSIX): `launch_supervised` (child as session/group leader), `kill_tree` (killpg the group, else /proc PPID walk; idempotent, refuses pid≤1), `install_teardown` (reap subtree on SIGINT/SIGTERM/SIGHUP + atexit) — no orphaned spawn grandchildren |
| [record-experience.py](record-experience.py) | Generate / extend difficulty-centric experience leaves (`search`/`new`/`extend`/`ticket`); auto-maintains the `experience/MEMORY.md` sub-index |
| [session-start-digest.sh](session-start-digest.sh) | SessionStart bootstrap: cwd, VCS branch / status / log, project-memory listing, in-progress markers in one digest |
| [set-context-cap.sh](set-context-cap.sh) | Set an arbitrary context-size cap (auto-compaction trigger) in tokens — computes `CLAUDE_CODE_DISABLE_1M_CONTEXT` + `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` into `base.json`; max ~830k (83% clamp) |
| [setup-ccgram.sh](setup-ccgram.sh) | Bootstrap CCGram (Telegram bridge) on a machine; idempotent, does not touch secrets |
| [setup-org.sh](setup-org.sh) | One-command org-portable onboarding wizard: wraps `configure-identity.sh` (channel auto-detect → per-machine identity) and prints an onboarding checklist; idempotent, no network |
| [setup-project-memory.sh](setup-project-memory.sh) | Per-project: symlink shared agent memory into the project tree |
| [setup-symlinks.sh](setup-symlinks.sh) | Apply runtime symlinks for agents, skills, memory-global |
| [skill-usage-audit.py](skill-usage-audit.py) | Audit which user-invocable skills are actually used (catalog curation) |
| [spawn-cursor-escape.py](spawn-cursor-escape.py) | Wrap `agent -p` overcome-difficulty escape for Cursor: recursion cap, API key, marker validation, cost log |
| [spawn-cursor-specialist.py](spawn-cursor-specialist.py) | Cursor analogue: wrap `agent -p` specialization spawn — inline SKILL.md, budget→timeout, recursion cap, marker validation, cost log |
| [spawn-specialist.py](spawn-specialist.py) | Wrap `claude -p` spawn: recursion cap, budget tier, permissions digest, marker validation, cost log |
| [stamp-memory-dates.py](stamp-memory-dates.py) | One-shot backfill of `created`/`last_verified` into existing memory leaves (derive from git add-date → filename prefix → mtime); `--scope all/global/project/personal`, dry-run default, `--apply` to write; idempotent, never touches the body |
| [sync-instructions-repo.sh](sync-instructions-repo.sh) | `pull` / `push` this repo |
| [sigma-sentinel.py](sigma-sentinel.py) | Read-only σ build-trigger instrument (ADR-0002): measures condition (A) — re-refutation of an already-promoted principle by a `tier:1` leaf, flags at `principle-promotion-threshold`; reports the cheap (C) proxy (corpus size / near-dup density); logs (B) + dear-(C) as deferred. Measures, never decides or builds |
| [tool-usage-report.py](tool-usage-report.py) | Per-task report of specialization spawns and skill / subagent invocations (feeds the experience leaf) |
| [verify-agentctl.py](verify-agentctl.py) | Verify the agentctl engine: schema, transitions, leaves, gate↔guardian-hook consistency |
| [verify-all.py](verify-all.py) | Run all instruction-policy checks (entry point; pre-commit hook uses `--staged`) |
| [verify-cross-refs.py](verify-cross-refs.py) | Catch broken intra-repo Markdown links and inline-code path references |
| [verify-doc-concepts.py](verify-doc-concepts.py) | Verify foundational-concept doc-bindings: each registered concept's doc section heading exists and its code anchors are importable |
| [verify-experience-leaf.py](verify-experience-leaf.py) | Verify experience-leaf schema (`difficulty/v1`, required `resolution_confirmed_by_user` frontmatter) |
| [verify-instructions-sync.sh](verify-instructions-sync.sh) | Check global symlinks and drift |
| [verify-language.py](verify-language.py) | Enforce English-by-default policy with adjacent-exception rule |
| [verify-layout-contract.sh](verify-layout-contract.sh) | Compare tree to the layout in `skills/self-improvement/policy.md` |
| [verify-leaf-structure.py](verify-leaf-structure.py) | Verify non-experience leaves: `schema:leaf/v1` enforces 3 sections; grandfathered SK leaves get the difficulty-lead baseline |
| [verify-memory-index.py](verify-memory-index.py) | Verify every memory-global leaf is referenced from an index and carries a valid top-level `type:` frontmatter key |
| [verify-plan-file.py](verify-plan-file.py) | Structural validator for planner output (per `planner` SKILL.md § Plan format) |
| [verify-readme.py](verify-readme.py) | Verify the README inventory sentinels (scripts / flat skills / specializations) match the filesystem; `--fix` reconciles, `--root` for project repos |
| [verify-self-improvement-edit.py](verify-self-improvement-edit.py) | `commit-msg` gate: require `[self-improvement-reviewed]` in commits that touch `skills/self-improvement/` |
| [verify-tests-accompany-code.py](verify-tests-accompany-code.py) | `commit-msg` advisory (warn-only, never blocks): nudge when staged non-test `scripts/**.py` carries no test delta; `[skip-test-guard: <reason>]` trailer suppresses it |
<!-- inventory:scripts:end -->
