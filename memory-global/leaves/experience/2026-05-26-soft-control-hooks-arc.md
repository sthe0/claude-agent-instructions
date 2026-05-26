---
name: soft-control-hooks-arc
description: Self-improvement round that grew out of the nanobot CRON_TZ diagnosis — frontmatter sentinel for experience leaves, CLAUDE.md token-trim, three soft-control hooks (self-critique reminder, tracker reminder, push-confirmation reminder), and one rejected proposal (hard cap on memory files).
type: reference
resolution_confirmed_by_user: "да"
---

# Soft-control hooks arc — 2026-05-26

Five-commit round triggered by a self-improvement signal embedded in the
`2026-05-26-cron-tz-user-crontab-trap` experience leaf. The user later
asked to look for more code-driven enforcement candidates across the
instruction surface, which expanded the round into three packs.

## Final plan as executed

| # | Commit | Pack | What |
|---|---|---|---|
| 1 | `c3e897e` | (pre-round) | `verify-experience-leaf.py` (PreToolUse + `--staged` + default-all + direct file) + frontmatter sentinel `resolution_confirmed_by_user`; coordinator-pitfalls row; `lint-cursor-mirror.py` structural check; backfilled two existing leaves with `<retroactive>` value. |
| 2 | `ded1df9` | A (token economy) | CLAUDE.md compress: § Spawning specialists / § Recursion cap / § Return markers. 380 → 338 lines (~420 tokens/session). Kept cognitive content (which inputs to supply, when to escalate); dropped wrapper-internal documentation duplicated by `spawn-specialist.py --help`. |
| 3 | `1177ac3` | A side | Feedback leaf `feedback-no-hard-caps-on-memory.md` after user rejected the proposed `memory-md-max-lines: 200` ceiling. Distinguishes instruction surfaces (hard ceiling) from memory content stores (soft curation). |
| 4 | `63aa7a5` | B (soft control) | `hook-self-critique-reminder.py` (PostToolUse Write → § Self-critique substantive → stderr nudge to invoke `self-improvement`) + `hook-tracker-reminder.py` (UserPromptSubmit → ticket-key regex or keywords → stdout nudge to invoke `tracker-management`). Both fail-open. |
| 5 | `9eeef94` | C (push gate) | `hook-push-confirmation-reminder.py` (PreToolUse Bash → `git push` / `sync-instructions-repo.sh push` → stderr nudge to verify user confirmation). Warn-only by user choice — false-block cost > false-pass cost for a rare action. |

All five commits pushed to `origin/main`.

## Difficulties

- **`verify-experience-leaf.py` initial CLI failed under `verify-all.py`.** Script supported `--staged` and `--hook` but not the default no-args mode that `verify-all.py` invokes. Refactored into a unified `_scan(mode)` with `mode in {"staged", "all"}`. **Lesson**: every new `verify-*` / `lint-*` script must pass `python3 scripts/verify-all.py` (no args) before commit; the missing mode surfaced only at integration.

