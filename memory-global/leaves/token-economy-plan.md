---
name: token-economy-plan
description: Prioritized plan to reduce token/cache cost in Claude Code sessions without sacrificing autonomy. Items map to concrete artifacts; self-improvement reads this leaf before proposing further token-economy changes so it does not duplicate or conflict with the existing programme.
type: reference
---

# Token-economy improvement plan

Drafted 2026-05-27 after analysing four recent deepagent sessions (367, 415, 414×2) and cross-referencing with public best-practices on context engineering (Anthropic, LangChain, arXiv SkillReducer / Tokenomics, community Claude Code guides). Two-layer derivation: **local measurement** + **web research**; sources cited inline.

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
| 7 | Default `--model sonnet` for `developer`/`thinker` spawns | community Claude Code guides | `scripts/spawn-specialist.py` mapping | **planned** — not started in this batch |
| 8 | `ENABLE_PROMPT_CACHING_1H=1` in settings env | [Introl guide](https://introl.com/blog/prompt-caching-infrastructure-llm-cost-latency-reduction-guide-2025) | `~/.claude/settings.json` env | **planned** — not started in this batch |
| 9 | `DISABLE_NON_ESSENTIAL_MODEL_CALLS=1` | [Claude Code docs](https://code.claude.com/docs/en/costs) | `~/.claude/settings.json` env | **planned** — not started in this batch |
| 10 | Proactive `/compact` at stage boundaries | Context Rot (FlowHunt / Anthropic) | Coordination cycle prose in `CLAUDE.md` | **planned** — not started in this batch |
| 11 | `session-start-digest.sh` to replace 4–5 startup Bash | local analysis | `scripts/session-start-digest.sh` | done |

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
