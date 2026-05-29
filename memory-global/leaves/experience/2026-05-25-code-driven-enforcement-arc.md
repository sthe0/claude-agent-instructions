---
name: code-driven-enforcement-arc
description: Nine-iteration build-out of code-driven enforcement for the instructions repo (verify-* scripts, hooks, structured permissions, spawn wrapper, cost log) plus three follow-up rule additions. Lessons on process-as-code pacing, verify-script ROI, and the cognition/process boundary.
type: reference
resolution_confirmed_by_user: "<retroactive: rule introduced 2026-05-26; original confirmation not captured at write time>"
---

# Code-driven enforcement arc — 2026-05-25

A long session that started from a critique of the global instruction set
(see § Self-critique below for the prompts) and ended with most code-tractable
items closed. The output is a layered set of scripts, hooks, and structured
config that the harness now enforces automatically. Cognition stays in prose.

## Final plan as executed

Eleven commits across nine iterations + three rule additions.

| Iter | Commit | Adds |
|---|---|---|
| 1 | `f612e2b` | `verify-language.py` + `verify-all.py` entry point + `githooks/pre-commit`. First check infrastructure. |
| 2 | `1006ad1` | `permissions/global.json` + `permissions-cli.py` (then named `permissions.py`) + `lint-permissions.py` (then `verify-permissions.py`). Old free-form markdown leaf removed. |
| 2.5 | `6d1610b` | Rename: `permissions.py` → `permissions-cli.py`, `verify-permissions.py` → `lint-permissions.py`. User feedback ("nouns without verbs are unclear"). |
| 3 | `bd709d1` | `spawn-specialist.py` — recursion cap, budget tier resolution, auto-embed of permissions digest, return-marker validation, cost-log append. Five critique items in one wrapper. |
| 4 | `62e47f2` | `verify-cross-refs.py` — markdown link + inline-code path validity. Filters: glob/placeholder chars; `docs/migrations/` skipped wholesale. |
| 5 | `d3e512a` | `lint-cursor-mirror.py` — flat-skill parity, specialization parity, `**TRIGGER:**` marker presence. Structural only, no text comparison. |
| 6 | `2b2f6f7` | `cost-report.py` + `memory-audit.py` + refused-event logging in the spawn wrapper. Informational, not gating. |
| 7 | `5f166e7` | `skills-local/` accepts directories (`<name>/SKILL.md`) symmetric with `skills/`. `prune_dangling` logs to stderr + `~/.local/log/setup-symlinks-prune.log`. |
| 8 | `b5ce3ed` | `verify-self-improvement-edit.py` + `githooks/commit-msg`. Edits under `skills/self-improvement/` require `[self-improvement-reviewed]` in the commit message body. |
| 9 | `0a49684` | `lint-prose-length.py` with ceilings in `config.md`. Mechanical guard on growth of always-loaded policy text. |
| +3 | `10e1329` | Three rule additions: `policy.md` § Process as code; CLAUDE.md § On task resolution → 6th required section (Cost & effort); planner SKILL.md § Reuse vs generalization. |

Deferred (work-machine only, no Arcadia on this Mac):
**Idea #4 from end-of-session feedback** — a `ticket-spawn.sh` script for
`~/arcadia/robot/deepagent/.claude/scripts/` that mounts a per-ticket arc
branch, computes the relative project path from cwd, and invokes
`spawn-specialist.py`-style `claude -p` with cwd set in the new mount.
Design captured in the conversation; implementation requires the work
machine.

## Difficulties

- **PyYAML not on Python 3.9.** Iter 2 originally planned YAML for permissions.
  Mac stock Python has no `yaml`. Switched to JSON (stdlib). Lesson:
  cross-machine scripts must lean on stdlib unless we own the install path.

- **verify-cross-refs false-positive flood on first run.** 34 "broken refs"
  reported; almost all were glob patterns (`agents/*.md`) or placeholder
  templates (`skills/<name>/`) in prose. Fixed by (a) filtering inline-code
  refs with `* ? < > [ ] { }` chars; (b) skipping files under
  `docs/migrations/` (migration notes legitimately reference old paths).
  True positive count after filters: 0.

