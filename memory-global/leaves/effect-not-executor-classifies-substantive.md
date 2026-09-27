---
name: effect-not-executor-classifies-substantive
description: A task is Substantive whenever its effect is external/irreversible, even when the agent only delivers commands or config for the user to run rather than executing them itself — classify by the effect, not by who executes it.
type: reference
schema: leaf/v1
created: 2026-09-27
last_verified: 2026-09-27
---

## Difficulty

The production-edit gate watches only the agent's own file writes. Advisory output the agent hands to the user — a sudo command, a system/power-setting change, a LaunchDaemon definition, a one-off script — or a config artifact merely staged in `/tmp` can induce external system state without ever touching a file the gate watches, so it slips the coordination spine entirely if classification keys on who performs the action.

## Guidance

Classify a task as Substantive by its **effect**, not by **who executes it**. An external or irreversible effect (sudo, system/power settings, a LaunchDaemon, a one-off script) makes the task Substantive even when the agent delivers it as commands or config for the user to run, exactly as if the agent had run it directly.

Extracted 2026-09-27 from `CLAUDE.md` § Classify task weight during instruction grooming (headroom recovery under `claude-md-max-chars`) — the rule sentence stays inline there; this leaf carries the full elaboration.

## See also

- [acting-without-asking.md](acting-without-asking.md)
