---
name: specialization-skill-usage-tracking
description: At task resolution, emit a `name | count | purpose` table for every specialization spawn and `Skill` / `Agent` invocation during the task — feeds the experience leaf's "Cost, effort, and tool usage" section so future similar tasks can analyze where agent effort actually went.
type: reference
---

# Specialization & skill usage tracking

The post-resolution experience leaf (see `~/.claude/CLAUDE.md` § On task resolution → § What to record, section #6) carries a structured tally of every specialization spawn and inline skill / subagent invocation made during the task. The goal is **analyzable per-task data on agent-system effectiveness**: which specializations actually pull weight, which skills get invoked but skipped in similar future tasks, which subagents are reached for and why.

## What counts

| Source | Captured how | Counted as |
|---|---|---|
| `Skill` tool calls (inline skills: `overcome-difficulty`, `self-improvement`, `tracker-management`, project-local skills, etc.) | `name:"Skill"` `tool_use` blocks in the parent session transcript | row keyed by `input.skill` |
| `Agent` / `Task` tool calls (subagents: `yandex-guru`, project-local agents) | `name:"Agent"` (or legacy `"Task"`) `tool_use` blocks in the parent transcript | row keyed by `input.subagent_type` |
| `claude -p` spawns (spawned specializations: `developer`, `planner`, `thinker`, `yandex-cloud-expert`, project-local specializations) | one entry per spawn in `~/.local/log/claude-spawn-costs.jsonl` (written by `scripts/spawn-specialist.py`) | row keyed by `kind`; purpose is the spawn's `return_marker` + exit-code summary (the cost log does not store the prompt itself) |

Include **all** invocations, including the trivial ones (a single `tracker-management` call to post one comment, the `self-improvement` invocation that wrote the leaf itself). The user wants the raw counts, not a curated subset — bias would defeat the analysis.

Refused spawns (recursion-cap hits, unknown-kind, plan-not-found) appear as a separate `spawn-refused` kind. They are signal worth preserving.

## How to generate the table

At the resolution gate, after writing the experience leaf's frontmatter and before populating section #6:

```bash
python3 ~/claude-agent-instructions/scripts/tool-usage-report.py \
    --since <task-start-YYYY-MM-DD>
```

Defaults: scans the current cwd's transcripts at `~/.claude/projects/<sanitized-cwd>/*.jsonl` and the spawn cost log at `~/.local/log/claude-spawn-costs.jsonl`. Window: last 7 days unless `--since` is given (use the task's first-turn date — same value passed to `cost-report.py`).

Useful flags:

- `--cwd <abs-path>` — scan transcripts for a different project than the current cwd.
- `--transcript <path>` — scan exactly one jsonl file (useful when the task spanned a known session id).
- `--no-spawns` — inline `Skill` / `Agent` only; skip the spawn cost log.
- `--csv` — CSV instead of markdown (analysis pipeline-friendly).
- `--max-purposes N` — cap the number of distinct purposes shown per row (default 3; older entries are summarized as `+N more`).

The script extracts purpose strings automatically — first line of `Skill.args` or `Agent.description` (or first line of `Agent.prompt` as fallback), truncated to ~80 characters. No bookkeeping discipline is required during the task; everything is reconstructed from the transcript at the end.

## Output shape

```markdown
| Kind | Name | Count | Purposes |
|---|---|---|---|
| Skill | `overcome-difficulty` | 2 | "Stage 2 verification mismatch on YT path"; "Cannot observe Nirvana TTL via UI" |
| Skill | `self-improvement` | 1 | "User feedback: prefer AskUserQuestion at every confirmation gate" |
| Agent | `yandex-guru` | 3 | "What is dsv3?"; "Nirvana TTL layers"; "Logos runtime vs VH3" |
| spawn | `developer` | 4 | "COMPLETED"; "REPLAN"; "INCOMPLETE"; +1 more |
| spawn | `planner` | 1 | "PLAN-READY" |

_Total: 6 inline invocations, 5 spawns, 0 refused._
```

Paste this directly into the experience leaf under section #6 ("Cost, effort, and tool usage").

## Edge cases and notes

- **Multiple sessions per task.** If the task spans more than one Claude Code session id, the script merges them automatically — all jsonls in the cwd's project dir are scanned, time-filtered by `--since`. No special handling needed.
- **Subagent transcripts.** `Agent` tool calls write a child transcript under `~/.claude/projects/<...>/subagents/`. The script does **not** descend into subagent transcripts — they are the agent's internal work, not parent decisions. The single `tool_use` row in the parent transcript is the right unit of counting (one row per agent invocation).
- **Sidechains.** Sidechain messages (`isSidechain:true`) are included; they still represent an agent decision to invoke a tool from the parent's perspective.
- **Purpose deduplication.** The markdown renderer drops exact-duplicate purposes within a row (e.g. five identical `developer` spawns marked `COMPLETED` collapse to one "COMPLETED"; +4 more). For raw data use `--csv`.
- **Future field.** If the spawn cost log gains a `plan_step` or `description` field (e.g. `spawn-specialist.py --description "Stage 2"`), the script will pick it up automatically — `collect_spawns` reads any string fields that are present.

## Why machinery and not "you remember"

Recalling every `Skill` / `Agent` / spawn call from a long-running task by reading back through the conversation is unreliable — calls in the middle of long contexts get missed, and there is no integrity check. The transcript and the cost log already record every call deterministically; a thin wrapper around them gives us reliable data with zero discipline cost during the task. This is a "single failure recoverable but observed repeatedly" case: under-reporting in past leaves is the observed failure mode.
