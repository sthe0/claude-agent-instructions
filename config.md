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
| `rediscovery-threshold-min` | `5` | Quality bar for post-resolution experience leaves — record if skipping the leaf would cost a future similar task at least this much rediscovery. |
| `claude-md-max-lines` | `400` | Hard ceiling on `CLAUDE.md` line count. Above this, extract a section to a `memory-global/leaves/` leaf and reference it. Enforced by `scripts/lint-prose-length.py`. |
| `claude-md-max-bytes` | `39000` | Hard ceiling on `CLAUDE.md` UTF-8 byte size — a buffer under the harness's 40 000-byte ceiling, past which CLAUDE.md is silently truncated and tail rules are lost. Enforced by `scripts/lint-prose-length.py` (the line-count guard does not catch byte growth). |
| `readme-max-lines` | `140` | Hard ceiling on README.md line count — README is the thin entry-point; everything deeper lives under docs/. Enforced by scripts/lint-prose-length.py. |
| `cursor-mirror-max-lines` | `220` | Hard ceiling on `cursor/rules/claude-code-sync.mdc`. The mirror is thin by design. |
| `skill-md-max-lines` | `200` | Hard ceiling on any `skills/*/SKILL.md` or `skills/specializations/*/SKILL.md`. Detail belongs in a sibling `policy.md` or a memory leaf. |
| `policy-md-max-lines` | `400` | Hard ceiling on any `skills/*/policy.md`. Policies are allowed to be detailed but not unbounded. |
| `core-difficulty-mass-threshold` | `8` | Cluster mass (Σ severity over the severity-weight ladder low=1/medium=2/high=4/critical=8) at/above which a Core-difficulty cluster is flagged for a batched Core change (ADR-0001 § Difficulty-accumulation). Consumed by the digest `core-difficulty-digest.py`. **Basis:** `8` = four independent medium reports (4×2) or two high (2×4) — the recurrence point at which a difficulty has been reported enough to warrant a Core change rather than a one-off; any single critical short-circuits to flag regardless. Derived from the ladder, not guessed; recalibration guidance in `docs/architecture/core-difficulty-calibration.md` (ADR open question #2). |
| `difficulty-channel` | `startrek` | Default channel for filing Core difficulties from non-author machines. Overridden per-machine via `~/.claude/agent-identity.local` (`difficulty_channel=<name>`). Values: `startrek` (Yandex Tracker queue OOSEVENREPORT) or `github` (GitHub Issues sthe0/claude-agent-instructions). Core commit authority is determined solely by `git push --dry-run` capability — no per-machine config flag (a shared config file cannot distinguish machines). |

Audit cross-references with `rg '<key>' --glob '*.md' ~/claude-agent-instructions/` to see who depends on each value.
