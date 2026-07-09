---
name: 2026-07-09-stalled-mid-stream-is-client-byte-watchdog
description: A turn that streams a very large tool_use input (a ~50 KB wholesale Write of a plan file) can go silent long enough for the CLI's stream-idle watchdog (dEi(), 300 s floor, raisable only via CLAUDE_STREAM_IDLE_TIMEOUT_MS) to finalize a partial response and append the synthetic text 'API Error: Response stalled mid-stream'. The aborted turn leaves no tool_result, so on resume the model reads its own transcript and concludes it 'announced the write and ended the turn without calling the tool' — a false self-diagnosis that invites behavioural fixes for a transport problem. Repeats deterministically as long as the same oversized Write is retried (4x here). Remedies: split the plan per plan-file-split.md and use Edit; optionally CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING=1 so tool-input bytes flow incrementally.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
created: 2026-07-09
last_verified: 2026-07-09
---

# "Response stalled mid-stream" is a client-side byte-watchdog, not a model no-op

## Difficulty
The same turn aborts four times in a row with 'API Error: Response stalled mid-stream. The response above may be incomplete.' Nothing in the transcript names the cause: the synthetic record carries model='<synthetic>' and zero usage, and the missing tool_result makes the model blame itself for never calling the tool.

## Order & criterion
Read the transcript for the shape of the failing turn, not just its text; find the emitter in the compiled CLI (strings over claude.exe, then Python re.finditer for context — ugrep hits a complexity limit on wide .{0,200} windows); correlate the stall with the size of the last announced tool input; stop rewriting the artifact wholesale.

**Acceptance check:** The watchdog constant is read straight out of the bundle (Math.max(env||0, 300000) — a floor, so the env var can only raise it) and the four stalls line up with Write payloads of 40-50 KB while all smaller writes in the same session succeeded.

## Contexts

### 2026-07-09 — initial
- Where it arose: Session 1c6baca4 on robot/deepagent, rewriting ~/.claude-agent/plans/deepagent-449-auto-eval-hermetic-build.toml (16 KB -> 49 KB over six writes).
- Working plan: Diagnose the repeated stall in session 1c6baca4; rename its tmux window to its purpose; keep the window list legible.

## Cost
One session; one thinker plan-review spawn (wasted, but it is what caught the error); final change was one line in ~/.tmux.conf.

## Self-critique of the agent system
Related failure the same day: before planning a hook to compose a tmux pane_title, I never probed what the display surface already rendered — choose-tree -Zw already composes '#{window_name}#{window_flags}: "#{pane_title}"' and Claude already writes the live ai-title into pane_title every frame. The four-stage plan was strictly worse than zero code; the thinker plan-review gate caught it, my own check did not. Probe the target surface's current behaviour before planning a mechanism to produce it.
