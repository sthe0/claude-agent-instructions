---
name: prompt-cache-1h-ttl-already-active
description: Difficulty it removes — you suspect the 5-minute prompt-cache TTL is what forces the constant cache re-writes, and you go looking for an env var to extend it. Fact — the 1h TTL is already reachable with no env var (client-side gate keyed on querySource against a server-tunable allowlist that already contains the main REPL thread), the choice is fixed per session, and on this machine's 14-day corpus roughly 70% of sessions and half the cache-creation tokens are already 1h — so cache writes come from prefix INVALIDATION, not from TTL expiry.
type: reference
schema: leaf/v1
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