- **M1 threshold tuning.** First version required `≥ 3 non-blank lines AND ≥ 100 chars` for "substantive". Real critique sections often fit in 2 paragraphs (e.g. today's CRON_TZ leaf had exactly 2). Lowered to char-count-only (`≥ 80`) — more robust to formatting variation.

- **Backfill vs grandfather.** Two existing experience leaves (`2026-05-24-coordination-refactor`, `2026-05-25-code-driven-enforcement-arc`) lacked the new frontmatter field and broke `verify-all.py`. Chose explicit backfill with a `"<retroactive: rule introduced 2026-05-26; original confirmation not captured at write time>"` sentinel over a date-based grandfather clause in the verifier — honest, sortable, and the rule stays uniform.

- **H2 rejection (proposed `memory-md-max-lines: 200` lint).** I proposed mirroring the CLAUDE.md / cursor mirror / SKILL.md / policy.md ceilings onto `MEMORY.md`. User pushed back: "не надо жестко ограничивать память, память пусть растёт." Captured as a feedback leaf; the principle is **instruction surfaces vs memory content stores**, which I had been silently conflating.

- **SSH push failed twice.** First push attempt blocked by `Permission denied (publickey)` — user's secure-enclave keys not visible to the process. Resolved when user ran `ssh-add` interactively. Second push rejected because origin advanced during the session; `pull --rebase` + reconcile + re-confirmation per policy.

- **Recurring `date: invalid argument 's' for -I` from `sync-instructions-repo.sh`.** BSD `date` on macOS does not accept GNU's `-Is`. Not blocking — just noise in the output. Worth fixing in a follow-up.

## Artifacts

- Commits: `c3e897e` → `ded1df9` → `1177ac3` → `63aa7a5` → `9eeef94`, all on `origin/main`.
- New scripts in `~/claude-agent-instructions/scripts/`:
  - `verify-experience-leaf.py` (4 modes: `--staged`, default-all, `--hook`, direct file).
  - `hook-self-critique-reminder.py` (PostToolUse Write).
  - `hook-tracker-reminder.py` (UserPromptSubmit).
  - `hook-push-confirmation-reminder.py` (PreToolUse Bash).
- Updated: `CLAUDE.md`, `cursor-rules/claude-code-sync.mdc`, `memory-global/leaves/coordinator-pitfalls.md`, `memory-global/MEMORY.md`, `scripts/verify-all.py`, `scripts/verify-layout-contract.sh`, `scripts/lint-cursor-mirror.py`, `skills/self-improvement/policy.md`, `~/.claude/settings.json` (PreToolUse Write + PreToolUse Bash + PostToolUse Write + UserPromptSubmit).
- New memory leaves: `feedback-no-hard-caps-on-memory.md`, this leaf.

## Lessons

- **Warn-only over hard-block for low-frequency external actions** where the cost of a false block (agent abort mid-deploy, confused recovery) exceeds the cost of a false pass (force-push back is available, or the user notices and corrects). Push, deploy, prod commands fit this pattern. Hard blocks suit high-frequency local actions where mistakes accumulate silently (the frontmatter sentinel for experience leaves).

- **Distinguish instruction surfaces from content stores when proposing ceilings.** Instruction files (`CLAUDE.md`, cursor mirror, SKILL.md, policy.md) are loaded into every session prompt — token budget is real, hard ceilings protect it. Memory (`MEMORY.md` indices, leaves) accumulates across sessions — curation is judgment, not a linter job. Confusing the two was today's misstep.

- **Three event types cover the soft-control palette**: `PreToolUse` (anticipate-and-block or nudge before action), `PostToolUse` (react-with-nudge after action), `UserPromptSubmit` (scan-input as system context). Pick by where the signal lives.

- **For verify-script CLI conventions in this repo**: support `--staged` (pre-commit) + default no-args (full repo, called by `verify-all.py`) + any hook-specific mode (`--hook`, `--file`). Test all three before commit; integration via `verify-all.py` is the first place a missing mode surfaces.

- **Heuristic thresholds (M1 char count, M2 false-positive ignore list) start permissive and tighten if noise becomes a problem.** Starting strict produces silent misses; starting permissive produces visible noise that's easy to tune.

- **`spawn-specialist.py --help` is the canonical source for spawn-flag mechanics.** CLAUDE.md prose duplicating it is dead weight loaded into every session.

## Self-critique of the agent system

- **I conflated "instruction file" with "any markdown file" when proposing the H2 lint.** The taxonomy "instruction surfaces vs content stores" exists implicitly in `CLAUDE.md` § Memory (three scopes) and `policy.md` § File structure (layout), but nowhere as a single explicit rule about *what kinds of files deserve hard ceilings*. The feedback leaf now captures it, but the same distinction should appear in `coordinator-pitfalls.md` (a "proposed a hard cap on memory" pitfall row) so it's reachable from the coordinator surface rather than only from feedback memory.

- **I missed the auto-trigger self-improvement step earlier in the day** after closing the CRON_TZ task. User reminded me. The new M1 hook fixes this specific recall miss, but the underlying pattern — "rule exists in CLAUDE.md, agent misses it" — applies to other CLAUDE.md rules that haven't been lifted to code yet. The "missed-leaf-at-resolution" entry in the earlier code-driven-enforcement-arc leaf already flagged this; today is the second instance of the same shape. Worth an entry in coordinator-pitfalls.md if not already there.

- **`sync-instructions-repo.sh` has a portability bug** (`date -Is` is GNU-only). The error doesn't block but appears on every invocation, training the agent to ignore script stderr — bad signal-to-noise. Fix in a follow-up.

- **No structural check that all new `verify-*` scripts register with `verify-all.py`.** Adding to `CHECKS` is manual. A simple lint that diffs the `scripts/verify-*.py` set against `verify-all.py`'s `CHECKS` list and reports drift would catch the "added a verify script but forgot to wire it in" failure mode that almost happened today.

## Cost & effort

- **Wall-clock**: ~3 h from the nanobot CRON_TZ diagnosis (09:25 MSK) to closing this round (~14:00 MSK), including the unrelated diagnostic phase, two SSH push failures, and the rebase pause.
- **`claude -p` spawn spend**: $0 — no specializations spawned this session; all work in-thread under the carve-out (per-step changes fit the *small change* bound).
- **User interventions beyond the initial brief**: 1 substantive correction (H2 rejection) + 1 design choice (warn-only for push gate) + ~6 routine push/resolution confirmations. The substantive correction is the one that taught a generalizable principle.