- **"inline" terminology overlapped two unrelated mechanisms.** Old CLAUDE.md
  used "inline mode" for two distinct things: (a) reading a specialization
  `SKILL.md` and acting as that specialist without `claude -p`; (b) flat
  skills invoked via the `Skill` tool in the main thread. Removing (a) per
  user simplification ("always `claude -p`") required disambiguating each
  reference one at a time. `rg "inline"` across all `*.md` / `*.mdc`,
  classify each hit.

- **Repeated git rebase conflicts early in the session.** The work-machine
  branch was being actively pushed in parallel. Three consecutive rebase
  rounds. Eventually serialized: pull → reconcile → commit → push as a
  short tight loop. After hour 1 conflicts dropped to zero.

- **`claude -p --output-format json` schema undocumented.** Wrote a tolerant
  parser: try `result` then `output`; try `cost_usd` then `total_cost_usd`;
  log `null` on missing. End-to-end test confirmed both `result` and
  `cost_usd` exist as expected on this version.

- **Pre-commit caught me mid-iteration on my own writing.** Iter 9 follow-up
  introduced an inline-code reference to `` `scripts/workflows/<name>.py` `` (an
  illustrative path that does not exist). `verify-cross-refs` blocked the
  commit. Rephrased to placeholder form `scripts/workflows/<name>.py`
  (skipped by filter), commit passed. Loop closed in real time.

- **Ambiguous user input.** `lf` from the user in response to a "push?"
  prompt — almost certainly a typo for `да`, but ambiguous. Asked for
  confirmation rather than assuming. Reinforces: irreversible actions
  (push) deserve explicit confirmation even when context strongly suggests
  intent.

## Artifacts

Commits in `origin/main`: `f612e2b 1006ad1 6d1610b bd709d1 62e47f2 d3e512a
2b2f6f7 5f166e7 b5ce3ed 0a49684 10e1329`.

Scripts (all under `scripts/`):
- Verify / lint (in `verify-all.py` CHECKS): `verify-language`,
  `lint-permissions`, `verify-cross-refs`, `lint-cursor-mirror`,
  `lint-prose-length`.
- Tools (informational or workflow): `permissions-cli`, `spawn-specialist`,
  `cost-report`, `memory-audit`.
- Hooks: `verify-self-improvement-edit` (via `githooks/commit-msg`).

Hooks: `githooks/pre-commit` runs `verify-all.py --staged`;
`githooks/commit-msg` runs `verify-self-improvement-edit.py`;
`githooks/post-commit` already existed (push reminder).

Config: `config.md` grew with `claude-md-max-lines`, `cursor-mirror-max-lines`,
`skill-md-max-lines`, `policy-md-max-lines`.

Data files (machine-local, outside the repo):
`~/.local/log/claude-spawn-costs.jsonl` (spawn cost log),
`~/.local/log/setup-symlinks-prune.log` (dangling symlink log).

Rule additions in prose:
- `skills/self-improvement/policy.md` § Process as code (new top-level section).
- `CLAUDE.md` § On task resolution → § What to record → 6th required section
  (Cost & effort).
- `cursor/rules/claude-code-sync.mdc` § On task resolution (inline list
  updated to include item 6).
- `skills/specializations/planner/SKILL.md` § Reuse vs generalization (new
  section between Research and Cost).

## Lessons

- **Process-as-code is operationalizable iteration-by-iteration.** Eleven
  critique items closed by nine iterations averaging 30-200 lines each.
  No big-bang refactor. Each iteration shipped a working check and was
  exercised on the same commit when possible.

- **Verify scripts pay for themselves on the day they ship.** `verify-cross-refs`
  caught a real lint issue in the very session it landed (the `scripts/workflows/<name>.py`
  reference). The self-improvement commit-msg gate caught my own first commit
  attempt without the marker. The user does not need to be the linter.

- **Tier classification before writing a rule.** «Verify property X / fixed
  sequence A,B,C / think about Z» picks the file location and the rule shape
  in one step. Codified in Iter-9 follow-up under `policy.md` § Process as
  code; the rule is the rule that wrote itself.

- **JSON over YAML for shared scripts.** Stdlib-only is worth real readability
  cost. Markdown sidecars (e.g. `permissions/README.md`) can carry the prose
  explanation that YAML comments would have.

- **Glob / placeholder filters and per-dir skips are essential for any
  reference linter.** Documentation prose naturally writes `agents/*.md` and
  `skills/<name>/SKILL.md` as patterns. Without filters the lint is unusable.

