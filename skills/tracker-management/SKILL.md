---
name: tracker-management
description: TRIGGER when the user's task references an external tracker, ticket, or issue — by ticket key (e.g. ABC-123), by the words "ticket"/"issue"/"tracker"/"тикет"/"тред" in any language, by asking to post/update/close a ticket, or by linking to a tracker URL. Layer tracker publication on top of normal coordination — load the ticket context, publish the working plan, post key progress updates, and post the final result. SKIP when the user's task has no tracker reference, or when the user explicitly says not to touch the tracker.
---

# Tracker management

You are invoking this skill because the user's task references an external tracker. The skill is a **layer on top of normal coordination** — the root coordinator still decides routing, plans, delegates to `planner` / `developer` / `thinker`; this skill adds the tracker-side responsibilities (loading context, publishing the plan, posting progress, posting the result).

This skill is **tracker-agnostic**. Specific API conventions, status transitions, queue routing, comment templates, branch / PR naming — those live in **project memory** (`<project_cwd>/.claude/agent-memory/`) or in the project's MCP server. This file describes only what to publish and when.

## Responsibilities

0. **Activate the engine plugin (first, on invocation)** — run `agentctl plugin-activate --plugin tracker --tracker-key <KEY>` (idempotent; re-run safely on session resume). This attaches the `tracker` sub-state-machine to the session so the **engine drives publication timing**: it surfaces a `publish_*` directive at each phase boundary and its resolution gate blocks `resolve` until the mandatory phases are recorded. You no longer have to *remember* when to post — the engine tells you; this skill supplies the **content and transport**.
1. **Load ticket context** — fetch the ticket and its links / comments / parent tasks via whatever tooling the project provides.
2. **Publish the plan** — before non-trivial execution begins, post the plan (or its summary + link) as a comment on the ticket.
3. **Publish key progress updates** — at meaningful boundaries, post a short status.
4. **Publish the final result** — at the end, post the resolution with all artifacts.

## Phase hooks — engine owns *when*, this skill owns *what*

Once the plugin is active (Responsibility 0), the engine emits a `publish_*` directive (under `Directive.data.plugin_directives`) at each boundary. **Act on the directive when it appears**; this table maps directive → content. After a publication actually lands, record it with `agentctl plugin-record --plugin tracker --phase <p>` (the gate counts only recorded phases).

| Engine signal (`publish_*` directive / state) | What to post |
|---|---|
| (no directive) **Ticket loaded** — first turn referencing the ticket | Internal: read ticket + links + recent comments; confirm context to the user. No comment yet. |
| `publish_plan` (on `submit_plan`) | Post the plan as a comment **before** asking for approval, then `plugin-record --phase plan`. The user reviews the plan on the ticket as well as in chat. |
| `publish_progress` (on a passed stage) | One-line status + artifact link (PR, dashboard, file path, measurement). In PR-stage work, route to the PR instead (see the Special case below). |
| `publish_replan` (on `replan`) | What changed in the plan, why, link to the revised plan. |
| `publish_result` (on `resolve`, before the gate passes) | Final result: resolution summary + all artifacts (merged PRs, dashboards, measurements), **plus the structured difficulty record** (Difficulty / Order & criterion / Context / Working plan) — the ticket is its single source of truth. Then `plugin-record --phase result`. |
| `transition_status` (on `resolve`, after the result post, before the gate passes) | Transition the ticket(s) to the terminal/resolved status per project memory (subtickets before parent), then `plugin-record --phase status`. If the ticket is intentionally left open (e.g. a follow-up PR still pending), record that decision explicitly with `plugin-record --phase status --note "<why open>"` — the specific transition/resolution values are project memory. |

**Resume across sessions.** Plugin state is per-session: a new session continuing an in-flight ticket starts with no tracker bag. Re-run Responsibility 0, then reconcile the ticket's last comment against the **actual current state** — branch commits (`arc log trunk..HEAD`), PR status, prod runs, populated tables — and backfill a catch-up status comment for every meaningful boundary crossed since the last post. The across-session gap is exactly where progress posts silently vanish.

**Blocker / overcome-difficulty.** No dedicated directive: on a blocker post what blocks, what is needed, who can unblock; on a difficulty post one short note (no recursion mechanics on the ticket). If any publication was skipped, post it on the next opportunity rather than dropping it — the ticket should reflect the actual sequence of events.

## Special case: an open PR carries the work

Once the work has a published PR — whether you are resolving review comments, rebasing onto trunk, fixing a conflict, or re-running CI — the **PR is where incremental status lives**, not the ticket. For the duration of PR-stage work this **overrides the per-stage progress rows** of the phase-hooks table:

