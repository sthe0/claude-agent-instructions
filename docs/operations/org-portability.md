# Using Core in another organization

Core (this repo) is the **universal** layer — it is org-neutral by design, and project-specific runbooks live in each project's `<project>/.claude/` tree, never here. This page documents the **org-portable profile**: what a developer in any organization gets out of the box, and the small opt-in surface that attaches the facilities that exist only inside one organization.

## Principle: internal-only is opt-in; public stays

The dividing line is the **internal-identifier test**: what must not appear in Core is a genuine *org-internal identifier* — not any brand word.

- **Org-internal** facilities, and the names that identify them, are **opt-in** and never assumed: an internal monorepo and its VCS command verbs, an internal tracker product with its queue names and ticket-key shape, an internal orchestration or CI platform, internal project codenames, internal team/personal path conventions, internal hostnames and services. Core prose is VCS-neutral and the defaults below select the public equivalents.
- **Publicly-reachable** vendor services and public product names stay available unchanged. Yandex Cloud is a public cloud anyone can sign up for, so the `yandex-cloud-expert` specialization remains in Core — it is useful to anyone, not a coupling.
- Where an internal facility is the default elsewhere, **functionality is preserved through a public equivalent**: tracker work → GitHub Issues; an internal VCS → plain `git` / `gh` (which Claude Code drives natively).

Reachability from outside a corporate network is the *reason* the line falls where it does, not the test itself — an internal service could in principle be publicly reachable and would still be an internal identifier. So the judgement is made **per occurrence**, from the surrounding sentence: the same word can be allowed where it names a public product and denied a paragraph later where it names an internal host of the same vendor. There is no blanket substitution.

This means a fresh clone in another org needs **zero edits** to Core.

## The seams, at a glance

Every org-specific facility attaches to Core through one of five seams. Each is *mechanism in Core, data outside it* — Core ships the resolver, the neutral default and the contract; a higher layer (Personal, machine-local, or a project overlay) supplies the org half.

| Seam | Core ships | A higher layer installs | Where |
|---|---|---|---|
| Workspace / tracker backends | `git` workspace, `github` + `none` trackers, the registry and the `default` auth profile | an org workspace backend, an org tracker backend, org auth profiles | `${CLAUDE_PROJECT_PLUGIN_DIR:-<config root>/project-entry-plugins}/{backends,trackers}/`, `${CLAUDE_AUTH_PROFILE_DIR:-~/.config/claude/auth-profiles.d}/` |
| Difficulty-channel adapters | the `DifficultyChannel` port, the `github` adapter, the registry | an adapter for an org tracker, registering itself under its own channel name | `${CLAUDE_DIFFICULTY_PLUGIN_DIR:-<config root>/difficulty-channel-plugins}/adapters/<name>.py` |
| Difficulty-channel **detect hook** | the probe interface and the org-neutral precedence | a `detect(...)` hook that recognizes org host signals | `${CLAUDE_DIFFICULTY_PLUGIN_DIR:-<config root>/difficulty-channel-plugins}/detect.py` |
| Skills overlay | the catalog wiring and the two controls | org- or machine-specific skills, plus a manifest naming them | `<config root>/skills-local/`, named in `<config root>/extracted-skills.local` |
| Term-lint ruleset | the matcher, the discovery order and the gates | the actual denylist of org-internal terms | `<config root>/term-rulesets/*.toml` (or `<project>/.claude/term-rulesets/`) |

The shape repeats: **built-in name first, machine-local plugin second**. `scripts/project_entry/registry.sh` (`_registry_resolve`) does it for shell backends and `scripts/lib/plugin_dir.py` (`resolve_plugin_dir` + `load_plugin_module`) for the Python seams, so a new plugin *name* attaches with zero edits to Core. The sections below cover each seam in turn.

## Onboarding (three commands)

