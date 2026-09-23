---
name: 2026-08-25-unisolated-judge-subprocess-recursion
description: Single-turn 'claude -p' judge calls ran with no environment isolation, so each child was a full session that loaded the fleet's own hooks, and those hooks called a judge again: 1583 one-call sessions ate a 5-hour Max quota window in 47 minutes, prompt template nested 126 deep, with no alarm anywhere because the advisor is fail-open. Isolating the child's config root then silently severed its LOGIN, because auth on this fleet is FILE-carried in $CLAUDE_CONFIG_DIR/.credentials.json — the same blindness reached from the other side (a permanently unconsulted judge instead of a quota fire). Two structural lessons: a subprocess of the agent that inherits the ambient config root is a re-entrancy hazard by construction, and replacing a process's config root removes every capability that root carried in FILES, so the fix is discharged only by ENUMERATING what the ambient root supplies with a disposition per item. Both guard sets on the way were denylists that had already lost members (3 of 5 judge-invoking hooks carried the marker; 3 of 6 guard-inversion shapes were caught) and both were repaired by inverting to a mechanically-derived allowlist.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [2b22784, 24af0bb, a622df4, 0bd7970]
created: 2026-08-25
last_verified: 2026-09-23
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


### 2026-09-23 — 2026-09-23 — macOS: auth is Keychain-carried, not file-carried
- Where it arose: lib/host_llm.py _lend_auth/_read_oauth_token(_from_keychain); trips project session 0ad1155a-7ef8-4778-9c70-be7a179a0ab2, stage 2 of the0fun-public-bot.toml blocked on this
- Working plan: No formal plan.toml — user-mandated detour, worked in isolated worktree ~/claude-agent-instructions-judge-keychain-auth (branch judge-keychain-auth), landed via land-on-main.sh to origin/main 075b961

## Common core & variations
**Common:** This leaf's own initial premise -- 'auth on this fleet is FILE-carried in $CLAUDE_CONFIG_DIR/.credentials.json' -- is itself platform-scoped and was wrong for macOS: `claude login` under a personal Pro/Max subscription writes the OAuth blob into the macOS login Keychain (service 'Claude Code-credentials', optionally hash-suffixed by CLAUDE_CONFIG_DIR) and never creates .credentials.json at all. _lend_auth's file-only read therefore always failed on a real macOS dev machine, so the isolated judge subprocess never authenticated -- surfacing as a permanently fail-open 'judge exited non-zero' with no informative stderr, exactly the silent-capability-loss failure mode this leaf already names, just via a second, platform-specific storage channel the ENUMERATE-what-the-ambient-root-supplies pass had not covered.

**Variations:** Fix: _read_oauth_token_from_keychain shells out to `security find-generic-password` (macOS-only no-op elsewhere), tried only when the file result is genuinely ABSENT (a present-but-broken file short-circuits first), and the plain non-hash-suffixed service name is only tried when config_dir is the actual default root (Path.home()/.claude) -- otherwise a non-default CLAUDE_AGENT_HOME could borrow a different identity's plain-named item. Two independent operational traps surfaced verifying the fix end-to-end: (1) TEST-SHARED-MODULE -- subprocess is one shared module object; a fixture that neutralizes the new Keychain subprocess.run call by patching subprocess.run itself loses to any test that patches the same name LATER in its own body, silently polluting that test's unrelated call-count/order assertions -- the durable fix is a conftest.py autouse fixture patching the whole named function (_read_oauth_token_from_keychain), not the shared subprocess call beneath it. (2) LAND-ON-MAIN LOCAL-CHECKOUT LAG -- land-on-main.sh pushes straight to origin/main via its own isolated worktree; it does NOT fast-forward the caller's own long-lived main checkout, so re-running the fixed code from that checkout immediately after landing still executed the PRE-fix bytes (verified: isolated_run_kwargs() still returned no_credential_file status until `git pull --ff-only` was run) -- a landed fix is not live in a given checkout until that checkout is pulled, and the safest proof is re-running the exact live command/flow the fix was meant to unblock, not just re-reading the merged diff.

## Cost
$24.25, 6 spawns, ~50 min active; quality 3 (user) — the result was reached and measured, but two review rounds and the cost overrun beyond the $8 tier label made the process dearer than the work warranted.

## Self-critique of the agent system
Two independent review rounds each found real defects, so neither was waste; but the second existed only because the first fix answered a denylist finding with three more denylist entries instead of inverting the predicate — answering a finding at the level it was made, rather than at the level that closes the class, is what bought the extra round. Separately I measured a piped exit code (tail's status, not python's) and had to re-run the verify command verbatim; a piped check is not the check.
