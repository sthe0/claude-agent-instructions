---
name: skill-catalog-curation
description: Discipline for keeping the user-invocable skill catalog lean. Every catalog line is text in the session system reminder — paid by every turn's cache prefix and re-paid in full on every cache_create event. Use scripts/skill-usage-audit.py to surface skills that are never invoked, then remove or de-symlink them.
type: reference
---

# Skill catalog curation

The "user-invocable skills" section in every session's system reminder is rendered from the skills directory tree. In a fresh deepagent session the block is ~100 lines and several thousand bytes — every byte sits in the cached prefix, paid once per turn at the cache-read rate and re-paid in full on every prefix invalidation (CLAUDE.md / MEMORY.md edit). See [token-economy-plan.md](token-economy-plan.md) item 4 and arXiv [SkillReducer](https://arxiv.org/html/2603.29919v1) for the principle (tiered architecture > monolithic).

## What "in the catalog" means here

A skill appears in the system reminder when:

- It is a directory under `~/.claude/skills/<name>/` with a `SKILL.md` (or it is a single-file skill `~/.claude/skills/<name>.md`).
- Project-local skills under `<project>/.claude/skills/` extend the catalog in that project's sessions.
- Plugins enabled in `~/.claude/settings.json` (`enabledPlugins`) and skills exposed by extra marketplaces likewise contribute lines.

Removing the symlink (or disabling the plugin / marketplace) is what removes the line from future system reminders.

## Audit habit

Run periodically (e.g. monthly, or after a session feels noisy):

```bash
python3 ~/claude-agent-instructions/scripts/skill-usage-audit.py --days 30
```

Output: a markdown table with three columns per skill — `Skill-invocations` (explicit `Skill` tool calls), `other-mentions` (string tokens in any tool input), `recommendation`. Recommendation values:

| Recommendation | Meaning |
|---|---|
| `keep` | Skill was actually invoked at least once. |
| `review (mentioned but not invoked)` | The slug appeared in tool input strings but never as an explicit `Skill` call. Usually a false positive when the slug is a common English word (`arc`, `run`, `init`) — the human decides. |
| `consider removing from catalog` | Neither invoked nor mentioned in the window. Safest removal candidates. |

The audit does **not** delete anything — it only reports. Removal is a manual judgement call (a skill might be valuable but rare; a skill might be expected to ramp up).

## How to act on the report

1. Filter for `consider removing from catalog`.
2. For each, decide: is the skill imported via a marketplace plugin (turn off via `enabledPlugins`), via a symlink under `~/.claude/skills/` (delete the symlink — the source stays where it is), or via a project-local mount?
3. For corporate / org skills that we want to keep available but not in the global catalog: move them under a topic-specific path and load only via the relevant project's `.claude/skills/` symlink.
4. Re-run the audit after a few sessions to confirm the removal didn't break any workflow.

## Why this isn't enforced by a hook

The decision is judgemental: false positives from common-word slugs (`arc`, `run`, `init`) inflate `other-mentions`; rare-but-load-bearing skills look like noise; new skills haven't had a chance to accumulate usage. A linter would either be too strict (deletes useful skills) or too lax (deletes nothing). Discipline + periodic audit > automation here.