```bash
~/claude-agent-instructions/scripts/setup-symlinks.sh   # symlinks + settings + hooks
~/claude-agent-instructions/scripts/setup-org.sh        # detect channel, write per-machine identity
~/claude-agent-instructions/scripts/doctor.sh           # expect all [ OK ]
```

`setup-org.sh` is a thin, idempotent wrapper over `configure-identity.sh`: it runs the channel auto-detector, writes the system root's `agent-identity.local` (`$CLAUDE_AGENT_HOME/agent-identity.local`, default `~/.claude-agent/…`; never overwriting an existing one), and prints an onboarding checklist. Git needs no special wiring — Claude uses `git`/`gh` directly; just have your VCS auth configured.

## The opt-in surface

Everything org-specific is steered by per-machine keys in the system root's `agent-identity.local` (`$CLAUDE_AGENT_HOME/agent-identity.local`, machine-local, never committed):

| Key | Default (no overlay) | With an org overlay | What it controls |
|---|---|---|---|
| `difficulty_channel` | `github` | an org channel | Where non-author machines file Core difficulties. **Auto-detected** by `scripts/difficulty_channel/detect.py`, which consults the machine-local plugin's optional `detect.py` hook first and falls back to `github`. Core ships no org probes — *which* host signals identify an org is data that lives in the overlay hook. Override by editing the line. |
| `long_job_orchestrators` | unset (no names) | your org's job runners | Comma/space-separated orchestrator names the `hook-long-job-arm.py` advisory hook watches for. **Core ships no built-in names** (`long_job_detect.DEFAULT_ORCHESTRATORS` is empty), so an unconfigured machine arms on no orchestrator launch verb at all — only on the org-neutral `nohup ` signal. Set e.g. `airflow,dagster,prefect`. |
| `skill_first_tracker_hosts` | unset (generic shape only) | an org tracker host | Comma/space-separated host/path fragments the `hook-skill-first.py` skill-first advisory adds to its host-agnostic tracker-API detection (`tracker.` subdomain, `/v2/issues`, `/rest/api/<n>/issue`). Unset → only the generic shape is detected. Set e.g. `mytracker.example.com` for your org's exact tracker hostname. |
| `cursor_project_roots` | unset (no roots) | your mount/checkout globs | Comma/space-separated glob patterns (leading `~` expanded) naming the project checkouts `cursor/scripts/migrate-cursor-namespace.sh --all-configured-roots` re-links. **Core ships no built-in roots**, so an unconfigured machine discovers none and the script asks for explicit roots. Set e.g. `~/mounts/*/myproject`. |
| `project_backend` | `git` | an org backend | Workspace backend the task-entry subsystem uses to make an isolated working copy: `git` worktree (the only backend Core ships) or a plugin backend. **Auto-detected** by `scripts/project_entry/detect_backend.py`, which consults the plugin's `detect.py` hook first and otherwise yields `git`. Override per machine. |
| `tracker_backend` | `github` / `none` | an org backend | Tracker backend that resolves an issue key → slug: GitHub Issues (when `gh` is present), `none` (name pass-through), or a plugin backend. Auto-detected alongside `project_backend`. |
| `projects_dir` | unset (machine-local root only) | shared records root | Optional **shared** root of the named project registry (see below). A team distributes portable project records here; `claude-task --register` always writes absolute checkout paths to the machine-local `<config root>/projects.d` (`~/.claude-agent/projects.d` on a migrated machine; legacy `~/.claude/projects.d` still read as fallback) regardless. A project overlay's own `setup-local.sh` may set this to its workspace-storage `projects/` dir automatically. |

Authority to commit to Core directly is **not** a config flag — it is determined solely by `git push --dry-run` capability on the instructions repo. A read-only clone is fully functional; self-improvement edits land as local commits and any upstream push is gated behind explicit confirmation.

## Workspace / tracker backends and the machine-local plugin dir

