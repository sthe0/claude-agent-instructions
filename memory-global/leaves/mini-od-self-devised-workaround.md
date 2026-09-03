---
name: mini-od-self-devised-workaround
description: Extend the mini-OD (Expected/Actual/Mismatch) discipline to any blocker met while building or running a self-devised workaround, not only a failed orchestrated job — and use the Mismatch sentence as the escalation question when own research can't close it.
type: feedback
schema: leaf/v1
created: 2026-09-03
last_verified: 2026-09-03
---

# Mini-OD binds on a self-devised-workaround blocker too

## Difficulty

CLAUDE.md's mini-OD rule (inline Expected/Actual/Mismatch before relaunch or log-diving) is scoped to a failed *orchestrated job* — CI run, orchestrator work item, build graph. A blocker met while building or running a workaround **you invented yourself** (a credential exchange, a wrapper script, a one-off build) gets no equivalent discipline. Without it, the response to such a blocker drifts into one of two failure modes: silent improvisation past the blocker (try approach after approach with no declared Mismatch, no escalation), or silent stalling (no progress, no question) — until the user notices and has to specify the next concrete step themselves. Both defeat the point of mini-OD: neither produces a structured investigation, and neither produces an articulated question.

## Guidance

The same declare-before-retry discipline binds before a **second** alternative approach to *any* blocker met while building or executing a self-devised workaround, not only an orchestrated-job failure. State the Mismatch as one concrete, checkable sentence — what specifically is missing, unclear, or blocking — not a vague "it didn't work" or "still stuck".

Tie the Mismatch directly to escalation. If your own investigation (CLAUDE.md § Escalation's "own research precedes escalation") cannot close that Mismatch, that same sentence **is** the question to ask the user via `AskUserQuestion` — don't keep improvising past it in silence, and don't let the user discover the blocker only by watching you stall and having to direct the next step themselves. Declaring the Mismatch explicitly is what makes the question askable in the first place: a blocker never named as a sentence has no content to escalate, so it gets neither investigated structurally nor asked about — it just sits until someone else notices.

Concrete instance (2026-09-03): a control-run subtask hit a genuine local blocker (a stale forwarded credential-agent socket, then a hung credential exchange requiring a hardware confirmation neither the agent nor its fork could give) while assembling a one-off workaround. Neither blocker got a declared Expected/Actual/Mismatch before the next approach was tried, and neither produced a question to the user — the user instead had to name each next concrete step (route through a specific host, rebuild via the project's build tool, locate the exact command that triggered the auth exchange) one at a time. A declared Mismatch at the first blocker ("the forwarded agent socket resolves to a dead session — is a fresh login expected, or should I read the key from disk directly?") would have surfaced the same question the user ended up answering anyway, several steps earlier and without needing to be asked twice.

## See also

- [[workflow-debug-investigation]] — the mini-OD investigation pattern this extends (baseline → topology → code delta).
- [[capability-before-offload]] — the inverse failure: doing for the user what you could do yourself. This leaf is about the case where the missing piece genuinely belongs to the user (a decision, a credential only they hold) and staying silent instead of asking is the defect.
