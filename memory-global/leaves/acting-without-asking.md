---
name: acting-without-asking
description: Carve-outs that let the agent act without a per-action confirmation — side-effect-free classes, plan-scope-declared changes, and the budget for estimating an unknown tool's side-effect class. Substantive plan changes still require approval.
type: reference
created: 2026-05-26
last_verified: 2026-06-24
---

# Acting without asking

User intent: **minimize per-action confirmation points** without losing safety. This leaf is the detailed policy referenced from CLAUDE.md § Acting without asking.

## The three carve-outs

### 1. Side-effect-free actions are pre-authorized

You may invoke these without `AskUserQuestion` / `PERMISSION-REQUEST:`, regardless of whether a plan has been approved:

| Class | Examples |
|---|---|
| File reads | `Read`, `cat` (small files), `ls`, `stat`, `file` |
| Code/text search | `Grep`, `Glob`, `find` (read-only), `rg`, `codesearch`, `ast-index` |
| Web / wiki / docs reads | `WebFetch`, `WebSearch`, wiki `GetPageDetails`, docs read, intrasearch read MCPs |
| Tracker / system reads | `tracker-cli issues get/comments/links`, ABC reads, juggler/monium **read** (no write) |
| Discovery & introspection | `ToolSearch`, any `--help`, any `--dry-run`, MCP describe / metadata calls |
| Pure compute | `jq` (over data already in context or in a temp file), `awk`/`sed` reading files, scripts that don't touch shared state |
| Naming convention shortcut | MCP / CLI subcommands matching `get_*`, `list_*`, `search_*`, `find_*`, `describe_*`, `show_*`, `status`, `check_*`, `info` |

These are pre-authorized even if no plan exists yet (exploration phase counts).

