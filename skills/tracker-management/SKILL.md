---
name: tracker-management
description: TRIGGER when the user's task references an external tracker, ticket, or issue — by ticket key (e.g. ABC-123), by the words "ticket"/"issue"/"tracker"/"тикет"/"тред" in any language, by asking to post/update/close a ticket, or by linking to a tracker URL. Layer tracker publication on top of normal coordination — load the ticket context, publish the working plan, post key progress updates, and post the final result. SKIP when the user's task has no tracker reference, or when the user explicitly says not to touch the tracker.
---

# Tracker management

You are invoking this skill because the user's task references an external tracker. The skill is a **layer on top of normal coordination** — the root coordinator still decides routing, plans, delegates to `planner` / `developer` / `thinker`; this skill adds the tracker-side responsibilities (loading context, publishing the plan, posting progress, posting the result).

This skill is **tracker-agnostic**. Specific API conventions, status transitions, queue routing, comment templates, branch / PR naming — those live in **project memory** (`<project_cwd>/.claude/agent-memory/`) or in the project's MCP server. This file describes only what to publish and when.

## Responsibilities

1. **Load ticket context** — fetch the ticket and its links / comments / parent tasks via whatever tooling the project provides.
2. **Publish the plan** — before non-trivial execution begins, post the plan (or its summary + link) as a comment on the ticket.
3. **Publish key progress updates** — at meaningful boundaries, post a short status.
4. **Publish the final result** — at the end, post the resolution with all artifacts.

## Phase hooks (when each publication fires)

This skill is layered on top of the root coordination cycle in `CLAUDE.md`. Each tracker action is tied to a specific phase — not done ad-hoc.

| Coordination phase | Tracker action |
|---|---|
| **Ticket loaded** (first turn referencing the ticket) | Internal: read ticket + links + recent comments. Confirm to the user you have the context. No comment yet. |
| **PLAN-READY** received from `planner` (or in-thread plan finished) | Post the plan as a comment, **before** asking the user for approval. The user reviews the plan on the ticket as well as in chat. |
| **User approval** received on the plan | Optional one-line ack ("approved, starting"). Begin execution. |
| **Stage `COMPLETED`** (specialist returns `COMPLETED:`) | One-line status + artifact link (PR, dashboard, file path, measurement). |
| **`REPLAN:` from a specialist** | Post: what changed in the plan, why, link to the revised plan. |
| **`overcome-difficulty` invoked** | One-line note that a difficulty arose; the resolution belongs to a later post (no recursion mechanics on the ticket). |
| **Blocker** (specialist returned `INCOMPLETE:` with a blocker, or escalation to user) | What blocks, what is needed to unblock, who can unblock. |
| **Task resolution** (user confirmed the task is done) | Final result: resolution summary + all artifacts (merged PRs, dashboards, measurements). Then any tracker-side close action that project memory specifies. |

If a phase fires and you skipped the tracker action, post it on the next opportunity rather than dropping it — the ticket should reflect the actual sequence of events.

What this skill is **not**:
- Not a replacement for `planner` (who decomposes) or `developer` (who writes code).
- Not tied to a specific tracker product. Yandex Tracker / Jira / Linear / GitHub Issues specifics are project memory.
- Not authoritative for status transitions (project-specific).

## Content guidance per action

Adapt the detail to the project's conventions. If project memory specifies a comment format, follow it. Otherwise: terse, factual, linkable. Plan posts include the markdown plan (or a link to a plan file) and flag stages needing approval. Progress posts are one line plus an artifact link. Final-result posts list all artifacts.

## How to publish

In priority order:

1. **MCP tracker tools** if available in the session — e.g. `mcp__tracker__GetIssue`, `mcp__tracker__*` for Yandex Tracker, equivalents for other trackers. MCP servers are often **read-only**; if writes are needed, do not stop here — fall through to step 2.
2. **Project-specific CLI / scripts.** Check both locations:
   - `<project_cwd>/.claude/agent-memory/` for documented runbooks (look for a "Tooling" / "Write operations" section in the tracker leaf).
   - `<project_cwd>/.claude/skills/` for symlinked corporate skills (e.g. `tracker/scripts/tracker-cli.sh` in monorepos that mount org skills into the project). Run `ls <project_cwd>/.claude/skills/` to enumerate what is wired up. These CLIs typically carry their own write-scoped auth (token auto-fetch, kerberos) — do not assume your shell `$*_TOKEN` env vars have write scope.
3. **Direct API call** via `Bash` + `curl` only after step 2 is exhausted. If a `curl` write returns 401/403, the obstacle is credential scope — re-check step 2 for a CLI with proper auth before escalating.
4. **Ask the user to post on your behalf** only as a last resort — provide the exact text to paste.

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
