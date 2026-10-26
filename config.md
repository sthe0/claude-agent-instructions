# Coordination constants

Keys and values for the coordination machinery; edit values here, refer to them by key. Full meaning and basis of each key: `docs/operations/config-rationale.md`.

| Key | Value | Meaning |
|---|---|---|
| `max-recursion-depth` | `5` | Hard cap on `AGENT_RECURSION_DEPTH`; a spawn above it is forbidden, escalate to the user. |
| `loop-sensitivity-depth` | `2` | From this depth a spawned level checks for re-framing of an ancestor's task and returns `LOOP_DETECTED:`. |
| `budget-small-usd` | `1.00` | Expected-size telemetry label for small specialist work; not a kill-cap. |
| `budget-medium-usd` | `3.00` | Expected-size telemetry label for typical specialist work (default tier); not a kill-cap. |
| `budget-large-usd` | `8.00` | Expected-size telemetry label for cross-cutting work; fallback for `spawn-runaway-ceiling-usd`. |
| `spawn-runaway-ceiling-usd` | `25.0` | Single `--max-budget-usd` passed to every spawn; kills a runaway spawn only. |
| `small-change-max-lines` | `20` | Changed-line bound of the small-change class (single file, no architectural decision). |
| `substantive-wall-clock-min` | `30` | Expected minutes from which a task is substantive. |
| `rediscovery-threshold-min` | `5` | Rediscovery minutes a future task must save for a resolved task to earn an experience leaf. |
| `claude-md-max-lines` | `400` | Line ceiling on `CLAUDE.md`; extract a section to a leaf above it. |
| `claude-md-max-chars` | `39500` | Character ceiling on `CLAUDE.md` (UTF-16 code units, the harness's own unit). |
| `readme-max-lines` | `140` | Line ceiling on README.md; deeper material lives under docs/. |
| `cursor-mirror-max-lines` | `220` | Line ceiling on `cursor/rules/claude-code-sync.mdc`. |
| `skill-md-max-lines` | `200` | Line ceiling on any SKILL.md; detail goes to policy.md or a leaf. |
| `policy-md-max-lines` | `400` | Line ceiling on any skill policy.md. |
| `core-difficulty-mass-threshold` | `8` | Severity mass at which a Core-difficulty cluster is flagged for a batched Core change. |
| `difficulty-channel` | `github` | Channel for filing Core difficulties from non-author machines; auto-detected per machine. |
| `advisor-mode` | `substantive` | When the agentctl advisory judge activates: `off` or `substantive`; `AGENTCTL_ADVISOR` overrides. |
| `child-contour-linking` | `on` | Kill switch linking a spawned specialist into its own engine contour; `AGENTCTL_CHILD_LINKING` overrides. |
| `principle-promotion-threshold` | `3` | Context-block count at which promote-scan flags an experience cluster for a principle leaf. |
| `skill-description-max-chars` | `850` | Character ceiling on a SKILL.md frontmatter `description:`. |
| `always-loaded-surface-advisory-chars` | `80000` | Advisory-only ceiling on the aggregate always-loaded surface; never fails. |
| `memory-index-max-bytes` | `24000` | UTF-8 bytes cap on `memory-global/MEMORY.md` (harness truncates at 25000); see memory-index-max-bytes-derivation.md. |
| `phase3-due-sessions-floor` | `30` | Minimum scanned sessions before a Phase-3 firing verdict counts as evidence. |
| `phase3-due-data-window-days` | `14` | Minimum days since the Phase-3 baseline stamp before its verdict is trusted. |
| `effort-divergence-multiple` | `5.0` | Actual-to-estimated effort ratio at which a passing result is routed to DIAGNOSING (spend, wall-clock). |
| `effort-stage-minutes-small` | `10` | Expected active minutes of a small-tier stage; also prices in-thread stages. |
| `effort-stage-minutes-medium` | `25` | Expected active minutes of a medium-tier stage. |
| `effort-stage-minutes-large` | `60` | Expected active minutes of a large-tier stage. |
| `effort-replan-absolute` | `5` | Replan count (and pre-approval review rounds) at which the effort trigger fires or the round release opens. |
| `effort-replan-backstop` | `6` | Replans since the user's last decision after which the agent may no longer acknowledge an effort fire itself. |
| `review-round-backstop` | `6` | Review rounds since the user's last decision after which the agent may no longer record a review override. |
| `effort-absolute-interactions` | `43` | User-interaction count recorded (record-only, never gating) as an effort crossing. |
| `review-pairwise-min-edges` | `2` | Reliance edges below which a multi-stage plan gets one whole-plan review instead of pairwise review. |
| `review-parallel-max` | `6` | Cap on concurrent pair-reviewer spawns within one topological-review level. |
| `verify-final-parallel-max` | `2` | Cap on checks `verify-final` runs at once (1 = serial); also bounded by the free-CPU worker budget. |
| `review-cycle-acts` | `20` | Counted review acts in one pre-approval cycle at which PLAN_READY routes into DIAGNOSING. |
| `review-cycle-active-minutes` | `77` | Active minutes in one pre-approval review cycle at which PLAN_READY routes into DIAGNOSING. |
| `review-stage-nonpass-streak` | `3` | Consecutive non-passing stage-scoped reviews of one stage at which PLAN_READY routes into DIAGNOSING. |
| `root-bash-call-max-min` | `2` | Longest foreground Bash call the root makes; longer ones run with `run_in_background`. |
| `root-silent-max-min` | `25` | Longest stretch the root stays silent to the user while specialists are in flight. |
| `fanout-cap-extra-tasks` | `512` | Extra threads/processes a fan-out trial under `scripts/cap-run.sh` may add to the user's task count. |
| `fanout-cap-timeout-s` | `3600` | Wall-clock limit in seconds for one capped fan-out trial. |
| `memory-leaf-max-bytes` | `74000` | Byte size above which `self-diagnose.py` flags a difficulty/v1 leaf `oversized-leaf`. |
| `memory-leaf-max-contexts-multiple` | `27` | Multiple of `principle-promotion-threshold` in `### ` contexts above which the same flag fires. |
| `plan-stage-max-chars` | `70000` | Largest rendered size of one plan node; over it blocks `submit-plan` and topological review. |
| `plan-field-inline-max-chars` | `3000` | Longest prose field a plan prints inline; longer ones are externalized (advisory). |
| `reaper-pressure-free-percent` | `10` | Free-space percent below which `hook-reaper.py --on-pressure` runs a reaper before its 24-hour throttle. |

Per-machine values live uncommitted beside this file (`agent-identity.local`, `term-rulesets/*.toml`); see `docs/operations/config-rationale.md` and `docs/operations/org-portability.md`.