- Do **not** post intermediate / progress comments to the ticket. Status about the PR itself — rebase done, conflict resolved, CI re-run, what changed in this diff-set — belongs in the PR.
- **Prefer actualizing the PR description** over posting a comment for status that describes the *current state* of the change (what the PR now does, how it was verified, how a conflict was resolved) — the description is the living summary a reviewer reads first. A reply comment is for back-and-forth discussion (reply rationale, resolve/drop notes), not for restating current state. Mechanics for editing a published PR's description programmatically are project memory (for Arcanum: the `arcanum-api-readonly-pr-fields` leaf — `PUT /review-requests/{id}/description`, no draft revert).
- Post **exactly one** ticket comment, and only **after the PR is merged** — the final-result post (merged PR link + summary).

The PR already carries the discussion and the current state; mirroring it on the ticket is noise.

What this skill is **not**:
- Not a replacement for `planner` (who decomposes) or `developer` (who writes code).
- Not tied to a specific tracker product. Yandex Tracker / Jira / Linear / GitHub Issues specifics are project memory.
- Not authoritative for status transitions (project-specific).

## Content guidance per action

Adapt the detail to the project's conventions. If project memory specifies a comment format, follow it. Otherwise: terse, factual, linkable. Plan posts include the markdown plan (or a link to a plan file) and flag stages needing approval. Progress posts are one line plus an artifact link. Final-result posts list all artifacts.

### Experience record lives in the ticket

For ticket-driven work the **ticket is the single source of truth for the resolved difficulty** — do not duplicate it into a full experience leaf. At resolution, post the structured record (Difficulty / Order & criterion / Context / Working plan) as the final comment, then write only a **thin pointer leaf** (`ticket:` frontmatter + a one-line reusable hook). `scripts/record-experience.py ticket …` does both: it writes the thin leaf and prints the comment body to post here. Schema: [experience-leaf-schema.md](../../memory-global/leaves/experience-leaf-schema.md). This keeps the ticket and memory from diverging on later edits and saves context that would go to re-typing the plan.

## How to publish

In priority order:

1. **A local / project-wired tracker skill** — a CLI-backed skill (in the session skill list, or symlinked under `<project_cwd>/.claude/skills/`) that can read *and write* and carries its own write-scoped auth (token auto-fetch, kerberos). **Prefer this for any write.** Enumerate with `ls <project_cwd>/.claude/skills/` and scan the session skill list; do not assume your shell `$*_TOKEN` env vars have write scope.
2. **Project-specific CLI / scripts** documented in `<project_cwd>/.claude/agent-memory/` runbooks (a "Tooling" / "Write operations" section in the tracker leaf).
3. **MCP tracker tools** if available — e.g. `mcp__tracker__GetIssue`, `mcp__tracker__*`. Convenient for **reads**, but the server is often **read-only**; use it for reads when no skill is wired, not for writes — for writes fall back to 1–2.
4. **Direct API call** via `Bash` + `curl` only after 1–3 are exhausted. A 401/403 on write is a credential-scope problem — re-check 1–2 for a CLI with proper auth before escalating.
5. **Ask the user to post on your behalf** only as a last resort — provide the exact text to paste.

## Loading ticket context

Before publishing anything, load:

- The ticket itself (title, description, status, assignee).
- Comments (especially recent ones, accepted decisions, links).
- Linked issues / parent tasks if relevant.
- Wiki / docs linked from the ticket.

If the ticket has **numbers, deadlines, TTLs, or abbreviations without an explicit link to a field or config**, surface them to the planner / root: source them or ask the user. Do not let the ticket's vague numerics get interpreted by guess.

## Coordination with other skills and agents

- **With `planner`** — the plan it produces is what you publish. If the user approved a revised plan after `overcome-difficulty`, publish the revised version.
- **With `overcome-difficulty`** — you don't suppress its operation. When it spawns a recursive escape, that's internal; on the ticket, post one short note about the difficulty and the resolution (when it lands), not the recursion mechanics.
- **With `developer`** — once the developer produces artifacts (PR link, branch name, test output), include them in the next progress post.
- **With project memory** — read the project's `agent-memory/` for tracker conventions before publishing the first comment.

## Not in this skill

- Specific status transitions (Open → In Progress → Done) — project memory.
- Required comment template / language — project memory.
- Branch / PR naming conventions — project memory.
- Draft-PR vs ready-PR policy — project memory.
- Tracker authentication — settings.json or MCP responsibility.
- Cross-tracker integration (e.g. mirroring between Jira and Linear) — out of scope.

When the project has conventions about any of these, find them in `<project_cwd>/.claude/agent-memory/` or the project's `CLAUDE.md` **before** publishing. If conventions are missing for something material, ask the user once and propose recording the answer in project memory.
