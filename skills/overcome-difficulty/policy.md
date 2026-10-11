# Overcome-difficulty policy — recursive-escape mechanics

Elaboration moved out of `SKILL.md` to keep that trigger surface lean. The skill keeps the depth/budget rules + result-marker summary inline; the spawn recipe, full result handling, extra safeguards, and the Cursor variant live here.

## Invocation

Before spawning, verify the would-be `AGENT_RECURSION_DEPTH` does not exceed `max-recursion-depth` (see `~/.claude-agent/config.md`). If it would, follow § Safeguards § Hard depth cap and do not spawn.

Choose the budget tier per `~/.claude-agent/config.md` — `budget-medium-usd` is the default for overcome-difficulty escapes; use `budget-large-usd` only when the difficulty likely needs deep exploration.

The escape is the **`manager` kind** of `scripts/spawn-specialist.py`: the empty specialization — a depth n+1 manager with no role `SKILL.md` and no appended marker protocol, so it is a vanilla Claude Code root with your `CLAUDE.md`, memory and skills. Write the brief below to a file (any readable file; `--plan` inlines its text into the child's prompt), then:

```bash
python3 scripts/spawn-specialist.py --kind manager \
  --plan <brief file> \
  --done-criterion 'RESOLVED: the difficulty is resolved, or INVESTIGATION: findings returned' \
  --criterion-type acceptance-review \
  --budget medium --complexity high --effort high
```

The wrapper increments `AGENT_RECURSION_DEPTH`, refuses above `max-recursion-depth`, applies the global runaway budget ceiling, logs the spawn, and checks the return marker. The brief file holds:

```
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
3. If you yourself hit an unyielding sub-difficulty, escalate with the same mechanism (this recipe, one level deeper).
4. If your work needs an action outside your granted permissions, return PERMISSION-REQUEST: with the request instead of acting.

Reply with one of these exact markers on the first non-empty line of your final output:
- RESOLVED: <one paragraph resolution + concrete next action for the caller>
- INVESTIGATION: <findings + what you would try next, if you investigated but could not resolve>
- LOOP_DETECTED: <how this task mirrors an ancestor's task you noticed, if AGENT_RECURSION_DEPTH is at or above loop-sensitivity-depth (see ~/.claude-agent/config.md) and the pattern repeats>
- PERMISSION-REQUEST: <the specific action, why it is needed, and the fallback if denied>
```

The wrapper computes the child's `AGENT_RECURSION_DEPTH` (current value + 1) and puts it at the top of the child's prompt, so the spawned model sees its depth directly without reading env. The `manager` kind accepts exactly these four markers; the specialist marker set (`COMPLETED:` etc.) is not valid for it.

## Reading the result

The child returns to stdout. The Bash tool result will start with one marker:

- **`RESOLVED:`** — apply the resolution. Continue the original work.
- **`INVESTIGATION:`** — incorporate findings. Decide whether to retry inline, escalate to the user, or accept partial.
- **`LOOP_DETECTED:`** — the recursion is not converging. Stop, summarize for the user, ask for direction. Do not spawn again on the same difficulty.
- **`PERMISSION-REQUEST:`** — the child needs an action outside its grants. Route it as for any specialist (the `Rule:` / `Resource:` format of the marker protocol); the user decides.

Correspondence with the specialist vocabulary: `RESOLVED` ~ `COMPLETED`, `INVESTIGATION` ~ `INCOMPLETE`, `LOOP_DETECTED` ~ `ESCALATE`, `PERMISSION-REQUEST` ~ `PERMISSION-REQUEST`. Unifying the two vocabularies is a later task; until then the `manager` kind validates only the escape set.

If the child hits its budget cap (`--max-budget-usd`) without emitting a marker, treat the output as `INVESTIGATION:` even without the prefix.

## Safeguards (beyond the inline hard depth cap)

- **Per-level budget** — `--budget` is the tier label recorded for the spawn (default `medium` = `budget-medium-usd` for overcome-difficulty spawns); the wrapper passes the global `spawn-runaway-ceiling-usd` as the actual `--max-budget-usd` kill. Hitting the cap returns control to the caller.
- **Visible depth** — `AGENT_RECURSION_DEPTH` is in env and in the prompt; each level knows where it is in the stack.
- **Loop sensitivity at depth ≥ `loop-sensitivity-depth`** (see `~/.claude-agent/config.md`) — the spawned level must self-check whether its task is a re-framing of an ancestor's task. If yes, return `LOOP_DETECTED:` early rather than recursing further.
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