- **One commit per iteration; push at the end of each.** Easy to bisect,
  easy to revert one piece. Some ceremony in confirming each push, but the
  granularity is worth it for risky structural changes.

- **`commit-msg` over `pre-commit` for any check that reads the commit
  message.** `pre-commit` for interactive commits only sees a draft;
  `commit-msg` sees the final.

- **Test the gate on its own commit when possible.** Iter 8 added the
  self-improvement gate AND touched `policy.md` — the same commit
  exercised the gate end-to-end (without marker → blocked; with marker →
  passed).

## Self-critique of the agent system

- **Missed `instruction-language.md` when first auditing contracts.** Read
  `file-structure-contract.md` and the verify scripts; assumed those were
  the contracts. Did not `ls` the parent directory. User caught the gap.
  Fix-pattern: when auditing "all contracts of X", first
  `ls memory-global/agent-instructions/` (or analogous) — do not enumerate
  by memory.

- **Almost missed writing this leaf.** Substantive task was resolved; user
  said "let's pause"; I proposed pause too. The CLAUDE.md rule says to
  write the leaf at resolution. I only proposed after the user said
  "continue" and there was nothing else obvious. The "On task resolution"
  trigger is too easy to forget when the work was iterative — perhaps a
  Stop-hook that detects "this turn closed a substantive task" (heuristic:
  `git log --since=<session-start>` shows ≥ 3 commits and the user said
  "thanks" / "пауза" / "остановимся" / equivalent) and prompts the
  experience-leaf decision.

- **`lint-cursor-mirror` is structural only.** Trigger wording can drift
  between `SKILL.md` frontmatter and the mirror block without detection.
  Acceptable today; revisit if a real drift surfaces.

- **No session-id on cost-log entries.** `cost-report --since <date>` works
  but cannot group by "this conversation". `$CLAUDE_SESSION_ID` (if exposed
  by the harness) would let `spawn-specialist.py` tag each entry and
  `cost-report.py` filter by session. Open improvement.

- **No CHANGELOG-style summary in the repo.** The git log carries the
  story but is hostile to a new reader. A short changelog document under
  `docs/` written at the end of each significant arc would orient future
  sessions / contributors.

- **Per-iteration push felt ceremonious in retrospect.** With pre-commit
  catching real issues at commit time and rebase conflicts mostly absent
  after hour 1, three-iter bundles would have been less interactive. The
  push-confirmation rule is right; the granularity of "what is a push
  unit" is judgment.

## Cost & effort

(Demonstrates the new 6th-required section the same session added.)

- **`$` spent on `claude -p` spawns** (per
  `~/.local/log/claude-spawn-costs.jsonl`): one real spawn at
  **`$0.24107375`** (Iter 3 end-to-end test, `developer` specialization,
  returned `CLARIFY:`, 14.974 s). All other "spawn-related" work was
  `--dry-run` (no cost). Total spawn spend for this arc: **`$0.24`**.

  Caveat — this measures only spawn cost. The main session cost (this
  conversation, with all the file edits, reads, verify runs) is **not**
  in this log; it is billed separately to the harness. A rough order-of-
  magnitude guess: 1-2 orders larger than the spawn line item. Cost
  telemetry needs to be widened to cover both surfaces.

- **Wall-clock duration:** approximately **5 hours** of active session.

- **User interventions:** approximately **30** across the arc, categorized:
  - Pure confirmations (`да`, `yes`, `поехали` between iterations): ~13.
  - Substantive corrections: 5
    - "rename for clarity, use verbs" (permissions scripts).
    - "always `claude -p`, drop inline-mode" (simplify routing).
    - "alternatives `cli`, `lint` are clearer" (naming).
    - "do not limit to memory; analyze all available sources" (planner
      reuse search scope).
    - "`lf`" (typo, confirmed as push).
  - Substantive new ideas (end-of-session feedback): 4 — process-as-code
    as explicit rule, deepagent ticket-spawn script, cost & effort in
    leaves, reuse vs generalization.
  - Other: scope / design questions, push approvals, "продолжим".

  High-intervention by design: the work was collaborative refactor of the
  policy layer. A typical substantive task should aim for far fewer
  interventions per resolution; use this number as a counter-example, not
  a target.
