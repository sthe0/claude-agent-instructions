---
name: log-reading-discipline
description: Hard limit on how many lines you may dump from a single log file per tool call. Aggregate first, then surface counters and short summaries — context is the scarce resource.
type: reference
---

# Log-reading discipline

When investigating logs, traces, or any saved blob with `cat` / `head` / `tail` / `grep` / `rg` / `sed` / `awk` / `python` / `jq` or a pipeline thereof, **no single tool call may emit more than 10 lines from one log file**.

Rule is per tool call, per file. Not per session, not per turn.

## Why

Logs are the worst context-economy material there is: high volume, low signal density, easy to dump thousands of lines that the model then has to carry through every subsequent turn. The 10-line cap forces you to aggregate first (counts, distributions, top-K, time windows) and surface only the lines that materially change the hypothesis. Adapted from `<arcadia>/ai/artifacts/skills/gena/gena-investigate/SKILL.md`.

## What to do instead

1. **Aggregate in a file or script.** Write counts, histograms, top-N errors, time-bucketed counts to a temp file or stdout pipe; emit only the digest.
2. **Pre-filter by identifier.** `request_id` / `order_id` / `trace_id` / timestamp window — narrow first, then read.
3. **Sample, not stream.** When you need raw lines, take 1–3 representative cases per class of behavior; never the full stream.
4. **Stage the search.** First pass: counts. Second pass: distinct error texts. Third pass: 1–3 example lines for the most relevant class.

## Carve-outs

- A single line genuinely longer than 10 visual lines (e.g. a JSON event pretty-printed) counts as one line. The cap is about *log lines*, not characters.
- Reading **source code or markdown** is not covered — the `Read` tool already has its own page mechanics.
- If the user explicitly asks for raw log dump in their reply, comply — the rule is your default, not theirs.

## When to expand

If after three staged passes you still cannot localize the issue, that is a difficulty — invoke `overcome-difficulty` rather than dumping a larger window.

## See also

- `<arcadia>/ai/artifacts/skills/gena/gena-investigate/SKILL.md` — upstream rule + a 20-min investigation budget that pairs well with this discipline.
- `~/.claude/skills/overcome-difficulty/SKILL.md` — when log search isn't converging.