The task-entry subsystem (`claude-task`, `enter-task.sh`) is split across two orthogonal axes — a **workspace** backend (how an isolated working copy is made) and a **tracker** backend (how an issue key resolves to a slug) — plus an independent **auth-profile** axis. Core ships only the org-neutral halves: the `git` workspace backend, the optional `github` tracker, the `none` pass-through, the registry/discovery, and the `default` auth profile.

Specialized adapters are **not** committed to Core. An org adapter — its own workspace backend, its own tracker backend, and its own auth profiles — lives in the project's **workspace storage** and is **installed by that project's `setup-local.sh`**, which symlinks into the machine-local plugin dirs:

- backends / trackers → `${CLAUDE_PROJECT_PLUGIN_DIR:-<config root>/project-entry-plugins}/{backends,trackers}/`
- auth profiles → `${CLAUDE_AUTH_PROFILE_DIR:-~/.config/claude/auth-profiles.d}/`

Core's `registry.sh` resolves a backend **name** by checking its built-in directory first, then this plugin dir — so a plugin name Core has never heard of attaches with **zero edits to Core**. The install is idempotent and fires only on a machine where the overlay's own toolchain is detected; on any other machine the plugin dir stays empty and only the Core defaults are available.

**Plugin-installed vs Core-resident.** A backend is Core-resident only when it is org-neutral and useful to anyone (`git`, `github`, `none`). Anything that only exists inside one organization is plugin-installed from workspace storage — the same internal-identifier line that governs the rest of this page. That includes the plugin's *name*: a backend named after an internal tool would put the internal identifier back into Core the moment Core listed it.

### Named project registry

Which workspace subpath and tracker queue a key resolves to is **data, not hardcode**: a named project registry maps each project key to its `{workspace_backend, workspace_subpath, tracker_backend, tracker_queue}`. Core merges two roots by key — a **machine-local** root (`<config root>/projects.d`, holding absolute checkout paths written by `claude-task --register`, never versioned) and an optional **shared/versioned** root (`projects_dir` above, holding only portable fields, distributable to a team). `claude-task --list-projects` prints the merged table; `--project <key>` selects a record explicitly when invoked from outside any working copy. Absent a record, selection falls through to the auto-detect defaults, so a fresh org clone needs no registry to function.

