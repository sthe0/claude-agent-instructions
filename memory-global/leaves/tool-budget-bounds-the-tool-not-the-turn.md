---
name: tool-budget-bounds-the-tool-not-the-turn
description: A per-turn cap on one tool (cirp's once-per-turn check read, a ten-line log dump) bounds that tool alone and never licenses ending the turn — when the capped tool is spent, continue with the work that does not need it.
type: feedback
created: 2026-11-24
last_verified: 2026-11-24
---

# A per-turn tool budget bounds the tool, not the turn

Several rules — in this repo and in the corporate skills it loads — cap a tool **per model turn**: `cirp`'s `get_pull_request_checks` may be called at most once per turn, and [[log-reading-discipline]] caps a dump at ten lines per tool call. Each cap exists to stop polling and context floods. None of them says anything about how much *work* the turn may contain.

**Rule.** When a per-turn budget is spent, keep working on everything that does not need that tool. Ending the turn on a status report because one capped tool is exhausted converts a narrow tool limit into an idle turn, and the user then has to prompt you to resume.

**Why.** A user correction on 2026-11-24 — "why are we stalled?" — landed on exactly this. The CI badge genuinely could not be re-read that turn, which was true and beside the point: plan edits, check-script work, re-sealing and a review round were all available, and none of them touched the badge. What the user saw was a report that read as a stop.

The failure is a scope error rather than forgetfulness: a constraint attached to one instrument was applied to the whole activity that instrument serves. That is the same shape as reading a read-only envelope on one repository as a ban on every edit, or a denied tool call as a denied task.

**How to apply.** Before ending a turn on "waiting for X", name one concrete piece of the current plan that X does not block. If such a piece exists, do it instead of reporting. If none exists, say plainly *what* you are waiting for and *when* you will look again — and prefer a detached watcher plus a scheduled wake-up over a turn whose only content is the announcement of a wait ([[long-job-monitoring]]).
