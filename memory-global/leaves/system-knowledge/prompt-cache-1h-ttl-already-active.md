---
name: prompt-cache-1h-ttl-already-active
description: Difficulty it removes — you suspect the 5-minute prompt-cache TTL is what forces the constant cache re-writes, and you go looking for an env var to extend it. Fact — the 1h TTL is already reachable with no env var (client-side gate keyed on querySource against a server-tunable allowlist that already contains the main REPL thread), the choice is fixed per session, and on this machine's 14-day corpus roughly 70% of sessions and half the cache-creation tokens are already 1h — so cache writes come from prefix INVALIDATION, not from TTL expiry.
type: reference
schema: regularity/v1
converted_from: leaf/v1
given: code
given_source: ~/.nvm/versions/node/*/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe
created: 2026-08-24
last_verified: 2026-08-24
---

# The 1h prompt-cache TTL is already active — cache writes are invalidation, not expiry

## Difficulty

Telemetry shows cache-creation tokens are a small share of volume but a large share of list price
(~5.6% of tokens, ~29% of opus list price over 14 days). The obvious hypothesis is that the cache
entry keeps expiring: Anthropic's default ephemeral TTL is 5 minutes, a long turn easily exceeds it,
so every turn pays to write the prefix again. That hypothesis points at an easy fix — set
`ENABLE_PROMPT_CACHING_1H=1` fleet-wide.

The hypothesis is wrong, and acting on it wastes an env-var rollout on something already switched on
for most sessions. Worse, it hides the real cause: the prefix is being **invalidated** (something
near the front of the context changes), and no TTL setting can help with that.

## Regularity

In Claude Code client 2.1.241 (read from the installed bundle on 2026-08-24) the prompt-cache TTL is chosen by a client-side gate keyed on the request's querySource: FORCE_PROMPT_CACHING_5M gives 5m; ENABLE_PROMPT_CACHING_1H (or Bedrock plus ENABLE_PROMPT_CACHING_1H_BEDROCK) gives 1h; a client not logged in with subscription scopes, or an account using overage, gets 5m; otherwise the querySource is matched against the server-tunable allowlist tengu_prompt_cache_1h_config, whose built-in default is repl_main_thread*, sdk, auto_mode, memdir_relevance. The interactive main thread and headless claude -p are therefore on the allowlist with no env var, and the TTL is a per-session constant (only 5 of 1971 cache-creating sessions were mixed). Over 2787 transcripts (14 days, deduplicated by message.id) 49.3% of cache-creation tokens (128657201 of 260752004) and 63.8% of cache-creating steps (21579 of 33809) were 1h, with whole days at 0% and whole days at 100%; which condition flipped those days was not verified. For a session at 1h a 5-minute expiry cannot be the reason a prefix was written again, so those writes follow from the cached prefix changing between turns. The minified function names are renamed between builds, so the code is findable only by the literal string tengu_prompt_cache_1h_config.

## Given

Given from code: `~/.nvm/versions/node/*/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe`. The statement is taken as known from that source, not induced from recorded experience.

## Refutation

A 1h session shows cache-creation tokens re-written on a turn where the cached prefix was byte-identical to the previous turn, or a client build's gate returns 5m for repl_main_thread on a logged-in subscription account without overage or the env override, or the measured 1h share over a fresh 14-day corpus counted by the stated rule differs materially from these figures without a change of client.

## Guidance

### The gate, as it is actually written

Read out of the installed client bundle
(`~/.nvm/versions/node/*/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe`, client 2.1.241)
on 2026-08-24. In that build the gate is the minified function `EEe(e)`, where `e` is the request's
`querySource`; the main query path calls it as `EEe(i.querySource) ? "1h" : void 0` and passes the
result into `wEe({ttl})`, which builds the `cache_control` object. Its logic, in order:

1. `FORCE_PROMPT_CACHING_5M` truthy → **5m** (hard override).
2. `ENABLE_PROMPT_CACHING_1H` truthy — or Bedrock plus `ENABLE_PROMPT_CACHING_1H_BEDROCK` → **1h**.
3. Not logged in with subscription scopes (`ds()`), or the account `isUsingOverage` → **5m**.
4. Otherwise: match `querySource` against the allowlist from the server-tunable config key
   `tengu_prompt_cache_1h_config`, whose **built-in default is**
   `["repl_main_thread*", "sdk", "auto_mode", "memdir_relevance"]`, with `*` meaning prefix match.

Step 4 is the load-bearing one: `repl_main_thread*` is the interactive CLI's own main conversation
loop and `sdk` covers headless `claude -p` spawns, so **the two query sources that produce nearly all
of this fleet's volume are on the default allowlist**. No env var is required, and setting
`ENABLE_PROMPT_CACHING_1H=1` only adds the sessions excluded by step 3.

> Symbol names are minifier output and are renamed between builds (the same repo already records
> `dYr()` → `fXr()` inside a single 2.1.220 series). Re-find this code by the literal string
> `tengu_prompt_cache_1h_config`, never by the function name.

### What the transcripts measure

Counting rule — the corpus is every `*.jsonl` under `~/.claude-agent/projects/*/` and
`~/.claude/projects/*/` whose mtime falls inside the last 14 days; rows are **deduplicated by
`message.id`** (one API response emits several transcript rows carrying an identical `usage` object,
so naive summation inflates counts about twofold); the TTL split comes from the per-row
`usage.cache_creation` object, whose two keys are `ephemeral_1h_input_tokens` and
`ephemeral_5m_input_tokens`. Script: `/tmp/cc-scratch/telemetry/ttl.py` (+ `ttl2.py`–`ttl4.py` for
the breakdowns); not committed — the figures below are the durable part.

Measured 2026-08-24 over 2 787 transcripts / 34 133 deduplicated steps / 4 512M tokens:

| Quantity | Value |
|---|---|
| Cache-creation tokens in window | 260 752 004 |
| … as `ephemeral_1h_input_tokens` | 128 657 201 (**49.3%**) |
| … as `ephemeral_5m_input_tokens` | 132 094 803 (50.7%) |
| Cache-creating steps at 1h | 21 579 of 33 809 (**63.8%**) |
| Sessions entirely 1h / entirely 5m / mixed | 1 379 / 587 / **5** |

Two structural facts, both more durable than the percentages:

- **The TTL is a per-session constant.** Of 1 971 cache-creating sessions only 5 were mixed. This
  matches the code: the allowlist is fetched once and memoised per process, and the account-state
  checks in step 3 are evaluated per process too.
- **The share swings on whole days, not on workload.** Whole days sit at 0% (2026-08-21…23) and whole
  days at 100% (2026-08-19…20); splitting by model or by sidechain shows no discriminator (haiku is
  72% of 1h sessions and 66% of 5m sessions). The bundle names exactly two runtime conditions that
  can flip a whole period — `isUsingOverage` and the server-tunable allowlist — but **which one fired
  on those days is not verified here**; do not present the attribution as established.

### The conclusion this supports

For every 1h session — at least half of all cache-write volume — a 5-minute TTL cannot be the reason
the prefix was re-written, because the entry had an hour to live. Those writes are therefore caused
by **prefix invalidation**: something inside the cached prefix changed between turns (an edited
always-loaded memory file, a re-ordered system block, a mutated tool list, a growing prefix crossing
a breakpoint). Optimisation effort belongs on prefix stability — see
[token-economy-plan.md](../token-economy-plan.md) item 2 on cache-aware memory layout — not on TTL.

### Whether to force 1h for the rest

Enabling `ENABLE_PROMPT_CACHING_1H=1` fleet-wide would recruit only the step-3 sessions, and it
overrides an account-state check the client makes deliberately (it withholds the longer, more
expensive-to-write TTL while on overage). The residual is real but its size is exactly the unverified
part above, so this leaf does **not** recommend the env var.

> **Superseded figure.** An earlier pass in the same investigation reported this split as 88.5% /
> 11.5%. That number does not reproduce under the counting rule above and must not be re-published;
> the row here replaces it.

## See also

- [token-economy-plan.md](../token-economy-plan.md) — the living token-economy programme; the
  cache-write line item this leaf re-aims from TTL to prefix stability.
- [autocompact-threshold-policy.md](../autocompact-threshold-policy.md) — the other
  client-side constant read out of the same bundle; same "re-find by literal string, not by symbol
  name" caveat.
