---
name: effect-not-executor-classifies-substantive
description: A task is Substantive whenever its effect is external/irreversible, even when the agent only delivers commands or config for the user to run rather than executing them itself — classify by the effect, not by who executes it.
type: reference
schema: principle/v1
converted_from: leaf/v1
grounded_in: [regularity:a-step-that-ran-is-not-thereby-a-step-that-worked, ontology:inline-class]
grounding_judged: 51e65daf6e17ff179f3949ce4f4825c9f7e53d0de015a57afe3a39cbcc2dcf4d
grounding_override: The leaf bundles several clauses resting on different regularities; across two judge rounds the judge alternately names a missing ground and then an irrelevant one among the added grounds, so no single grounded_in satisfies both; the repair is a per-clause split of the leaf, a content change outside this in-place conversion.
created: 2026-09-27
last_verified: 2026-09-27
---

## Difficulty

The production-edit gate watches only the agent's own file writes. Advisory output the agent hands to the user — a sudo command, a system/power-setting change, a LaunchDaemon definition, a one-off script — or a config artifact merely staged in `/tmp` can induce external system state without ever touching a file the gate watches, so it slips the coordination spine entirely if classification keys on who performs the action.

## Principle

To keep externally effective work inside the coordination spine, classify a task as Substantive by its external or irreversible effect, not by who executes it, even when the agent only hands the user commands or config (sudo, system or power settings, a LaunchDaemon, a one-off script) to run.

## Goal class

task weight classification that keys on effect

## Grounded in

- [[a-step-that-ran-is-not-thereby-a-step-that-worked]]
- [[inline-class]]

## Guidance

Classify a task as Substantive by its **effect**, not by **who executes it**. An external or irreversible effect (sudo, system/power settings, a LaunchDaemon, a one-off script) makes the task Substantive even when the agent delivers it as commands or config for the user to run, exactly as if the agent had run it directly.

Extracted 2026-09-27 from `CLAUDE.md` § Classify task weight during instruction grooming (headroom recovery under `claude-md-max-chars`) — the rule sentence stays inline there; this leaf carries the full elaboration.

## Exception: a payload the user approved verbatim

The spine exists so that no external effect happens without the user's agreement. When the user has already approved the effect's exact payload verbatim — the text of a comment, an issue or a message, shown in full in an `AskUserQuestion` and accepted — and nothing is left to decide, that approval is the gate the spine would add. Classify such a task by its remaining signals and pass `classify --external-effect --payload-approved "<which ask approved which texts>"`; the engine then does not count the external effect, and the reasons record the approval.

The exception holds only while all of these hold:

- the approval covers the exact bytes that will be published; any content edit after it voids the exception;
- no choice remains (target, wording, timing) and the effect is no more irreversible than the publication itself;
- the published-text gate still applies: a tech-writer pass before those bytes.

It does not cover sudo, system or power settings, scripts, or any effect whose payload cannot be shown and approved verbatim; those stay Substantive.

Precedent: 2026-11-10, four backlog comments whose texts the user approved one by one were routed through the full order/plan/review spine; the user asked that such publications no longer count as substantive tasks.

## See also

- [acting-without-asking.md](acting-without-asking.md)
