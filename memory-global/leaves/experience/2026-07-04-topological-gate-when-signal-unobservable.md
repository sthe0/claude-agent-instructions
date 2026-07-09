---
name: 2026-07-04-topological-gate-when-signal-unobservable
description: Difficulty — a PreToolUse gate (hook-ask-text-split.py) meant to catch 'report lost because it shared one message with an AskUserQuestion' measured the wrong thing: it counted same-turn assistant TEXT in the transcript, but the client drops that text from the transcript entirely when it shares a message with the following tool_use — so the gate had NO observable and allowed the exact failure it existed to prevent. The general fix: when the signal that distinguishes the bad case is structurally unobservable to the gate's inputs, stop trying to measure it and forbid the whole risky TOPOLOGY instead (deny ANY ask in a turn that already completed a tool call), accepting the resulting over-strictness (every gate costs one timer-split turn) as the price of determinism. Record the platform behavior that forces the topological choice next to the mechanism, with a re-verify-before-relax probe, or the next maintainer 'simplifies' the gate back into the blind spot.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [2026-06-24-gate-exemption-is-category-error-for-result-images.md, 2026-06-30-mechanize-subdifficulty-extraction-and-audit.md]
plan_file: /home/the0/.claude-agent/plans/ask-delivery-determinism.toml
created: 2026-07-04
last_verified: 2026-07-09
---

# When the failure-carrying signal is structurally invisible to the gate, gate the topology, not the content

## Difficulty
A gate measures a proxy (transcript text length) for a failure whose actual signal (same-message pre-ask text) never reaches the gate's inputs; the gate then passes the failure through silently.

## Order & criterion
Reproduce the miss -> inspect WHAT the gate can observe (PreToolUse payload = tool_name/tool_input/transcript_path; the same-message text is in neither) -> conclude the content is unmeasurable -> replace content-measurement with a topological ban on the risky shape -> price and accept the over-strictness -> record the forcing platform behavior + a re-verify probe beside the mechanism.

**Acceptance check:** hook denies a zero-text mid-turn ask (the case the content rule missed) AND still allows turn-opening asks; the leaf documenting WHY the gate is topological carries a live re-verification probe.

## Contexts

### 2026-07-04 — initial
- Where it arose: claude-agent-instructions: scripts/hook-ask-text-split.py v2 + CLAUDE.md resolution/escalation wording + system-knowledge leaf claude-code-drops-pre-tool-call-text; triggered by a real lost report in session e00ff3b4.
- Working plan: /home/the0/.claude-agent/plans/ask-delivery-determinism.toml


### 2026-07-09 — 2026-07-09 — the symmetric side: promising an ask without arming the timer
- Where it arose: claude-agent-instructions: scripts/hook-ask-defer-timer.py + scripts/tests/test_ask_defer_timer_hook.py (11 -> 20 cases). Trigger: user, twice in one session — 'you promised buttons again and did not send them'.
- Working plan: /home/the0/.claude-agent/plans/hook-ask-defer-timer-block.v4.toml

## Common core & variations
**Common:** Same gate family, same subject (ask delivery determinism), same lesson: a prose rule the model already carries does not hold — CLAUDE.md had the timer-arming rule, added THAT SAME DAY, and the ask was still stranded. Mechanize the decidable part; see [[2026-07-09-gate-must-execute-what-it-attests]] for the sibling defects found in the engine while landing this hook.

**Variations:** Two distinct defects, each alone enough to make the guard a no-op. (1) The promise-verb x ask-noun regexes missed the real phrasings; broadened, with _strip_quoted() removing fenced blocks, inline code spans and blockquote lines so a quoted example is not read as a promise. (2) The Stop hook exited 0 with stdout — which the platform shows only to the HUMAN in transcript mode and NEVER feeds back to the model. Escalated to {"decision":"block","reason":...} on stdout with exit 0 (feeds the model AND blocks turn end), bounded by a one-shot marker file per session_id so a genuinely stuck turn cannot loop. Contrast with hook-ask-text-split.py, which guards the opposite topology (an ask mid-turn); this one guards a promise of a future ask with no timer armed to make that future turn exist.

## Cost
Small-to-moderate: one developer spawn ($1.12, 17 tests) + two thinker review rounds (r1 REVISE, r2 PASS-WITH-NOTES) + in-thread doc edits. The design insight (topological over content gate) was the load-bearing part; the code was mechanical.

## Self-critique of the agent system
The lost report happened because I followed a CLAUDE.md carve-out ('recap + ask in the same reply') that the client had already made unsatisfiable — I trusted prose over the known drop-behavior. The instruction that caused the failure was itself the thing to retire; a rule the runtime makes impossible must be removed the moment the mechanism proves it, not left to train the model into a broken habit.
