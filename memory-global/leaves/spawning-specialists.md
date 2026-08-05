---
name: spawning-specialists
description: Full mechanics of spawning a specialist via claude -p — spawn template inputs, budget tiers, recursion cap, monitoring a running spawn, after-spawn checks, bypassPermissions discipline, return markers.
type: reference
created: 2026-06-04
last_verified: 2026-07-27
---

# Spawning specialists

A **spawned specialist** is a fresh Claude Code process (`claude -p`) with a specialization skill appended to its system prompt. No parent conversation history, but the same CLAUDE.md, memory, skills, and tools. Use this mode when inline (see `CLAUDE.md` § Invoking specialists) is not sufficient: large scope, fresh-context-as-feature, multi-stage work, or you want the spawn-cost log entry.

## When NOT to spawn — tiny-edit-in-large-file

Means is chosen by *what the work needs to hold in context*, not only by work type. A spawn carries a ~150k autocompact ceiling (`spawn-specialist.py` pins the child's window via `CLAUDE_CODE_AUTO_COMPACT_WINDOW` + top-level `autoCompactWindow` in `--settings`, and the client derives the trigger from it — see [[autocompact-threshold-policy]]); a developer that full-Reads a large file (>~1000 lines) re-reads it after each autocompact and **thrashes** — the harness emits *"Autocompact is thrashing: context refilled to the limit within N turns"*, the process dies `MALFORMED`, and **no commits land**. This is mis-assigned means: the task is tiny (a few-line edit, or restoring an existing commit) but the executor cannot hold the file context.

To remove this divergence: when the edit is surgical (≤ a few lines) but the target file is large, or the change is "restore an existing commit", **do it in-thread** — `arc cherry-pick <sha>` recovers a commit without dumping its diff into context; a ranged Read (`offset`/`limit`) + Edit touches only the edit region, never the whole file. If a spawn is genuinely unavoidable (multi-file, needs fresh context), the dossier **must** forbid full-file Reads (ranged only) and route large command outputs through `head` / `scripts/offload-large.sh`. Two consecutive `MALFORMED`-with-thrash on the same step is the overcome-difficulty signal — switch means, do not re-spawn a third time.

## Spawn-readiness for gated writes (state-gate)

`hook-state-gate.py` authorizes production Edit/Write by the **acting session's own** engine node — and a spawned specialist runs under a **fresh, unclassified** session that inherits none of the parent's execution authority. So spawning a `developer` for gated writes while the child has no plan/classification → every write is denied and the spawn burns its whole budget. Before such a spawn, ensure the child can reach `EXECUTING` in its own session: a `.toml` plan (markdown plans are structure-verified but do **not** populate `state.stages`, so `next-stage`/`dispatch` never reach an execution node — only `.toml` does). If you can't guarantee that, apply the reviewed code in-thread after driving *your* session to `EXECUTING` instead. See [experience/2026-06-25-state-gate-needs-acting-session-at-executing-via-toml.md](experience/2026-06-25-state-gate-needs-acting-session-at-executing-via-toml.md).

## Spawn template

Use `scripts/spawn-specialist.py` — it handles process concerns (recursion-cap check, budget-tier resolution, permission digest, return-marker validation, cost log). Run `--help` for the flag list; `--dry-run` previews the assembled prompt and command.

Cognitive inputs the manager supplies (mechanics are in `--help`):

- `--kind` — specialization name. Resolved from `~/.claude-agent/skills/<kind>/SKILL.md` (global catalog) first, then from the **project-local** `<cwd>/.claude/skills/specializations/<kind>/SKILL.md` (global wins on name collision). Global kinds: `planner` / `developer` / `thinker` / `yandex-cloud-expert` / `tech-writer`; a project ships its own domain experts under its `.claude/skills/specializations/` and they spawn with the same `claude -p` isolation.
- `--plan` — markdown plan with the owned step marked `**<<this step>>**`.
- `--done-criterion` + `--criterion-type` (`measurable` | `acceptance-review`).
- `--context-dossier` — 5–10 line digest of conversation context the specialist cannot read on its own (intent nuances, rejected options, in-session decisions, terminology aliases). Omit if nothing's missable.
- `--budget` (cost ceiling) — see table below. `--complexity` (`low`/`medium`/`high` → haiku/sonnet/opus) sets the sub-agent model by **assessed task difficulty**, overriding the per-kind default; rubric in `--help`. Budget and complexity are distinct axes — a cheap-budget task can still need opus.
- `--project-permissions <project>/.claude/agent-memory/permissions.json` if inside a project tree.

**Large text travels as a file, never as inline argv.** A dossier, a replanning task, a long brief or a multi-paragraph constraints block goes into a file, and the flag gets `@<path>`: `--constraints @/tmp/constraints.md`, `--done-criterion @/tmp/criterion.md`. Linux caps a single argv string at 131072 bytes (`MAX_ARG_STRLEN`), and the kernel refuses the whole spawn with `E2BIG` *before the child starts* — so the failure lands on the one spawn whose brief was finally substantial enough to matter. Prose that legitimately begins with `@` is doubled (`@@`); a `@` reference to a file that does not exist exits loudly rather than being recorded as literal prose. The same convention holds across every narrative `agentctl` argument — see [`scripts/agentctl/README.md`](../../scripts/agentctl/README.md) § Passing large text.

**Budget tiers** (resolve to `budget-*-usd` in `config.md`):

| Tier | Use for |
|---|---|
| `small` | Single-file edit, narrow analysis, short plan refinement |
| `medium` | Multi-file change with tests, scoped refactor, standard plan — default when in doubt |
| `large` | Cross-cutting change, multi-stage plan, full feature, expensive research |

A specialist that hits its cap returns control with whatever it has.

## Recursion cap

`spawn-specialist.py` enforces `max-recursion-depth` (config.md): refuses with exit 3 when the next depth would exceed it. Applies to every `claude -p` invocation, including `overcome-difficulty`'s recursive escape — no exemption.

On refuse — **do not retry**. Stop, summarize for the user (original task, current chain state, what the next spawn would do, why the cap hit), ask whether to continue manually, restart, or accept partial.

## Monitoring a running spawn

`spawn-specialist.py` prints `transcript=<path>` to stderr within ~10s — the freshest jsonl under `~/.claude/projects/<sanitized-cwd>/` that didn't exist before the spawn. Tail that file periodically (~5 min cadence for `developer` spawns) to catch divergence: wrong `cwd`, writes/commits outside the assigned mount, off-scope work (e.g. running someone else's smoke test). **Kill early** — one rescoped re-spawn is cheaper than waiting for a runaway to exhaust its cap.