A registry record also carries an optional `opening_prompt_path` field overriding where the opening-dialogue template is read from (default: Core's own `scripts/project_entry/opening-prompt.md`) — a project with its own tone or extra required sections points this at its own file instead of forking the launcher.

### The optional `tracker_read` verb and resume detection

A tracker backend may define an optional `tracker_read` verb (`registry.sh`'s presence-probe pattern, `declare -F`) returning a normalized ticket record (title, status, author, description, comments). GitHub Issues defines it; `none` and any backend that omits it do not. `opening.py` degrades on a single class only: no verb, or a verb that errors on this particular ticket, drops just the `ticket:` line from the composed brief — it never affects the `mode:` verdict (opening vs resume-candidate), which is computed independently.

The `mode:` verdict itself is mechanized, not perceived: it comes from three observable probes — a plan file whose content matches the task, a tracker comment authored by the agent, or a git branch ahead of its merge-base. The rule is monotone on the negative: zero matching artifacts always verdicts `opening`; the model may only *demote* a `resume-candidate` back to `opening` when the artifacts turn out not to be settled work, never promote the reverse. A resume recorded only in a session checkpoint or an experience leaf — with no plan file, no tracker comment, and no branch — is invisible to all three probes and degrades to `opening`, costing an extra turn but never producing a wrong verdict. See [setup.md § The opening dialogue](setup.md#the-opening-dialogue) for the full two-branch flow.

### The optional `tracker_plan_marker` verb

A tracker backend may also define an optional `tracker_plan_marker <key>` verb (same `declare -F` presence-probe pattern as `tracker_read`), printing every comment posted on `<key>` in chronological (creation) order, newline-joined, with no header decoration and no marker-parsing of its own — parsing stays exclusively in `scripts/verify-ticket-plan-sync.py`. GitHub Issues defines it; `none` and any backend that omits it do not. Exactly one degrade class, same shape as `tracker_read`: exit 0 = rendered ok, INCLUDING zero comments (empty stdout is success, not failure — the absence is reported by `verify-ticket-plan-sync.py`'s own NO-PLAN status instead); any nonzero = unavailable, reason on stderr. Its intended consumer is the plan-sync comparator: `<backend-call> tracker_plan_marker <key> | python3 scripts/verify-ticket-plan-sync.py --plan <toml> --comment-file -`, used by [tracker-management](../../skills/tracker-management/SKILL.md) § Resume across sessions to detect DRIFT/NO-PLAN without a manual comment lookup.

## Difficulty-channel adapters and the detect hook

Filing a Core difficulty from a machine that cannot push to Core needs a venue that machine can already write to. Core ships the port (`scripts/difficulty_channel/port.py`), the `github` adapter, and nothing else — an org tracker attaches through two independent plugin points under the same dir, `${CLAUDE_DIFFICULTY_PLUGIN_DIR:-<config root>/difficulty-channel-plugins}`.

**The adapter** (`adapters/<name>.py`, loaded by `load_adapter`) must `register_channel(<name>, <factory>)` at import time — loading alone registers nothing — and must use **absolute** imports, because it is executed under a synthetic package name whose parents are never imported. Beyond the port, in-tree consumers reach for `QUEUE` / `BACKLOG_QUEUE` (stream identifiers), `add_tag`, `add_comment` and `list_comments`; omitting one breaks only the consumer that calls it. Requesting a name with no plugin raises a `FileNotFoundError` naming both the path searched and the built-in names that need no plugin.

**The detect hook** (`detect.py`) decides `difficulty_channel` before Core's neutral rules run. Core's own precedence is deliberately short — hook, then any GitHub credential, then `github` with a warning — because *which host signals identify an organization* is org data, not mechanism: a corp hostname suffix, an internal toolchain on `PATH`, an internal credential-agent directory. The hook receives the four probes (`hostname`, `has_command`, `path_exists`, `getenv`) as keyword arguments and returns a `DetectResult` to decide or `None` to defer. `detect_channel` itself stays pure — it never loads the hook; impure callers resolve it once and pass it in.

Note the asymmetry with the backend seam: Core keeps a `hostname` probe it never uses. The probe set is the *contract*, so it carries what the hook needs even where the neutral rules cannot — no hostname pattern is org-neutral.

## The skills overlay

A skill that is specific to one machine or one organization does not ship here. It lives in `<config root>/skills-local/` and reaches the Claude Code catalog through the symlink tree `setup-symlinks.sh` builds, so it is invocable exactly like a Core skill.

Its **name** is org data too, so Core carries no list of them: `<config root>/extracted-skills.local` is a machine-local manifest (one name per line, `#` comments) read by `scripts/lib/extracted-skills.sh`. Two controls consume it and must agree on the format — `verify-layout-contract.sh` (each named skill is absent from this repo *and* present in the overlay) and `verify-extracted-skills-resolve.sh` (each named skill resolves in the catalog). A missing manifest is a valid state yielding no names, which is what keeps both controls green on a clone that extracted nothing.

## Downstream overlay: the isolated-root contract

A downstream org overlay (a tree layered on top of Core, typically living in the org's own repo) composes on top of Core and installs into the **same** isolated config root — it must **not** re-hardcode its own root. Core exposes the root as a single source of truth (`scripts/lib/config-root.sh`, which exports `CLAUDE_AGENT_HOME`, default `~/.claude-agent`); the overlay's own setup sources it and reuses the variable rather than writing `~/.claude` or a private path:

```bash
source "$CORE/scripts/lib/config-root.sh"     # exports CLAUDE_AGENT_HOME
# install overlay symlinks under "$CLAUDE_AGENT_HOME/…", never ~/.claude
```

Because every Core setup script and launcher already honors `CLAUDE_AGENT_HOME`, an overlay that reuses it **inherits isolation with zero divergence**: bare `claude` stays personal, `claude-task` / `claude-agent` run Core ⊕ overlay on `~/.claude-agent`, and a single `CLAUDE_AGENT_HOME=/some/root` override relocates both in lockstep. An overlay that hardcodes a root instead re-introduces the clobber it was built to avoid and breaks the one-switch model — so reusing Core's resolver is the contract, not an optimization.

## Migrating a machine set up before the stage-5 self-heal removal

Commit `c3bdbc8` deleted three self-heal blocks from `setup-symlinks.sh` and one
stale-agent entry from `verify-instructions-sync.sh`, on the argument that all four
are no-ops on a live machine. That argument was verified on **one** machine only. If
your machine was set up before this commit, `setup-symlinks.sh` no longer cleans the
following up for you, and `verify-instructions-sync.sh` no longer flags them:

- An unconditional removal of one legacy per-org Cursor rules file is gone.
- The removal of a `$CLAUDE_AGENT_HOME/skills/` symlink whose *target still resolves*
  and points at a legacy external location is gone. The remaining `prune_dangling`
  pass only removes symlinks whose target no longer exists, so a still-resolving
  legacy symlink is now covered by **neither** prune.
- The removal of a legacy per-agent `$CLAUDE_AGENT_HOME/agents/*.md` symlink pointing
  outside this repo is gone.
- `verify-instructions-sync.sh`'s stale-agent check no longer names one formerly-stale
  agent filename, so a leftover symlink for it will not surface as a FAIL.

Run `git show c3bdbc8 -- scripts/setup-symlinks.sh scripts/verify-instructions-sync.sh`
to read the removals as a diff — the deleted lines carry the exact file paths and
symlink-target substrings to look for. Check your machine against them by hand and
remove anything that matches. This is a one-time migration step, not an ongoing
requirement — a machine set up after this commit never creates these artifacts in the
first place.

The SHA is stable: this branch lands by fast-forward, so `c3bdbc8` keeps its identity
on `main`. If a future rebase ever invalidates it, search the log for the commit
subject `strip remaining org-internal DATA from Core scripts` instead.

## Term neutrality: the C1 ruleset mechanism

Core ships a generic, org-agnostic **term-lint** mechanism (`scripts/lib/term_ruleset.py`) that carries **zero org names** — it is a matcher, not a denylist. The denylist itself is data an org supplies locally; Core never bundles one.

**Discovery** (`discover_rulesets`), in order:

1. `$CLAUDE_TERM_RULESET_DIR` — if set, REPLACES the discovery set entirely (does not union with the two directories below; used by tests and single-shot overrides).
2. Personal dir: `<agent-home>/term-rulesets/` (e.g. `~/.claude-agent/term-rulesets/`).
3. Team/project dir: `<project>/.claude/term-rulesets/`.

Every `*.toml` file found across the active location(s) is loaded and unioned. See [scripts/term-ruleset.example.toml](../../scripts/term-ruleset.example.toml) for the schema (`[[deny]]`, `[[exempt]]`, `[[grandfather]]` tables) and inline comments explaining each.

**Self-publication refusal.** A ruleset file tracked *inside* the tree it guards is refused at load time (checked via `git ls-files`, on the realpath so a symlink into the tree is caught too). A public repo cannot ship the very denylist that would leak the terms it is trying to hide — publishing the list of forbidden words publishes the words. This is the structural reason the org ruleset lives in the **Personal** layer (`~/.claude-agent/term-rulesets/`) and not in Core's own `.claude/`: the Team location exists for a *private* repo guarding itself, not for a public one.

**Compose semantics.** Rulesets from different layers combine asymmetrically, on purpose:

- `deny` patterns match content **and** path, case-insensitively; hits **UNION** across all discovered rulesets.
- `exempt` entries suppress a specific *occurrence* — a deny hit whose span is contained in the exempt's match — and are scoped to the ruleset that declares them.
- `grandfather` entries suppress every hit (content and path) under a matching path glob, scoped the same way.

The scoping is the load-bearing half: a suppression only applies to hits from patterns in the **same** ruleset, so **no layer can blind another layer's rule**. A Team ruleset cannot exempt its way out of a Personal deny, and a permissive ruleset dropped into the project dir cannot weaken one already installed. Additions compose; subtractions do not.

`grandfather` is meant to be temporary — a shrinking baseline for occurrences an org is not ready to fix yet, with `--assert-grandfather-empty` as the end-state check. `exempt` is the permanent form, for a name that is allowed by the internal-identifier test (a public vendor product) and would otherwise be caught by a deliberately broad deny pattern.

**Zero rulesets installed → reported no-op, never a silent pass.** Every gate below prints an explicit line saying no ruleset was found, rather than passing quietly — a machine with no ruleset installed is indistinguishable from "clean" only by reading that line.

**Three gate points**, by reversibility:

| Gate | Script | Strength | Why |
|---|---|---|---|
| Pre-write | `scripts/hook-term-neutrality.py` (PreToolUse Edit/Write) | Advisory — never blocks | A mid-edit draft is cheap to fix; blocking here would deny a tool call over a false positive. |
| Commit message | `githooks/commit-msg` | Hard-blocking | A bad commit message can't be fixed post-landing without a forbidden history rewrite. |
| Difficulty-record body | `scripts/file-difficulty.py` | Hard-blocking | The record is about to leave this machine for a PUBLIC channel (the report stream). |

`scripts/verify-terms.py` is the whole-tree check (content + path, over `git ls-files`), registered in `verify-all.py`; `--expect-rulesets N` asserts the discovered ruleset count, which is how Core proves its own tree carries zero rulesets.

## What stays vendor-named (and why it is not a coupling)

- **`yandex-cloud-expert`** — kept on purpose. Yandex Cloud is a public vendor cloud with public documentation and public sign-up, so the specialization is useful to anyone who uses that cloud and inert for anyone who does not. It is carved out as a permanent `exempt` entry, never a `grandfather` one: the distinction matters, because grandfather entries are supposed to shrink to zero and this one never will.

## Honest scope: what "org-neutral" does and does not claim

The claim this page and the gates support is a narrow one, and overclaiming it would be worse than not making it:

- **It means:** no org-**internal** identifier appears in the *current* tracked tree.
- **It does not mean org-name-free.** A publicly-reachable vendor name stays by design (above). The test is internal-vs-public, not brand-vs-no-brand.
- **It does not mean history-scrubbed.** Neutralization is a forward edit; removed terms remain in the repository's pre-landing history, which is not rewritten. Anyone holding a clone can read them.
- **It is not a completeness proof.** A ruleset is a **high-recall prefilter over judgement**, not a decision procedure. `verify-terms.py` exiting 0 means "no *listed* pattern matched" — a term nobody thought to add is a term it cannot catch, and the per-occurrence call (allowed public reference vs internal identifier) is made by a reader, not by a regex.
- **It says nothing about the higher layers.** Core is the only tree guarded here. A Team or Personal layer, a project's `<project>/.claude/` tree, and the machine-local plugin dirs are outside it by construction — which is exactly where the org data is *supposed* to live.

## See also

- [Setup and distribution](setup.md) — the full symlink table and per-machine settings merge.
- [Difficulty-channel onboarding](difficulty-channel-onboarding.md) — channel credentials and the `file-difficulty.py` CLI in depth.
- [Instruction layering](../architecture/instruction-layering.md) — how Core < Team < Personal compose.
