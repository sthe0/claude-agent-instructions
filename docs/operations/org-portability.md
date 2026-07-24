# Using Core in another organization

Core (this repo) is the **universal** layer — it is org-neutral by design, and project-specific Yandex runbooks live in each project's `<project>/.claude/` tree, never here. This page documents the **org-portable profile**: what a developer in a non-Yandex organization gets out of the box, and the small opt-in surface that switches the few internal-only facilities on.

## Principle: internal-only is opt-in; public stays

The dividing line is **reachability from outside the Yandex corporate network**:

- **Internal-only** facilities (not reachable off-corp) are **opt-in**, never assumed — Arcadia `arc` VCS, the Startrek difficulty/tracker channel, and internal job orchestrators (Nirvana, Sandbox, Reactor, …). Core prose is VCS-neutral and the defaults below select the public equivalents.
- **Publicly-reachable** services stay available unchanged. `yandex.cloud` is a public cloud, so the `yandex-cloud-expert` specialization remains in Core — it is useful to anyone, not a coupling.
- Where an internal facility is the default elsewhere, **functionality is preserved through a public equivalent**: tracker work → GitHub Issues; Arcadia VCS → plain `git` / `gh` (which Claude Code drives natively).

This means a fresh clone in another org needs **zero edits** to Core.

## Onboarding (three commands)

```bash
~/claude-agent-instructions/scripts/setup-symlinks.sh   # symlinks + settings + hooks
~/claude-agent-instructions/scripts/setup-org.sh        # detect channel, write per-machine identity
~/claude-agent-instructions/scripts/doctor.sh           # expect all [ OK ]
```

`setup-org.sh` is a thin, idempotent wrapper over `configure-identity.sh`: it runs the channel auto-detector, writes the system root's `agent-identity.local` (`$CLAUDE_AGENT_HOME/agent-identity.local`, default `~/.claude-agent/…`; never overwriting an existing one), and prints an onboarding checklist. Git needs no special wiring — Claude uses `git`/`gh` directly; just have your VCS auth configured.

## The opt-in surface

Everything org-specific is steered by per-machine keys in the system root's `agent-identity.local` (`$CLAUDE_AGENT_HOME/agent-identity.local`, machine-local, never committed):

| Key | Default (off-corp) | Internal Yandex | What it controls |
|---|---|---|---|
| `difficulty_channel` | `github` | an org channel | Where non-author machines file Core difficulties. **Auto-detected** by `scripts/difficulty_channel/detect.py`, which consults the machine-local plugin's optional `detect.py` hook first and falls back to `github`. Core ships no org probes — *which* host signals identify an org is data that lives in the overlay hook. Override by editing the line. |
| `long_job_orchestrators` | built-in Yandex list | — | Comma/space-separated orchestrator names the `hook-long-job-arm.py` advisory hook watches for. Unset → the built-in default (Nirvana, Sandbox, Reactor, vh3, hitman, yt), so an unconfigured machine is unchanged. Set e.g. `airflow,dagster` for your org's job runners. |
| `project_backend` | `git` | an org backend | Workspace backend the task-entry subsystem uses to make an isolated working copy: `git` worktree (the only backend Core ships) or a plugin backend. **Auto-detected** by `scripts/project_entry/detect_backend.py`, which consults the plugin's `detect.py` hook first and otherwise yields `git`. Override per machine. |
| `tracker_backend` | `github` / `none` | an org backend | Tracker backend that resolves an issue key → slug: GitHub Issues (when `gh` is present), `none` (name pass-through), or a plugin backend. Auto-detected alongside `project_backend`. |
| `projects_dir` | unset (machine-local root only) | shared records root | Optional **shared** root of the named project registry (see below). A team distributes portable project records here; `claude-task --register` always writes absolute checkout paths to the machine-local `<config root>/projects.d` (`~/.claude-agent/projects.d` on a migrated machine; legacy `~/.claude/projects.d` still read as fallback) regardless. On a `ya`+`arc` machine `setup-local.sh` sets this to the workspace-storage `projects/` dir automatically. |

Authority to commit to Core directly is **not** a config flag — it is determined solely by `git push --dry-run` capability on the instructions repo. A read-only clone is fully functional; self-improvement edits land as local commits and any upstream push is gated behind explicit confirmation.

## Workspace / tracker backends and the machine-local plugin dir

The task-entry subsystem (`claude-task`, `enter-task.sh`) is split across two orthogonal axes — a **workspace** backend (how an isolated working copy is made) and a **tracker** backend (how an issue key resolves to a slug) — plus an independent **auth-profile** axis. Core ships only the org-neutral halves: the `git` workspace backend, the optional `github` tracker, the `none` pass-through, the registry/discovery, and the `default` auth profile.

Specialized adapters are **not** committed to Core. The Yandex adapter (the `arc` workspace backend, the `startrek` tracker, and the `eliza`/`team`/`personal` auth profiles) lives in the project's **workspace storage** and is **installed at `setup-local.sh`** by symlinking into the machine-local plugin dirs:

- backends / trackers → `${CLAUDE_PROJECT_PLUGIN_DIR:-<config root>/project-entry-plugins}/{backends,trackers}/`
- auth profiles → `${CLAUDE_AUTH_PROFILE_DIR:-~/.config/claude/auth-profiles.d}/`

Core's `registry.sh` resolves a backend **name** by checking its built-in directory first, then this plugin dir — so a fresh plugin name (`arc`, `startrek`) attaches with **zero edits to Core**. The install is idempotent and fires only on a machine where `ya`+`arc` are detected; on any other machine the plugin dir stays empty and only the Core defaults are available.

