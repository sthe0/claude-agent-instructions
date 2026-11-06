---
name: the-policy-scorecard-subagent-failures-counter-counts-marker-text-in-any-tool-result-over-a-different-population
description: The scorecard's subagent_failures counter matches marker text in any tool result over a spawn denominator of another population, so its ratio exceeded 1 while the ledger rate fell
type: reference
schema: regularity/v1
generality: 1
induced_from: [subagent-failure-rate-w29-w30]
rule_derived: "lifted from subagent-failure-rate-w29-w30; no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# the-policy-scorecard-subagent-failures-counter-counts-marker-text-in-any-tool-result-over-a-different-population

## Regularity

policy-scorecard.py's subagent_failures counter (SUBAGENT_FAIL_RE, introduced in b74fb9f on 2026-06-17) increments once per tool_result message whose text matches MALFORMED:, INCOMPLETE: or ESCALATE:, whatever tool produced it, while its spawns_total denominator counts native Agent/Task tool uses although specialist spawns run through spawn-specialist.py under Bash; the two terms count unrelated populations, so the reported ratio exceeds 1 (1.83 in 2026-W29, 2.65 in W30). In W30, 319 of 342 matches (93.3%) came from Read or Bash results reading the marker-protocol source rather than from a spawn return, and the incidental hits rose with Read/Bash result volume (x1.52) and hit rate (x1.78). Against the spawn-cost ledger (~/.local/log/claude-spawn-costs.jsonl, one event == spawn row per spawn), exit_code != 0 fell from 16.5% (W29) to 3.1% (W30), malformed from 41.5% to 32.7%, and the main-thread marker-return rate from 20.7% to 9.1%, so the reported rise is a counting artefact and the genuine rate fell. A grep-derived spawn denominator over Bash commands was off from the ledger by -12% to +97% across W27-W30, because 51.2% of W30's matched commands only mentioned the spawner.

## Generality

Level 1 — ranges over one reported scorecard ratio whose numerator and denominator are counted by different text-matching rules over different populations. It is deliberately not claimed for other scorecard counters, nor for failure-rate measurements taken from the exact per-spawn ledger.

## Induced from

- [[subagent-failure-rate-w29-w30]] — contributes the observed case behind the statement: the W29 to W30 rise of the counter against the falling ledger rates.

## Refutation

The ledger's event == spawn rows show failures rising from W29 to W30 on exit_code, malformed or return_marker, or the counter's regex is applied only to results returned by a spawn, or a reproducer run on the stated corpora does not print the quoted per-week figures.
