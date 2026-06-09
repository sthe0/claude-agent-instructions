---
name: token-economy-plan
description: Prioritized plan to reduce token/cache cost in Claude Code sessions without sacrificing autonomy. Items map to concrete artifacts; self-improvement reads this leaf before proposing further token-economy changes so it does not duplicate or conflict with the existing programme.
type: reference
---

# Token-economy improvement plan

Drafted 2026-05-27 after analysing four recent deepagent sessions (367, 415, 414×2) and cross-referencing with public best-practices on context engineering (Anthropic, LangChain, arXiv SkillReducer / Tokenomics, community Claude Code guides). Two-layer derivation: **local measurement** + **web research**; sources cited inline. **Revised 2026-06-04** after a full sweep of all mount transcripts — see § Update 2026-06-04, which reframes the dominant driver.

This plan is a **living programme**: self-improvement should add new items here when token-economy issues are diagnosed, rather than scattering them across unrelated leaves.

## Why this exists — observed cost (2026-05-27)

| Session | Tokens out | `cache_read` | `cache_create` | Bash | Read | Worst re-read |
|---|---|---|---|---|---|---|
| DEEPAGENT-367 | 646k | **185M** | 2.17M | 245 | 42 | `workflow.py` ×10 |
| DEEPAGENT-415 | 624k | **177M** | 2.81M | 224 | 38 | `test_quality.py` ×10 |
| DEEPAGENT-414 (dada) | 160k | 33M | 1.53M | 51 | 15 | `yang_prepare_cli.py` ×2 |

Two dominant cost drivers identified:

1. **Static prefix mass × turn count.** `cache_read` scales linearly with turns; every byte of static prefix (CLAUDE.md, MEMORY.md, skill catalog) is paid once per turn at the cache-read rate.
2. **Prefix invalidation.** Each byte change in any cache-prefixed file forces `cache_create` for the rest of the prefix. Two long sessions burned 2–3M `cache_create` tokens this way.

## Update 2026-06-04 — accumulated context, not the static prefix, is the dominant driver

Full sweep of **all** mount transcripts (`~/.claude/projects/**/*.jsonl`, 68 sessions, 12 project dirs; aggregator: `/tmp/cc-scratch/quota_analyze.py`, reusable). Estimated at Opus list price — the *structure* is the point, not the absolute.

| Token class | Volume | ~Cost | Share |
|---|---|---|---|
| `cache_read` | 2 654M | $3 980 | 43% |
| `cache_create` (5m+1h) | 204M | $4 360 | 47% |
| `output` (incl. thinking) | 13M | $975 | 10% |
| `input` | 1.8M | $27 | 0.3% |

**Finding that reframes the plan:** ~90% of spend is cache operations on the **conversation context**, not model output, and not the static prefix. Within one 871-turn session `cache_read` grows 0 → **870k tokens/turn** by the end (avg 408k). **71% of all `cache_read` cost ($2 800) comes from session turns beyond #200**; only 13 of 68 sessions exceed 300 turns but they dominate. The static prefix (CLAUDE.md + memory + skill catalog + tool defs) is ~38k tokens ≈ 5–10% of cost — so the 2026-05-27 emphasis on prefix-shrinking addressed a minor lever. **The real levers control context growth: sub-agent offloading, earlier auto-compaction, task-scoped sessions, and a 200k (not 1M) default window.** `opus[1m]` lets context reach 870k vs ~200k → ~4× the per-turn `cache_read` on long sessions.

Tool mix confirmed the behaviour: `Bash` 2961 calls vs `Agent` (sub-agent) 32 — heavy exploration ran in the main thread, inflating its context. Spawns ran on Opus (Sonnet output was ~6k of 3.4M).

## Items

Ranked by expected ROI on observed sessions. **Status** key: `done` (artifact landed), `in-progress` (partial), `planned` (not yet implemented), `deferred` (decision to wait), `superseded` (replaced by a better approach).

