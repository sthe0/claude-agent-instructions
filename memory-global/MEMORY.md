# Global memory

Index of memories applicable across all projects on this machine. Entries are pointer lines to leaf files in `leaves/`.

Loaded into every session via `@~/.claude-agent/memory-global/MEMORY.md` import in `CLAUDE.md`. Keep this index under ~200 lines. CONFIRMED: the installed client bundle enforces a real 200-line / 25 000-byte cap on this file (function `Yqr(e)`, constants `Doe=200`/`rxe=25000`) on the AutoMem path, truncating silently; verified 2026-07-23 (bundle read + sentinel probe: line 150 returned, line 240 absent). The CLAUDE.md char ceiling, by contrast, is cosmetic — see `claude-md-max-chars` in docs/operations/config-rationale.md.

## How to use

- **Read:** open this file, then the relevant leaf. Do not load every leaf at session start.
- **Write:** when you learn a fact that applies across projects (user role, machine-wide tools, cross-project workflow), add a leaf in `leaves/` with the auto-memory frontmatter (`name`, `description`, `type` — `user` / `feedback` / `project` / `reference`, plus the `created` / `last_verified` date fields — see [memory-temporal-frontmatter.md](leaves/memory-temporal-frontmatter.md)) and add a one-line pointer here.
- **Project-only facts** belong in `<project_cwd>/.claude/agent-memory/` instead.

## Reasoning and coordination practices

- [Coordination and reasoning practices sub-index](leaves/coordination/MEMORY.md) — coordination, reasoning, planning, verification and engineering-practice leaves (the first group is also named inline in CLAUDE.md); one pointer here keeps the root index small.

## Tooling and mechanics