**Kill the whole subtree, not the wrapper pid.** A `claude -p` spawn forks children (the model process, tool subprocesses); a bare `kill <pid>` signals only the wrapper and **orphans those children**, which keep running and burning budget. Reap the group: `python3 scripts/kill-tree.py <pid>` (sends SIGTERM to the process group, waits a grace period, then SIGKILL; equivalent to `kill -- -<pgid>`). A spawn launched via `spawn-specialist.py` is already a session/group leader (`proc_tree.launch_supervised`), so its pid *is* its pgid. **Caveat:** the intentionally-detached `nohup … &` pollers from [long-job-monitoring.md](long-job-monitoring.md) are *designed* to survive the session — never reap them this way; kill only the specialist spawn you mean to stop.

## After the spawn (kill or completion)

Before deciding the next move (accept, re-spawn, manual takeover), check **both** uncommitted state *and* commit history on the assigned branch:

```bash
git status -s   # uncommitted changes only
git log -n 5    # whether the spawn committed on-scope work before drifting
```

(same idea on any VCS — check status before log.) A spawn killed for off-scope behavior may still have committed legitimate on-scope work before drifting — `status` is clean, but `log` shows the commit. Skipping `log` has cost a redundant verification spawn in one observed case.

## Permission mode: `acceptEdits` for `developer`, nothing extra for the rest

The wrapper defaults `kind=developer` to `--permission-mode acceptEdits` — the narrowest mode granting the unattended Read / Grep / Write the child needs on the assigned mount. Every other kind gets no mode flag at all and runs on harness defaults; a read-only reviewer or thinker needs no elevation, and passing one "just in case" is over-broad by construction.

**Do not reach for `bypassPermissions`, and do not pass it by hand.** Two independent reasons:

- **Wider than the need.** It waives *every* permission class, not just file writes. A capability beyond writes — running a test command, hashing a file — belongs in an explicit grant (`DEVELOPER_SETTINGS_ALLOW` in `spawn-specialist.py`, or `settings/base.json` when genuinely side-effect-free), where it is reviewable.
- **It is inert on this fleet, which is worse than merely wide.** `/Library/Application Support/ClaudeCode/managed-settings.json` sets `permissions.disableBypassPermissionsMode: "disable"`, and the managed layer outranks CLI args (Managed > CLI > Local > Project > User). The request is silently dropped and the child falls back to the settings `defaultMode` — so a spawn that *looked* unattended in fact ran under prompts nobody was there to answer. On 2026-08-04 this cost six spawns, ~$4.2 and ~40 min: the flag being inert is exactly what misdirected the diagnosis away from the managed layer. Check that file before theorizing about permission behaviour.

**A gate may not demand an attestation the spawn cannot produce.** The same incident's root cause was structural: `gates.plan_review_blockers` requires a reviewer-computed `plan_sha256`, while no spawn kind held any hashing capability. Both `shasum` and `sha256sum` are now in `classify.READONLY_BASH` and `settings/base.json`, and `scripts/tests/test_review_attestation_capability.py` binds the requirement to the grant so the pair cannot drift apart again. When adding a gate that requires a spawn to *prove* something, grant the means in the same change.

Elevation of any kind is not a substitute for **prompt-level discipline**:

- The `--constraints` / dossier **must** contain an explicit hard-deny list — no `cd` / no Write / no Edit / no VCS commit outside `<assigned-mount>`, no internal package-build / `docker push` / smoke tests of other tickets — plus a self-check at session start (`pwd` ⊆ expected mount; if not, return `CLARIFY:`).
- Without this discipline the child treats sibling mounts (referenced as "analogs") as fair game for "understanding through execution".

## Return markers

Each specialist's first non-empty line carries one of these. The wrapper validates and prefixes the output with `MALFORMED:` if the marker is missing.

- `COMPLETED:` — step done; summary + artifacts.
- `PLAN-READY:` — **planner-only.** Plan ready; manager must obtain explicit user approval before next spawn. Hard gate.
- `INCOMPLETE:` — partial; what's done, what's left, blocker.
- `CLARIFY:` — specialist needs one specific fact (path, number, choice between named options) to continue. Manager answers, re-spawns with answer embedded.
- `REPLAN:` — plan-level difficulty; specialist proposes a revision.
- `PERMISSION-REQUEST:` — explicit permission needed for a specific external / irreversible action.
- `ESCALATE:` — other decision (manager or user) affecting plan / scope.

`CLARIFY:` vs `ESCALATE:` — fact vs decision. Prefer `CLARIFY:` when work resumes immediately on the answer.

Handling each marker after the spawn returns: see [handling-escalations.md](handling-escalations.md).
