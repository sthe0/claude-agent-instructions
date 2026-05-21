# Reasoning and task solving

## Метаданные

| Поле | Значение |
|------|----------|
| `last_verified` | 2026-05-21 |
| `staleness_triggers` | смена обязательного workflow в CLAUDE.md |
| `revalidate` | сверить с README § «Кооперация агентов» и `~/.claude/CLAUDE.md` |

## Understand before acting

- Restate the goal and **done criteria** in one short paragraph before tools.
- Numbers, deadlines, or limits in a ticket **without a source** → find source (code, doc, user) **before** editing.
- Prefer the **smallest** change that satisfies criteria (minimal retest, one entry point, extend existing code).

## Plan and approval

- Non-trivial work: decompose (self or **planner**), show plan, wait for explicit OK unless user said «do it now».
- Parent coordinator must not substitute for **developer** on production code when policy assigns that role.

## When stuck

- Repeated failure, blocker, or process disagreement → **manager** in the same turn, not another blind retry loop.
- User corrects *how the agent behaved* → **self-improvement** in the same turn before the final reply.

## Memory vs prompts

- Durable domain facts → **memory** (local or global INDEX), not bloated agent prompts.
- Cross-session *how to work* → this tree (`memory-global/`).
- Product pipelines, infra names → `~/.claude/memory/INDEX.md` (local tree).

## Self-check before first production edit

- [ ] Goal and criteria clear
- [ ] Plan shown and confirmed (if required)
- [ ] Right specialist delegated (`~/.claude/agents/`, see INDEX in each memory tree)
- [ ] Not duplicating a full pipeline when only one stage needs retest
