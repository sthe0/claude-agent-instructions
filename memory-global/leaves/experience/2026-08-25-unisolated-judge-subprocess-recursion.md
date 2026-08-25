---
name: 2026-08-25-unisolated-judge-subprocess-recursion
description: Single-turn 'claude -p' judge calls ran with no environment isolation, so each child was a full session that loaded the fleet's own hooks, and those hooks called a judge again: 1583 one-call sessions ate a 5-hour Max quota window in 47 minutes, prompt template nested 126 deep, with no alarm anywhere because the advisor is fail-open. Isolating the child's config root then silently severed its LOGIN, because auth on this fleet is FILE-carried in $CLAUDE_CONFIG_DIR/.credentials.json — the same blindness reached from the other side (a permanently unconsulted judge instead of a quota fire). Two structural lessons: a subprocess of the agent that inherits the ambient config root is a re-entrancy hazard by construction, and replacing a process's config root removes every capability that root carried in FILES, so the fix is discharged only by ENUMERATING what the ambient root supplies with a disposition per item. Both guard sets on the way were denylists that had already lost members (3 of 5 judge-invoking hooks carried the marker; 3 of 6 guard-inversion shapes were caught) and both were repaired by inverting to a mechanically-derived allowlist.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [2b22784, 24af0bb, a622df4, 0bd7970]
created: 2026-08-25
last_verified: 2026-08-25
---

# An unisolated judge subprocess re-enters the agent; isolating it silently removes file-carried capability

## Difficulty
A child process the agent spawns for a small judgement inherits the parent's whole environment, so it is a full agent session that re-triggers the machinery that spawned it — unbounded recursion whose only visible symptom is quota disappearing. The obvious remedy, replacing the child's config root, removes capabilities that root carried in files (auth, model selection from settings.json) with no error the fail-open caller can see.

## Order & criterion
Close the quota hole so parallel sessions stop eating the window; then keep the isolated judge functional without placing a second copy of the secret on disk.

**Acceptance check:** measurable: a live probe driven through the SHIPPED isolated_run_kwargs() compares the isolated call's input context against an ambient baseline measured IN THE SAME RUN (never a frozen constant), carries a lower bound so a zero-usage call cannot sail under the ceiling, asserts the judge's actual ANSWER against a known-verdict fixture, and counts nested sessions from durable transcripts under the sandbox root rather than a sampled process tree. Measured: 15826 vs 37380 same run (ratio 0.42), answer correct, exactly one transcript, live .credentials.json unchanged by hash. A RED --self-test arm plants a second transcript and must exit non-zero.

## Contexts

### 2026-08-25 — initial
- Where it arose: scripts/lib/host_llm.py (isolated_run_kwargs / _lend_auth / _SANDBOX_ROOT), scripts/lib/advisor.py + marker_extract.py subprocess runners, the five judge-invoking scripts/hook-*.py, scripts/verify-judge-isolation.py, scripts/tests/test_judge_child_guard_coverage.py
- Working plan: /home/the0/.claude-agent/plans/judge-call-context-isolation.toml

## Cost
$24.25, 6 spawns, ~50 min active; quality 3 (user) — the result was reached and measured, but two review rounds and the cost overrun beyond the $8 tier label made the process dearer than the work warranted.

## Self-critique of the agent system
Two independent review rounds each found real defects, so neither was waste; but the second existed only because the first fix answered a denylist finding with three more denylist entries instead of inverting the predicate — answering a finding at the level it was made, rather than at the level that closes the class, is what bought the extra round. Separately I measured a piped exit code (tail's status, not python's) and had to re-run the verify command verbatim; a piped check is not the check.
