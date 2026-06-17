---
name: handling-escalations
description: How the manager resolves each specialist return marker (PLAN-READY / CLARIFY / REPLAN / PERMISSION-REQUEST / ESCALATE / INCOMPLETE / COMPLETED) and the continuation-prompt templates for re-spawning.
type: reference
---

# Handling specialist escalations

Resolve the marker, then re-spawn the specialist with the resolution embedded in a continuation prompt. Marker definitions: see [spawning-specialists.md](spawning-specialists.md) § Return markers.

**On `PLAN-READY:`** — **stop and present the plan to the user for explicit approval** before any further spawn. Do not infer approval from silence or from a side comment; require a positive answer. On `approve` — proceed to the next plan step. On `change` — update the plan (in-thread or by re-spawning planner) and ask again.

**On `CLARIFY:`** — answer the specialist's question directly (in user-visible text if the user can usefully see the question; otherwise inline). Re-spawn the specialist with the answer embedded in a continuation prompt:

```
The earlier CLARIFY: question — <restate question> — is answered: <answer>.
Continue from where you stopped:
<continuation context>
```

If the question requires the user's input (intent, preference, choice), ask the user first; do not invent an answer.

**On `REPLAN:`** — incorporate the proposed revision (possibly after asking the user), update the plan, re-spawn the same or a different specialist with the revised plan.

**On `PERMISSION-REQUEST:`** —

1. **Check existing grants** with `scripts/permissions-cli.py check "<requested action>"` against the global file (default) **and** against `<cwd>/.claude/agent-memory/permissions.json` via `--file` if you are in a project tree. Exit code 0 = matched, treat as granted, go to step 4.
2. **Otherwise ask the user** with the request. Options:
   - **Once** — granted for this specific action only.
   - **Always (project)** — `scripts/permissions-cli.py grant <pattern> --file <cwd>/.claude/agent-memory/permissions.json --context "..."`.
   - **Always (global)** — `scripts/permissions-cli.py grant <pattern> --context "..."`.
   - **No, do fallback** — deny.
3. **On any `always` grant** — the `grant` subcommand stamps the date and writes the entry; no manual editing of the JSON file.
4. **Re-spawn the specialist** with the resolution embedded in the new prompt:

   ```
   The earlier PERMISSION-REQUEST for <action> was resolved: GRANTED (scope: once / project / global) or DENIED.
   [If granted persistently:] Recorded in <path>.
   [If denied:] Do not perform <action>; use your stated fallback or stop.

   Continue from where you stopped:
   <continuation context>
   ```

**On `ESCALATE:`** — resolve the question (with the user if necessary), then re-spawn the specialist with the answer or hand back to the broader plan.

**On `INCOMPLETE:`** — decide: re-spawn with more context, ask the user, or accept the partial.

**On `COMPLETED:`** — before moving on, **diff the delivered approach against the user's recorded decision/intent**, not just against "tests pass". A specialist's `COMPLETED:` certifies the spec it was given was met — not that the delivery still matches what the *user* approved. If the specialist made a material design choice that softens / narrows / reinterprets a user-approved requirement — even a technically justified one (a constraint made the original literally impossible) — that is a **substantive deviation**, not an implementation detail. Surface the fork the specialist resolved unilaterally via `AskUserQuestion` ("you asked for X; constraint C makes X⊕Y impossible; I delivered X weakened to X′ — keep X′ / pick alternative?") and get re-approval **before** treating the step as done or launching its verification run. Only when the delivery matches the approved intent — move to the next plan step.

> Functional ground: a `COMPLETED:` whose delivery quietly reinterprets the user's intent reads as "done", and the divergence surfaces only later — here, "real typed outputs" silently became a JSON handle, caught only at user review after a verification run had already been launched. The diff-against-intent check converts a late, expensive rediscovery into an early one-click confirmation.

> Workflow-level permissions (this section) are independent of Claude Code's tool-call permissions in `~/.claude/settings.json`. The two are checked separately: a tool call may be allowed by settings but still need workflow permission for the higher-level action (e.g. `Bash` is allowed, but pushing to `main` is a workflow-level action that needs explicit permission).