**Plugin-installed vs Core-resident.** A backend is Core-resident only when it is org-neutral and useful to anyone (`git`, `github`). Anything reachable only from inside a specific org (`arc`, `startrek`, `eliza`) is plugin-installed from workspace storage — the same reachability line that governs the rest of this page.

### Named project registry

Which workspace subpath and tracker queue a key resolves to is **data, not hardcode**: a named project registry maps each project key to its `{workspace_backend, workspace_subpath, tracker_backend, tracker_queue}`. Core merges two roots by key — a **machine-local** root (`<config root>/projects.d`, holding absolute checkout paths written by `claude-task --register`, never versioned) and an optional **shared/versioned** root (`projects_dir` above, holding only portable fields, distributable to a team). `claude-task --list-projects` prints the merged table; `--project <key>` selects a record explicitly when invoked from outside any working copy. Absent a record, selection falls through to the auto-detect defaults, so a fresh org clone needs no registry to function.

A registry record also carries an optional `opening_prompt_path` field overriding where the opening-dialogue template is read from (default: Core's own `scripts/project_entry/opening-prompt.md`) — a project with its own tone or extra required sections points this at its own file instead of forking the launcher.

### The optional `tracker_read` verb and resume detection

A tracker backend may define an optional `tracker_read` verb (`registry.sh`'s presence-probe pattern, `declare -F`) returning a normalized ticket record (title, status, author, description, comments). GitHub Issues defines it; `none` and any backend that omits it do not. `opening.py` degrades on a single class only: no verb, or a verb that errors on this particular ticket, drops just the `ticket:` line from the composed brief — it never affects the `mode:` verdict (opening vs resume-candidate), which is computed independently.

The `mode:` verdict itself is mechanized, not perceived: it comes from three observable probes — a plan file whose content matches the task, a tracker comment authored by the agent, or a git branch ahead of its merge-base. The rule is monotone on the negative: zero matching artifacts always verdicts `opening`; the model may only *demote* a `resume-candidate` back to `opening` when the artifacts turn out not to be settled work, never promote the reverse. A resume recorded only in a session checkpoint or an experience leaf — with no plan file, no tracker comment, and no branch — is invisible to all three probes and degrades to `opening`, costing an extra turn but never producing a wrong verdict. See [setup.md § The opening dialogue](setup.md#the-opening-dialogue) for the full two-branch flow.

### The optional `tracker_plan_marker` verb

A tracker backend may also define an optional `tracker_plan_marker <key>` verb (same `declare -F` presence-probe pattern as `tracker_read`), printing every comment posted on `<key>` in chronological (creation) order, newline-joined, with no header decoration and no marker-parsing of its own — parsing stays exclusively in `scripts/verify-ticket-plan-sync.py`. GitHub Issues defines it; `none` and any backend that omits it do not. Exactly one degrade class, same shape as `tracker_read`: exit 0 = rendered ok, INCLUDING zero comments (empty stdout is success, not failure — the absence is reported by `verify-ticket-plan-sync.py`'s own NO-PLAN status instead); any nonzero = unavailable, reason on stderr. Its intended consumer is the plan-sync comparator: `<backend-call> tracker_plan_marker <key> | python3 scripts/verify-ticket-plan-sync.py --plan <toml> --comment-file -`, used by [tracker-management](../../skills/tracker-management/SKILL.md) § Resume across sessions to detect DRIFT/NO-PLAN without a manual comment lookup.

## Downstream overlay: the isolated-root contract

A downstream org overlay (e.g. the Yandex `junk/the0/agents` tree) composes on top of Core and installs into the **same** isolated config root — it must **not** re-hardcode its own root. Core exposes the root as a single source of truth (`scripts/lib/config-root.sh`, which exports `CLAUDE_AGENT_HOME`, default `~/.claude-agent`); the overlay's own setup sources it and reuses the variable rather than writing `~/.claude` or a private path:

```bash
source "$CORE/scripts/lib/config-root.sh"     # exports CLAUDE_AGENT_HOME
# install overlay symlinks under "$CLAUDE_AGENT_HOME/…", never ~/.claude
```

Because every Core setup script and launcher already honors `CLAUDE_AGENT_HOME`, an overlay that reuses it **inherits isolation with zero divergence**: bare `claude` stays personal, `claude-task` / `claude-agent` run Core ⊕ overlay on `~/.claude-agent`, and a single `CLAUDE_AGENT_HOME=/some/root` override relocates both in lockstep. An overlay that hardcodes a root instead re-introduces the clobber it was built to avoid and breaks the one-switch model — so reusing Core's resolver is the contract, not an optimization.

## What stays Yandex-flavored (and why it's harmless)

- **`yandex-cloud-expert`** — kept on purpose; `yandex.cloud` is a public service.
- **`hook-arc-mount-search-guard.py`** — guards recursive search across `arc` FUSE mounts. With no arc mounts present it is simply inert.
- **Memory leaves referencing Arcanum / Startrek / Nirvana** under `memory-global/leaves/system-knowledge/` — read-only reference facts an external user never touches; they do not change behavior. Genuinely project-scoped runbooks belong in `<project>/.claude/agent-memory/`.

## See also

- [Setup and distribution](setup.md) — the full symlink table and per-machine settings merge.
- [Difficulty-channel onboarding](difficulty-channel-onboarding.md) — channel credentials and the `file-difficulty.py` CLI in depth.
- [Instruction layering](../architecture/instruction-layering.md) — how Core < Team < Personal compose.
