---
name: acting-without-asking
description: Carve-outs that let the agent act without a per-action confirmation — side-effect-free classes, plan-scope-declared changes, and the budget for estimating an unknown tool's side-effect class. Substantive plan changes still require approval.
type: reference
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

## Anti-patterns

- **Asking permission for a `Read` or `Grep`.** Pre-authorized — see § 1.
- **Asking permission for an edit in a file the plan declared.** Pre-authorized after approval — see § 2.
- **Burning 4 tool calls to "be sure" about a new MCP's side-effect class.** Budget is 1 lookup; after that — `PERMISSION-REQUEST:` (it's a single click for the user vs minutes of agent thrashing).
- **Quietly expanding scope** ("while I was at it I also fixed X in the adjacent file"). § 5 violation — silent substantive change. Ask first, even if the side change feels obviously beneficial.
- **Treating "refinement" as cover for substantive change.** Adding a file to `Reference files` is substantive (§ 5), not refinement, even though it looks like "just an addition".

## Policy ↔ settings.json alignment

The carve-outs in § 1 are **policy**; the **harness** only honors them when the matching patterns are present in `~/.claude/settings.json` `permissions.allow`. If you see permission prompts for items § 1 calls side-effect-free (`ls`, `head`, `cat`, `find`, `wc`, `grep`, `git status`, `arc log`, etc.), the policy is current but the settings haven't caught up — add the patterns. Maintain global read-only Bash idioms in **global** settings.json (cross-machine, cross-project), and project-specific Edit/Write paths in `<cwd>/.claude/settings.local.json`. Audit habit: `Skill(skill="fewer-permission-prompts")` after a click-heavy session.

## See also

- `~/.claude/CLAUDE.md` § Acting without asking — the short pointer that loads this leaf.
- `~/.claude/CLAUDE.md` § On task resolution § Cost & effort — per-stage `Actual effort:` from the plan file is the breakdown referenced from the experience leaf.
- `~/.claude/skills/specializations/planner/SKILL.md` § Plan format — Required resources (optional) and the per-stage `Actual effort:` field.
- `~/claude-agent-instructions/permissions/README.md` — for **specific named** persistent grants (`arc push origin/main`), not for categorical pre-authorizations like the ones in this leaf.