> **Canonical machine subset:** the verb-level form the `settings/base.json` linter actually enforces is `classify_action(tool, verb, subverb)` in `scripts/agentctl/classify.py` (`READONLY_BASH`/`READONLY_GIT`/`READONLY_ARC`, `MCP_READONLY_PREFIXES`, `_MUTATING_BASH`). The classes above are the broader **human-judgment** policy; the code is the conservative subset (it returns `unknown` for anything it doesn't recognize). When you add a read-only verb to the policy, add it to those sets too — single source, kept consistent.

### 2. Plan-scope-declared actions are pre-authorized after plan approval

Once the user has approved a plan (planner returned `PLAN-READY:` and the user said yes), every action **declared in the plan** is authorized — no per-action re-ask. Specifically:

- **File edits** in any path listed under `## Reference files` `### To modify` or as a stage's `Output:`.
- **Artifact creation** for outputs the plan declares (datasets, configs, generated files, PR drafts).
- **VCS operations** declared in the plan's scope: `arc commit` / `git commit` on the assigned branch; `push` to the declared remote and branch; `pr create` if the plan names a PR; **excludes** force-push to shared branches unless explicitly declared.
- **External calls** named in the plan: API calls, MCP write tools, CLI commands listed in `Required resources` or stage bodies.
- **Side-effect-free actions** (see § 1) anywhere, plan or no plan.

The boundary is the plan's **declared** scope. Action on a file or system the plan didn't mention → **not** covered; either replan (see § 5) or `PERMISSION-REQUEST:` for the one-off.

### 3. Unknown tool — budget the side-effect estimation

When you face a tool whose side-effect class is not obvious from its name / surrounding context (e.g. a new MCP, a CLI you haven't used in this project, a `mcp__*__*` tool just surfaced by `ToolSearch`):

**Budget: 1 lookup.** One of:

- `--help` invocation (cheap, side-effect-free for almost every CLI),
- `ToolSearch select:<name>` to fetch its full schema and description,
- `Read` of its SKILL.md or man page if there's an obvious one.

**Heuristic on the lookup output:**

| Verbs in name / description | Likely class |
|---|---|
| `get`, `list`, `search`, `find`, `read`, `describe`, `show`, `status`, `check`, `info`, `inspect`, `query` | side-effect-free → § 1 |
| `create`, `update`, `delete`, `post`, `send`, `push`, `commit`, `write`, `run`, `upload`, `start`, `cancel`, `set`, `apply`, `grant`, `revoke`, `add-*`, `remove-*` | has side effects → either § 2 (if in plan) or ask |

These verb lists are the human-judgment superset of the code's `classify_action` (see § 1) — the function recognizes a conservative subset and returns `unknown` for the rest, which is exactly the case the budget below handles.

After 1 lookup, if the class is **still unclear** (e.g. an MCP tool whose name is neutral and whose description doesn't disambiguate) → default to `PERMISSION-REQUEST:` with: tool name, what you'd run, what you don't know, the fallback if denied. **Do not** burn 3+ lookups trying to decide — the user's time is cheaper than your second lookup.

## Substantive plan changes still require approval

After plan approval, two kinds of plan changes can happen during execution:

### Refinement / addition — manager applies in-thread, no ask

- Tightening a stage's `Expected result image:` after seeing real output.
- Adding a missed Read step or context-gather step inside a stage.
- Reordering steps within a stage when there is no dependency change.
- Typos, wording, link fixes.
- Filling in a previously-unanswered `Operator question` with an answer that became available.
- **Adding `Actual effort:` post-hoc** to a completed stage (always allowed, never a substantive change).

Update the plan file in place; mention the refinement in passing in your reply if material; continue.

### Substantive change — `AskUserQuestion` with a diff vs the prior plan

- Adding or removing files in `Reference files` or `Stages.Output`.
- Adding or removing a stage.
- Changing contracts (API signature, schema, public DTO) the plan declared.
- New required resource (new MCP, new approval needed, new budget tier).
- New specialist becomes necessary (e.g. a `developer` stage that wasn't planned).
- Changing the done criterion or `## Final verification`.
- Introducing a new external action (push to a new remote, message to a new audience, API call to a new service).
- **Any scope expansion or contraction** beyond what the user approved.

For these: state the diff (was → now → why), present via `AskUserQuestion` (`Apply revised plan / Refine / Stick with original`), wait for confirmation. This is exactly the `REPLAN:` flow when it originates from `overcome-difficulty`; the same gate applies when the manager realizes the substantive change in-thread.

### A correction directive *at the approval gate* is not approval

When the plan is at `PLAN_READY` and the user replies with an instruction to *change* it ("make the sandbox the0-only", "tie X to Y", "also forbid Z") rather than an affirmative "go", that directive is a **refinement request, not approval to execute**. Apply the correction, re-present the corrected plan, and wait for an explicit affirmative approval on the plan *as it now stands*. Do not infer "approved" from the act of correcting — a user still shaping the plan has not yet signed off on running it. (Difficulty this removes: treating "here's how to fix the plan" as "start building it" — caught 2026-06-30 when a the0-only refinement was misread as a green light, costing a stopped spawn and an engine rollback.)

## In-context carve-out — manager implements substantive work in-thread

Referenced from CLAUDE.md § Classify task weight ("Carve-out for in-context substantive plans"). A substantive task whose individual implementation steps each fit the *small change* row (≤ `small-change-max-lines` per step, single file each, no irreversible action) may be executed by the manager in-thread, **after the plan is approved**. Rationale: the plan/approval gate already protects against scope drift; the `developer` spawn primarily protects against context drift, which does not apply when the manager has explored the affected files this session. Default to spawning if any step exceeds those bounds, or if the manager has not read the target files in this session.

### Exception — infrastructure-as-code (spawn anyway)

Even when all steps fit *small change*, spawn `developer` when the task:

- touches Dockerfile / docker-compose / CI / deploy scripts,
- restructures a git repo (init / move files into VCS / symlink migration),
- or changes container / service lifecycle on a host.

The aggregate scope and the irreversibility of running-state mutations outweigh per-step size, and the cost-log entry from a separate process is the point.

### Narrow counter-exception (in-thread allowed)

If the infra task's dominant risk is **preservation of live state the manager already has fully loaded this session** (e.g. content-preserving migration of populated memory), and **all** of:

- there are **no** external effects (local only — no remote / push / shared host),
- the manager keeps backups,
- the manager verifies each step,

then in-thread execution can beat a cold spawn that would only re-derive the preservation trap. This does **not** license skipping spawns to save effort — name the live-state-preservation difficulty explicitly, or spawn.

## Anti-patterns

- **Asking permission for a `Read` or `Grep`.** Pre-authorized — see § 1.
- **Asking permission for an edit in a file the plan declared.** Pre-authorized after approval — see § 2.
- **Burning 4 tool calls to "be sure" about a new MCP's side-effect class.** Budget is 1 lookup; after that — `PERMISSION-REQUEST:` (it's a single click for the user vs minutes of agent thrashing).
- **Quietly expanding scope** ("while I was at it I also fixed X in the adjacent file"). § 5 violation — silent substantive change. Ask first, even if the side change feels obviously beneficial.
- **Treating "refinement" as cover for substantive change.** Adding a file to `Reference files` is substantive (§ 5), not refinement, even though it looks like "just an addition".
- **Asking about a referenced file / script / skill without reading it first.** If the user mentions a file / script / skill you don't see in your working tree — `Read` it (and refresh the VCS view first if it's a tracked path that may have landed since your branch diverged — e.g. `arc fetch trunk` then `arc show arcadia/trunk:<path>`). Only `AskUserQuestion` *after* you have the actual content. Asking "how do we adapt X?" before reading X is the inverse of § 1 (`Read` is pre-authorized).
- **Surfacing the executor choice (manager in-thread vs `developer` spawn) to the user.** Who types the code is an *internal* routing decision (CLAUDE.md § Classify task weight + the in-context carve-out) — decide it yourself and proceed. The user approves the **plan and its scope**, not the implementer. An `AskUserQuestion` offering "in-thread vs developer-spawn" is friction, not a genuine user decision.
- **Re-asking to commit an in-scope edit.** `commit` on the assigned branch is plan-scope-declared (§ 2) — commit and report it. Only **push** (or a commit to a shared / protected branch) is a separate gate. Asking "commit in-thread?" for a file the plan exists to change is the same over-ask as asking permission for the edit itself.
- **Reading the user's diagnostic input as a go-ahead.** When you've asked an `AskUserQuestion` and the user replies with *analysis / hypotheses / new evidence* instead of picking an option, that is **diagnosis, not approval**. Fold their input into a revised proposal and re-ask; do not treat it as authorization to spawn a specialist, commit, or launch an irreversible / expensive action (GPU graph, external publish). Hypotheses answer "why", not "go". A bare factual answer to a `CLARIFY:` is the same — it unblocks analysis, it does not approve the fix.

## Policy ↔ settings.json alignment

The carve-outs in § 1 are **policy**; the **harness** only honors them when the matching patterns are present in `~/.claude/settings.json` `permissions.allow`. If you see permission prompts for items § 1 calls side-effect-free (`ls`, `head`, `cat`, `find`, `wc`, `grep`, `git status`, `arc log`, etc.), the policy is current but the settings haven't caught up — add the patterns. Maintain global read-only Bash idioms in **global** settings.json (cross-machine, cross-project), and project-specific Edit/Write paths in `<cwd>/.claude/settings.local.json`. Audit habit: `Skill(skill="fewer-permission-prompts")` after a click-heavy session.

## See also

- `~/.claude/CLAUDE.md` § Acting without asking — the short pointer that loads this leaf.
- `~/.claude/CLAUDE.md` § On task resolution § Cost & effort — per-stage `Actual effort:` from the plan file is the breakdown referenced from the experience leaf.
- `~/.claude/skills/specializations/planner/SKILL.md` § Plan format — Required resources (optional) and the per-stage `Actual effort:` field.
- `~/claude-agent-instructions/permissions/README.md` — for **specific named** persistent grants (`arc push origin/main`), not for categorical pre-authorizations like the ones in this leaf.
