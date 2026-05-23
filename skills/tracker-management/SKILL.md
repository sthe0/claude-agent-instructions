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

What this skill is **not**:
- Not a replacement for `planner` (who decomposes) or `developer` (who writes code).
- Not tied to a specific tracker product. Yandex Tracker / Jira / Linear / GitHub Issues specifics are project memory.
- Not authoritative for status transitions (project-specific).

## What to publish, and when

| Stage | What goes on the ticket |
|---|---|
| **Ticket loaded** | Internal — no comment yet; just confirm to the user you have read the ticket and links |
| **Plan ready, before execution** | Markdown plan (or link to a plan file). Indicate which stages need user approval; flag any blockers / open questions. |
| **Approval received** (or "do it now") | Optional one-line ack; execution begins |
| **Stage completed** | One-line status + artifact link (PR, dashboard, measurement, file path) |
| **Blocker** | What blocks, what's needed to unblock, who can unblock |
| **Approach change** | Why the plan changed, what's new — link the new plan version |
| **Difficulty escalated to `overcome-difficulty` skill** | One-line note that a difficulty arose; the resolution belongs to a later post |
| **Final result** | Resolution summary + all artifacts (merged PRs, dashboards, measurements, link to ticket-related code) |

Adapt the detail to the project's conventions. If project memory specifies a comment format, follow it. Otherwise: terse, factual, linkable.

## How to publish

In priority order:

1. **MCP tracker tools** if available in the session — e.g. `mcp__tracker__GetIssue`, `mcp__tracker__*` for Yandex Tracker, equivalents for other trackers.
2. **Project-specific CLI / scripts** described in `<project_cwd>/.claude/agent-memory/`.
3. **Direct API call** via `Bash` + `curl` if neither of the above and credentials are available.
4. **Ask the user to post on your behalf** if no tooling — provide the exact text to paste.

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