| # | Item | Origin | Artifact | Status |
|---|---|---|---|---|
| 1 | Suppress redundant Read of unchanged files | local analysis | Harness built-in (verified 2026-05-27) + [system-knowledge/harness-read-dedup.md](system-knowledge/harness-read-dedup.md) | done — built-in protection exists; no separate hook needed unless gap observed |
| 2 | Volatile sections at bottom of MEMORY.md | strict-prefix caching ([Anthropic API docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)) | Restructure of project MEMORY.md files | done for deepagent — extend per project when adopted |
| 3 | Self-improvement: defer prefix-edits to task end | strict-prefix caching | `skills/self-improvement/policy.md` § Cache-aware editing | done |
| 4 | Skill-catalog allowlist + usage audit | [arXiv SkillReducer](https://arxiv.org/html/2603.29919v1), tiered architecture | `scripts/skill-usage-audit.py` + [skill-catalog-curation.md](skill-catalog-curation.md) | partial — audit script in place; manual purge by user review |
| 5 | Large tool-output offloading | [Anthropic Cookbook](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools), LangChain Deep Agents | `scripts/offload-large.sh` wrapper + [large-tool-output-discipline.md](large-tool-output-discipline.md) | partial — wrapper available; voluntary use |
| 6 | Plan-file split into index + per-stage files | progressive disclosure | [plan-file-split.md](plan-file-split.md) + planner skill referenced | done |
| 7 | **Complexity-driven** sub-agent model: `--complexity low/medium/high` → haiku/sonnet/opus, set by the manager per spawn from the assigned task's difficulty. Precedence `--model` > `--complexity` > per-kind default (`MODEL_BY_KIND`, Sonnet for dev/thinker/tech-writer/cloud; planner inherits) > inherit. | community + [Anthropic costs](https://code.claude.com/docs/en/costs) | `scripts/spawn-specialist.py` `COMPLEXITY_MODEL`/`MODEL_BY_KIND`; rubric in `--help` | **done 2026-06-04** |
| 8 | 1h prompt caching | [Introl guide](https://introl.com/blog/prompt-caching-infrastructure-llm-cost-latency-reduction-guide-2025) | observed in transcripts (cw1h 48M) — harness already writes 1h for stable prefix | **deferred** — verify reuse pays off; 1h write costs 2× vs 5m 1.25×, net-negative without >5m idle reuse |
| 9 | `DISABLE_NON_ESSENTIAL_MODEL_CALLS=1` | [Claude Code docs](https://code.claude.com/docs/en/costs) | — | **dropped** — exact env name unconfirmed in docs; background calls are <$0.04/session (negligible ROI) |
| 10 | Earlier auto-compaction + `/compact` at stage boundaries | [Anthropic costs](https://code.claude.com/docs/en/costs) + [env-vars](https://code.claude.com/docs/en/env-vars) | `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70` in `settings/base.json` + `## Cost discipline` in `CLAUDE.md` | **done 2026-06-04** |
| 11 | `session-start-digest.sh` to replace 4–5 startup Bash | local analysis | `scripts/session-start-digest.sh` | done |
| 12 | Cap context window at 200k regardless of model — never use the 1M window. **Reliable lever is `CLAUDE_CODE_DISABLE_1M_CONTEXT=1`**, not the `model` alias: relying on `model: opus` = 200k is version/plan-dependent and proved wrong here (2026-06-09 `/context` showed `167k/1m` with `model: opus`, session reached 214k). With the 200k window + autocompact at 70% (item 10), context compacts at ~140k and can't grow past 200k. | Update 2026-06-04 (≈4× `cache_read` on 1m long sessions) + 2026-06-09 user request (hard 200k cap) | `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` in `settings/base.json` (all machines via `apply-settings.sh`); `model` alias is a weak secondary signal | **done 2026-06-09** — env switch in base.json; verify next session `/context` shows `/200k` |
| 13 | Sub-agent offloading of verbose / exploratory work (only conclusion returns) | [Anthropic costs — delegate to subagents](https://code.claude.com/docs/en/costs) | `## Cost discipline` in `CLAUDE.md` (behavioral) | **done 2026-06-04** |
| 14 | `BASH_MAX_OUTPUT_LENGTH` — native bash-output offload to file + preview | [env-vars](https://code.claude.com/docs/en/env-vars) | `settings/base.json` (30000) | **done 2026-06-04** — partly supersedes `offload-large.sh` |
| 15 | `/effort` down for chat / dispatch, high/xhigh for implementation (thinking bills as output) | [Anthropic costs](https://code.claude.com/docs/en/costs) | `## Cost discipline` (behavioral); `CLAUDE_CODE_EFFORT_LEVEL` env exists but a global value would kneecap coding — keep per-turn | **done 2026-06-04** (behavioral) |
| 16 | `/usage` per-component attribution as the measurement feedback loop | [Anthropic costs — /usage](https://code.claude.com/docs/en/costs) | `## Cost discipline` + this leaf | **done 2026-06-04** (habit) |
| 17 | One task ≈ one session; `/clear` between unrelated tasks | [Anthropic costs — manage context](https://code.claude.com/docs/en/costs) | `## Cost discipline` | **done 2026-06-04** |
| 18 | Trim idle MCP servers per mount; prefer skills over MCP | [Anthropic costs — MCP overhead](https://code.claude.com/docs/en/costs) | global generic skill>MCP rule (`skill-first-dispatch.md` + CLAUDE.md); env-specific mapping in project memory `skill-dispatch-yandex.md` (community skills > MCP for tracker/nirvana); **logos MCP moved to project-scope** in `~/.claude.json` (loads only under a `logos/` cwd) | **partial 2026-06-04** — logos done; further per-mount trimming of yandex/tracker/wiki/intrasearch planned |
| 19 | gena practices: selective memory recall (manifest → ≤5 facts), task context to disk (pass by path, not inline), time-boxed investigation, strict 10-line log cap | gena skills (`arcadia/ai/artifacts/skills/gena`) | log cap already in `CLAUDE.md`; recall + context-to-disk | **partial** — log cap done; recall/context-to-disk planned |
| 20 | **API-level (deepagent product, not our CC quota):** server-side compaction `compact_20260112`, context-editing `clear_tool_uses_20250919` (+`clear_at_least`), Batch API −50% (stacks with caching) | [context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing), [compaction](https://platform.claude.com/docs/en/build-with-claude/compaction), [batch](https://platform.claude.com/docs/en/build-with-claude/batch-processing) | deepagent product code (separate contour) | **noted** — flag to product; out of our-quota scope |
| 21 | **Context-growth nudge hook** — UserPromptSubmit reads live context size from the transcript (latest assistant `usage` = input+cache_read+cache_creation) and emits a throttled, per-band nudge (`/clear` on task switch; offload verbose work to a sub-agent). Turns items 13/17 from behavioral-only into a hook-enforced signal. Also surfaced and fixed a reliability gap: reminder-hook *wiring* was never reproducible (hooks are a machine-local settings key, not merged from base.json), so `hook-resolution-reminder.py` & co. were documented-as-enforced but wired nowhere — now installed idempotently by `install-reminder-hooks.sh` (called from `setup-symlinks.sh`). | this leaf, 2026-06-04 finding (long-session context dominates cost) | `scripts/hook-context-growth-reminder.py` + `scripts/install-reminder-hooks.sh` + `setup-symlinks.sh` wiring | **done 2026-06-09** |

## How self-improvement should use this leaf

- Before proposing any new token-economy edit, read this leaf and the listed artifacts. Update the table in place; do not start a parallel plan.
- A new diagnosed cost driver becomes a new row (status: `planned`) — even if the user hasn't asked for implementation yet.
- When an item lands, change its `Status` to `done`/`partial` and link the artifact.
- If a row turns out to be wrong or superseded by a better approach, mark `superseded` and link the replacement.

## Sources

- [Anthropic — Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic — Prompt caching API docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Claude Cookbook — memory, compaction, tool clearing](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools)
- [LangChain — context engineering for agents](https://www.langchain.com/blog/context-engineering-for-agents)
- [LangChain Deep Agents — context engineering](https://docs.langchain.com/oss/python/deepagents/context-engineering)
- [Augment Code — AI agent loop token costs](https://www.augmentcode.com/guides/ai-agent-loop-token-cost-context-constraints)
- [FlowHunt — context engineering / token optimization](https://www.flowhunt.io/blog/context-engineering/)
- [Introl — prompt caching infrastructure guide](https://introl.com/blog/prompt-caching-infrastructure-llm-cost-latency-reduction-guide-2025)
- [arXiv — SkillReducer: optimising LLM agent skills](https://arxiv.org/html/2603.29919v1)
- [arXiv — Tokenomics in agentic SDE](https://arxiv.org/pdf/2601.14470)

Added 2026-06-04:

- [Claude Code — Manage costs effectively](https://code.claude.com/docs/en/costs) (delegate-to-subagents, `/usage`, `/clear`, `/effort`, MCP overhead, compact instructions)
- [Claude Code — environment variables](https://code.claude.com/docs/en/env-vars) (`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `BASH_MAX_OUTPUT_LENGTH`, `CLAUDE_CODE_EFFORT_LEVEL`)
- [Claude API — Effort](https://platform.claude.com/docs/en/build-with-claude/effort) / [Adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking)
- [Claude API — Context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing), [Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction), [Batch processing](https://platform.claude.com/docs/en/build-with-claude/batch-processing)
- Local: `/tmp/cc-scratch/quota_analyze.py` (transcript aggregator, reusable)
