# Coordination constants

Single source of truth for the numeric constants used by the coordination machinery defined in `CLAUDE.md`, the cursor mirror, and the skills. Imported into every Claude Code session via `@~/.claude/config.md` at the end of `CLAUDE.md`.

Edit values **here** — not in the prose that references them. Every cross-reference uses the **key**, not the literal value. Where a mechanical surface (spawn template, shell script) must embed a literal number, an inline comment names the key so `rg <key>` still finds the use site.

| Key | Value | Meaning |
|---|---|---|
| `max-recursion-depth` | `5` | Hard cap on `AGENT_RECURSION_DEPTH`. A spawn that would push the env var to a value **above** this is forbidden — escalate to the user instead. Applies to every `claude -p` invocation, including `overcome-difficulty`'s recursive escape. |
| `loop-sensitivity-depth` | `2` | At this depth and deeper, a spawned level must self-check whether its task is a re-framing of an ancestor's task and return `LOOP_DETECTED:` early rather than recursing further. |
| `budget-small-usd` | `1.00` | `--max-budget-usd` for small specialist work (single-file edit, narrow analysis, short plan refinement). |
| `budget-medium-usd` | `3.00` | `--max-budget-usd` for typical specialist work (multi-file change with tests, scoped refactor, standard plan). Default tier — pick when in doubt. Also the default for `overcome-difficulty`'s recursive escape. |
| `budget-large-usd` | `8.00` | `--max-budget-usd` for cross-cutting changes (multi-stage plan, full feature, expensive research). |
| `small-change-max-lines` | `20` | Upper bound (line count) for the *small change* class in task triage. ≤ this many changed lines + single file + no architectural decision = manager may handle in-thread without a plan-approval gate. |
| `substantive-wall-clock-min` | `30` | Lower bound (minutes) for the *substantive* class in task triage — work expected to take this long or more warrants the full coordination cycle. |
| `inline-mode-wall-clock-min` | `5` | Upper bound (minutes) for inline-mode specialist work — beyond this, prefer spawning `claude -p`. |
| `rediscovery-threshold-min` | `5` | Quality bar for post-resolution experience leaves — record if skipping the leaf would cost a future similar task at least this much rediscovery. |

Audit cross-references with `rg '<key>' --glob '*.md' ~/claude-agent-instructions/` to see who depends on each value.
