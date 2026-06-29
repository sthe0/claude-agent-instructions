---
name: handling-escalations
description: The cognition the manager supplies for each specialist return marker (PLAN-READY / CLARIFY / REPLAN / PERMISSION-REQUEST / ESCALATE / INCOMPLETE / COMPLETED). The engine owns the routing + continuation-prompt assembly; this leaf is the judgment it cannot.
type: reference
created: 2026-06-04
last_verified: 2026-06-23
---

# Handling specialist escalations

The **routing** of each marker and the **continuation-prompt assembly** are owned by the engine, not this prose:

- `agentctl dispatch` parses the spawn's return marker (`scripts/agentctl/dispatch.py` `parse_marker`) and routes each one to a Directive action — `cmd_dispatch` in `scripts/agentctl/cli.py`: COMPLETED → `record_result` (with `intent_diff_required`), CLARIFY → `answer_clarify`, REPLAN → `replan`, INCOMPLETE → `decide_incomplete`, PLAN-READY → `await_plan_approval`, PERMISSION-REQUEST → grant-check then `continue_spawn` (already granted) or `ask_user_permission`, anything unroutable / ESCALATE / MALFORMED → parked BLOCKED.
- `agentctl resolve-permission --decision granted|denied --scope once|project|global` clears a parked permission request and returns the `continue_spawn` continuation.
- The continuation-prompt strings are rendered by `scripts/agentctl/continuations.py` (`clarify`, `permission_granted`, `permission_denied`) — not reassembled by hand.

This leaf is the **cognition the engine deliberately does not replace** — the judgment to apply at each marker before/around the engine's transition. Marker definitions: [spawning-specialists.md](spawning-specialists.md) § Return markers.

**`PLAN-READY:`** — a **hard gate**. Stop and present the plan to the user for explicit approval before any further spawn; never infer approval from silence or a side comment — require a positive answer. (The engine holds at the approval gate via `await_plan_approval`; you supply the ask and the decision.)

**`CLARIFY:`** — judgment: if the question needs the user's input (intent, preference, choice), ask the user first — do not invent an answer. Otherwise answer it directly. The engine fills your answer into the `clarify` continuation and re-spawns.

**`REPLAN:`** — incorporate the proposed revision (possibly after asking the user), update the plan, re-spawn the same or a different specialist with the revised plan.

**`PERMISSION-REQUEST:`** — the engine checks existing grants (`permissions-cli.py check`) and, if ungranted, surfaces `ask_user_permission` with options once / project / global / deny. The cognition you supply: **the user's grant decision**, and — for a persistent grant — the `permissions-cli.py grant <pattern> [--file <cwd>/.claude/agent-memory/permissions.json] --context "..."` call (the `grant` subcommand stamps the date and writes the entry; no manual JSON editing). Then `resolve-permission --decision …` resumes the specialist.

**`ESCALATE:`** — resolve the question (with the user if necessary), then re-spawn the specialist with the answer or hand back to the broader plan.

**`INCOMPLETE:`** — decide: re-spawn with more context, ask the user, or accept the partial.

**`COMPLETED:`** — before moving on, **diff the delivered approach against the user's recorded decision/intent**, not just against "tests pass". A specialist's `COMPLETED:` certifies the spec it was given was met — not that the delivery still matches what the *user* approved. If the specialist made a material design choice that softens / narrows / reinterprets a user-approved requirement — even a technically justified one (a constraint made the original literally impossible) — that is a **substantive deviation**, not an implementation detail. Surface the fork the specialist resolved unilaterally via `AskUserQuestion` ("you asked for X; constraint C makes X⊕Y impossible; I delivered X weakened to X′ — keep X′ / pick alternative?") and get re-approval **before** treating the step as done or launching its verification run. Only when the delivery matches the approved intent — move to the next plan step. (The engine flags this with `intent_diff_required` on the COMPLETED Directive; the diff itself is yours.)

> Functional ground: a `COMPLETED:` whose delivery quietly reinterprets the user's intent reads as "done", and the divergence surfaces only later — here, "real typed outputs" silently became a JSON handle, caught only at user review after a verification run had already been launched. The diff-against-intent check converts a late, expensive rediscovery into an early one-click confirmation.

> Workflow-level permissions (this section) are independent of Claude Code's tool-call permissions in `~/.claude/settings.json`. The two are checked separately: a tool call may be allowed by settings but still need workflow permission for the higher-level action (e.g. `Bash` is allowed, but pushing to `main` is a workflow-level action that needs explicit permission).
