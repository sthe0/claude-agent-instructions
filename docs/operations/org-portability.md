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

`setup-org.sh` is a thin, idempotent wrapper over `configure-identity.sh`: it runs the channel auto-detector, writes `~/.claude/agent-identity.local` (never overwriting an existing one), and prints an onboarding checklist. Git needs no special wiring — Claude uses `git`/`gh` directly; just have your VCS auth configured.

## The opt-in surface

Everything org-specific is steered by per-machine keys in `~/.claude/agent-identity.local` (machine-local, never committed):

| Key | Default (off-corp) | Internal Yandex | What it controls |
|---|---|---|---|
| `difficulty_channel` | `github` | `startrek` | Where non-author machines file Core difficulties. **Auto-detected** by `scripts/difficulty_channel/detect.py` — strong internal signals (corp hostname, `ya`+`arc` toolchain, skotty, `/etc/yandex`) select `startrek`; otherwise `github`. Override by editing the line. |
| `long_job_orchestrators` | built-in Yandex list | — | Comma/space-separated orchestrator names the `hook-long-job-arm.py` advisory hook watches for. Unset → the built-in default (Nirvana, Sandbox, Reactor, vh3, hitman, yt), so an unconfigured machine is unchanged. Set e.g. `airflow,dagster` for your org's job runners. |
| `project_backend` | `git` | `arc` | Workspace backend the task-entry subsystem uses to make an isolated working copy: `git` worktree (Core default) or `arc` parallel mount (Yandex plugin). **Auto-detected** by `scripts/project_entry/detect_backend.py` (`ya`+`arc` present → `arc`, else `git`). Override per machine. |
| `tracker_backend` | `github` / `none` | `startrek` | Tracker backend that resolves an issue key → slug: GitHub Issues (when `gh` is present), `none` (name pass-through), or Startrek (Yandex plugin). Auto-detected alongside `project_backend`. |

Authority to commit to Core directly is **not** a config flag — it is determined solely by `git push --dry-run` capability on the instructions repo. A read-only clone is fully functional; self-improvement edits land as local commits and any upstream push is gated behind explicit confirmation.

## Workspace / tracker backends and the machine-local plugin dir

The task-entry subsystem (`claude-task`, `enter-task.sh`) is split across two orthogonal axes — a **workspace** backend (how an isolated working copy is made) and a **tracker** backend (how an issue key resolves to a slug) — plus an independent **auth-profile** axis. Core ships only the org-neutral halves: the `git` workspace backend, the optional `github` tracker, the `none` pass-through, the registry/discovery, and the `default` auth profile.

Specialized adapters are **not** committed to Core. The Yandex adapter (the `arc` workspace backend, the `startrek` tracker, and the `eliza`/`team`/`personal` auth profiles) lives in the project's **workspace storage** and is **installed at `setup-local.sh`** by symlinking into the machine-local plugin dirs:

- backends / trackers → `${CLAUDE_PROJECT_PLUGIN_DIR:-~/.claude/project-entry-plugins}/{backends,trackers}/`
- auth profiles → `${CLAUDE_AUTH_PROFILE_DIR:-~/.config/claude/auth-profiles.d}/`

Core's `registry.sh` resolves a backend **name** by checking its built-in directory first, then this plugin dir — so a fresh plugin name (`arc`, `startrek`) attaches with **zero edits to Core**. The install is idempotent and fires only on a machine where `ya`+`arc` are detected; on any other machine the plugin dir stays empty and only the Core defaults are available.

**Plugin-installed vs Core-resident.** A backend is Core-resident only when it is org-neutral and useful to anyone (`git`, `github`). Anything reachable only from inside a specific org (`arc`, `startrek`, `eliza`) is plugin-installed from workspace storage — the same reachability line that governs the rest of this page.

## What stays Yandex-flavored (and why it's harmless)

- **`yandex-cloud-expert`** — kept on purpose; `yandex.cloud` is a public service.
- **`hook-arc-mount-search-guard.py`** — guards recursive search across `arc` FUSE mounts. With no arc mounts present it is simply inert.
- **Memory leaves referencing Arcanum / Startrek / Nirvana** under `memory-global/leaves/system-knowledge/` — read-only reference facts an external user never touches; they do not change behavior. Genuinely project-scoped runbooks belong in `<project>/.claude/agent-memory/`.

## See also

- [Setup and distribution](setup.md) — the full symlink table and per-machine settings merge.
- [Difficulty-channel onboarding](difficulty-channel-onboarding.md) — channel credentials and the `file-difficulty.py` CLI in depth.
- [Instruction layering](../architecture/instruction-layering.md) — how Core < Team < Personal compose.
