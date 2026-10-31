---
name: effect-not-executor-classifies-substantive
description: A task is Substantive whenever its effect is external/irreversible, even when the agent only delivers commands or config for the user to run rather than executing them itself — classify by the effect, not by who executes it.
type: reference
schema: principle/v1
converted_from: leaf/v1
grounded_in: [regularity:a-step-that-ran-is-not-thereby-a-step-that-worked, ontology:inline-class]
grounding_judged: 3cc1fc24ba35554683c3b0dabfce0197e8134643d445a0af1c12c43c33ba75bb
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

## See also

- [acting-without-asking.md](acting-without-asking.md)