- [Language-reminder hook coverage gap](leaves/language-reminder-hook-coverage-gap.md) — `hook-prompt-reminders.py` language check fires only on `UserPromptSubmit`; a pure tool-only/task-notification continuation turn gets no nudge — verify dialogue language yourself.
- [Subagent resume and transcripts](leaves/subagent-resume-and-transcripts.md) — `SendMessage` resume mechanism (needs `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`), transcript layout under `~/.claude/projects/.../subagents/`, subagent auto-compaction, cleanup.
- [settings.json env precedence](leaves/claude-code-settings-env-precedence.md) — env in settings.json overrides shell env (`env -u` does not help); auth precedence ladder; what to do when an apiKeyHelper isn't enough.
- [Remote sudo access paths](leaves/remote-sudo-access-paths.md) — when Claude needs sudo on a remote host (user-A ssh → work in user-B's space): NOPASSWD narrow scope (default) / Defaults timestamp_type=global (short window) / direct ssh as target; Claude's `!` shell has no TTY so interactive `sudo -v` won't work.
- [SSH ControlMaster group cache](leaves/ssh-controlmaster-group-cache.md) — after `usermod -aG` on a remote host, a "fresh" `ssh` still shows old groups because ControlMaster/ControlPersist reuses the master session; `ssh -O exit host` to refresh.
- [Stale forwarded SSH agent socket hangs git auth](leaves/ssh-stale-forwarded-agent-sock-hang.md) — `git fetch/push` wedges silently (no error) when `SSH_AUTH_SOCK` → `~/.ssh/ssh_auth_sock` → a dead session's forwarded agent; `timeout 5 ssh-add -l` confirms; `env -u SSH_AUTH_SOCK` works via the on-disk key; heals on next login.
- [macOS shell portability gotchas](leaves/macos-shell-portability-gotchas.md) — BSD sed lacks GNU `\+` (use `sed -E`); bash 3.2 errors on empty `"${arr[@]}"` under `set -u` (use `${arr[@]+"${arr[@]}"}`); `cmd | grep -q` under `pipefail` fails via SIGPIPE 141 (capture-then-grep); `${BASH_SOURCE[0]:-$0}` for zsh-sourced self-location.

- [Tracker write token](leaves/tracker-write-token.md) — internal-tracker writes need `~/.tracker-token` (OAuth); `$OAUTH_TOKEN` is read-only (403 `<tracker>:write`); MCP tracker tools are read-only.
- [Auto-compaction threshold policy](leaves/autocompact-threshold-policy.md) — keep the autocompact trigger comfortably ABOVE the ~90–97k post-compaction floor (a trigger at the floor thrashes — a past ticket); verified trigger ≈ round((window−20k)·0.8), frac 0.2 server-tunable; /context "buffer" (33k) is a display reserve, NOT window−trigger; current 210k window → trigger ~152k, min safe window ~210k; `apply-settings.sh` is additive so dropping a key doesn't clear it from live.
- [memory-index-max-bytes derivation](leaves/memory-index-max-bytes-derivation.md) — why 24000 not 25000, why UTF-8 bytes not chars, why no companion line-count key; verifies the 1000-byte margin against the harness constant `rxe=25000` and the largest pointer ever added to the index.
- [Policy effectiveness tracking](leaves/policy-effectiveness-tracking.md) — standing instrument (`scripts/policy-scorecard.py` + per-session ledger + weekly SessionStart nudge) tracking the model/sub-agent policy along efficiency (tokens/$/attention) and effectiveness (resolution proxies + batch manual rating); closes the "policy → measured outcome → adjustment" loop via a Flags-fire → self-improvement → record-movement procedure.
- [Quality-regression investigation](leaves/quality-regression-investigation.md) — runbook for the per-task quality loop: agent-proposed 1-5 rating confirmed in the resolution AskUserQuestion (`agentctl resolve --quality`, ledger `~/.local/log/claude-task-quality.jsonl` stamped with instructions-repo HEAD); rating rubric; in-flight signal inventory; on a scorecard degradation flag — `scripts/quality-regression-investigate.py` commit shortlist → hypothesis → fix ladder (mechanize → restore salience without growth → re-add prose within ceilings).
- [Solved-by marker and usage stats](leaves/solved-by-007-marker-and-usage-stats.md) — resolution gate auto-stamps a `solved_by_007` internal-tracker tag / GitHub label on a resolved task's ticket (fail-open, engine-executed — a state-decidable rule, unlike the skill-executed status transition); the quality row's new `tracker_key` field backs `scripts/agent-stats.py`, a single local usage report over the existing ledgers.
- [Agent usage telemetry](leaves/agent-usage-telemetry.md) — the opt-in (default OFF) cross-installation usage rollup: `usage-digest.py emit` posts an anonymized counts-only aggregate (salted-hash id, disjoint ISO-week period) to a per-channel tracking sink (private GitHub issue / internal-tracker ticket), `pull` sums them rated-row-weighted and channel-segmented; the consent + privacy envelope and the private-sink reachability constraint (fleet + granted collaborators, not arbitrary forks).

- [Fan-out trial under a cap](leaves/fanout-trial-under-cap.md) — before the first run of anything that spawns processes per invocation (xdist, pools, recursive hooks, spawners): check how the hook evaluates inside a child and run under `scripts/cap-run.sh` (RLIMIT_NPROC + timeout); an uncapped trial cost a 2 h machine reset on 2026-10-07.
- [Propose a skill on the second repeated manual workaround](leaves/propose-skill-on-repeated-workaround.md) — a second hand-rolled repeat of the same complex action with no skill for it is itself a self-improvement trigger, independent of user correction and of task resolution; creation-side counterpart to skill-catalog-curation.

Workflow-level permissions (separate from memory): `~/claude-agent-instructions/permissions/` + `scripts/permissions-cli.py`. Not a memory leaf — operational config.

## Layers (ADR-0001 substrate)

- [Team layer sub-index](leaves/team/MEMORY.md) — scope/authority of the Team precedence layer (EDIT, shared via the project repo) in the `Core < Team < Personal` ladder; the precedence + replace-vs-merge contract is in `docs/architecture/instruction-layering.md`, the rebase/`rerere` maintenance recipe in `docs/operations/layer-maintenance.md`.

## Principles (ADR-0001 generality tier)

- [Principles sub-index](leaves/principles/MEMORY.md) — generality-graded, provenance-rooted, refutable principles induced from recurring difficulties; the planner retrieves them at a plan's `refutable principle` element (retrieval-augmented planning). The **generality≥1 profile** of one difficulty-record model whose generality-0 profile is the experience leaf (two physically-separate sub-indexes, one model). Schema: [principle-leaf-schema.md](leaves/principle-leaf-schema.md).

## System knowledge

- [System-knowledge sub-index](leaves/system-knowledge/MEMORY.md) — durable facts about systems, processes, org structure, codebase architecture that aren't self-evident; recording criteria in `~/.claude-agent/CLAUDE.md` § Memory.

## Resolved-task experience

- [Experience sub-index](leaves/experience/MEMORY.md) — chronological log of resolved-task experience leaves (one per non-trivial task — final plan, difficulties, artifacts, lessons, self-critique, cost).
- [Experience leaf schema](leaves/experience-leaf-schema.md) — the `difficulty/v1` leaf schema (Difficulty / Order & criterion / Contexts / Cost) + `record-experience.py` tooling for resolved-task leaves; the **generality-0 profile** of one difficulty-record model whose generality≥1 profile is the principle leaf (`principle/v1`), keyed by an optional `generality` field (implied 0).
- [Published-text writer gate](leaves/published-text-writer-gate.md) — publishing to a ticket/issue is gated on the FACT of a tech-writer pass before those exact bytes, then a fail-open judge checks the rules in publish-rules.toml; routes, residuals, calibration.
