---
name: 2026-08-20-transcript-token-accounting-must-be-price-weighted
description: Answering 'what eats the quota' from Claude Code transcripts has two traps that both silently produce a WRONG ranking of remedies: (1) one API response emits several transcript rows sharing one message.id with an IDENTICAL usage object, so naive summation inflates totals ~1.9x — dedup by message.id; (2) raw token counts are not cost: cache read is 0.1x input, cache write 1.25x, output 5x, so a category that is 93.7% of tokens can be 50.7% of cost while output at 0.7% of tokens is 17.9% of cost. Ranking levers on raw tokens overstated the model-tier lever (~25% -> actually ~13%), credited a sleep/poll lever that saves wall-clock and no quota at all, and hid the real one: session cold starts (2189 first-steps = 38.4% of ALL cache writes ~ 16% of cost), because every new session pays a full cache write of its prompt.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user, 2026-08-20, rating 4/5"
refs: [/home/the0/.claude-agent/state/telemetry-baseline-2026-08-20/, https://github.com/sthe0/claude-agent-instructions/issues/73, https://github.com/sthe0/claude-agent-instructions/issues/138]
created: 2026-08-20
last_verified: 2026-08-20
---

# Quota analysis over transcripts: dedup by message.id, then weight by cache price

## Difficulty
A raw-token quota analysis ranks the wrong remedies: transcript usage rows double-count (several content blocks per API response share one message.id and repeat the same usage object), and untyped token sums ignore that cache read costs 0.1x, cache write 1.25x and output 5x the input rate — so the resulting share table has no relation to spend.

## Order & criterion
Analyze telemetry and transcripts over ~2 weeks; find what consumes the most quota; check specifically for over-smart model use and looping; then rank the actions by quota saved.

**Acceptance check:** measurable: a per-category cost table whose totals reconcile with the per-family price weights, plus a persisted baseline that a re-run one week later can be compared against

## Contexts

### 2026-08-20 — initial
- Where it arose: any session answering 'where does the quota go' from the transcript trees under the config root and the harness root (`projects/**/*.jsonl` in each)
- Working plan: 1) aggregate transcripts deduping by message.id; 2) categorize each step by its first tool_use (Bash split by command shape); 3) re-weight every bucket by per-family, per-token-type price; 4) attribute cache_creation to the pause before each step to separate cold starts from mid-session expiry; 5) reconcile findings against the Core and project backlogs before proposing anything; 6) persist scripts+JSON+baseline.txt outside the read-only canon mount for the follow-up measurement.

## Cost
~$9 list-price, 65 API steps, one session, zero spawns (all aggregation ran in-thread as /tmp scripts). The session's own transcript is itself an instance of trap (1): 138 usage rows over 65 distinct `message.id`s — a 2.1x inflation factor had it been summed naively.

## Self-critique of the agent system
The price weighting was added only after the user asked 'did you account for caching?' — the first ranking of levers was published on raw tokens and had to be corrected in three places. Cost weights belong in the aggregator from the first line, not as a later pass.
