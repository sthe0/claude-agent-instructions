# Overcome-difficulty policy — recursive-escape mechanics

Elaboration moved out of `SKILL.md` to keep that trigger surface lean. The skill keeps the depth/budget rules + result-marker summary inline; the spawn recipe, full result handling, extra safeguards, and the Cursor variant live here.

## Invocation

Before spawning, verify the would-be `AGENT_RECURSION_DEPTH` does not exceed `max-recursion-depth` (see `~/.claude/config.md`). If it would, follow § Safeguards § Hard depth cap and do not spawn.

Choose the budget tier per `CLAUDE.md` § Budget tier — `budget-medium-usd` is the default for overcome-difficulty escapes; use `budget-large-usd` only when the difficulty likely needs deep exploration.

```bash
# --max-budget-usd resolves to budget-medium-usd by default (~/.claude/config.md).
AGENT_RECURSION_DEPTH=$(( ${AGENT_RECURSION_DEPTH:-0} + 1 )) \
claude -p \
  --max-budget-usd 3.00 \
  --output-format text \
  "AGENT_RECURSION_DEPTH=$AGENT_RECURSION_DEPTH

You have been spawned as a fresh root coordinator to resolve a difficulty in isolation from any parent conversation. There is no prior history; treat the description below as a self-contained task.

Difficulty (in declaration form):
- Expected: <what the plan declared the result should be>
- Actual: <what actually happened>
- Mismatch: <one or two sentences naming the gap>

What has been tried so far (concise; do not retry blindly):
<bulleted list of approaches and what failed about each>

What you are asked to do:
1. Work through overcome-difficulty (declaration → investigation → critique).
2. Resolve the difficulty if you can.
3. If you yourself hit an unyielding sub-difficulty, escalate with the same mechanism (this prompt template, AGENT_RECURSION_DEPTH+1).

Reply with one of these exact markers on the first non-empty line of your final output:
- RESOLVED: <one paragraph resolution + concrete next action for the caller>
- INVESTIGATION: <findings + what you would try next, if you investigated but could not resolve>
- LOOP_DETECTED: <how this task mirrors an ancestor's task you noticed, if AGENT_RECURSION_DEPTH is at or above loop-sensitivity-depth (see ~/.claude/config.md) and the pattern repeats>"
```

The env-var line at the top of the bash command increments `AGENT_RECURSION_DEPTH` from the current process's env (default 0 if unset), then exports it to the spawned `claude` process. The same value is embedded as text in the prompt so the spawned model can see its depth directly without reading env.

## Reading the result

The child returns to stdout. The Bash tool result will start with one marker:

- **`RESOLVED:`** — apply the resolution. Continue the original work.
- **`INVESTIGATION:`** — incorporate findings. Decide whether to retry inline, escalate to the user, or accept partial.
- **`LOOP_DETECTED:`** — the recursion is not converging. Stop, summarize for the user, ask for direction. Do not spawn again on the same difficulty.

If the child hits its budget cap (`--max-budget-usd`) without emitting a marker, treat the output as `INVESTIGATION:` even without the prefix.

## Safeguards (beyond the inline hard depth cap)

- **Per-level budget** — `--max-budget-usd` (see `CLAUDE.md` § Budget tier — default `budget-medium-usd` for overcome-difficulty spawns) caps API spend at each level. Hitting the cap returns control to the caller.
- **Visible depth** — `AGENT_RECURSION_DEPTH` is in env and in the prompt; each level knows where it is in the stack.
- **Loop sensitivity at depth ≥ `loop-sensitivity-depth`** (see `~/.claude/config.md`) — the spawned level must self-check whether its task is a re-framing of an ancestor's task. If yes, return `LOOP_DETECTED:` early rather than recursing further.
- **Transcripts persist** — each spawned level leaves a session transcript at `~/.claude/projects/<cwd-hash>/<sid>.jsonl`. Useful for post-mortem if recursion was long.

## Cursor (use spawn-cursor-escape.py)

In **Cursor**, do **not** invoke the `claude` CLI for recursive escape (global hard gate). Use the wrapper instead:

```bash
~/claude-agent-instructions/scripts/spawn-cursor-escape.py \
  --expected '...' \
  --actual '...' \
  --mismatch '...' \
  --tried 'approach A — why it failed' \
  --tried 'approach B — why it failed' \
  --workspace /path/to/project
```

The script enforces `max-recursion-depth` from `config.md`, resolves `CURSOR_API_KEY` (env or `~/.cursor_api_key`), spawns `agent -p` with the same overcome-difficulty escape prompt, validates `RESOLVED:` / `INVESTIGATION:` / `LOOP_DETECTED:` on the first non-empty line (else `MALFORMED:`), and appends cost metadata to `~/.local/log/cursor-spawn-costs.jsonl`. Use `--dry-run` to preview prompt and command.

**When to use in Cursor:** after **two** full inline declaration → investigation → critique cycles on the same difficulty without convergence, or when context noise clearly anchors a wrong frame. Before that, prefer inline OD or asking the user. If spawn refuses (depth cap) or returns `LOOP_DETECTED:`, structured escalate to the user — do not relaunch external workflows on the same unexplained hypothesis.

**Reading the result:** same markers as § Reading the result above. On `RESOLVED:` — apply and continue. On `INVESTIGATION:` — merge findings into the plan. On `LOOP_DETECTED:` or cap refusal — stop and ask the user for direction.
