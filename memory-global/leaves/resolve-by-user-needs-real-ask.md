---
name: resolve-by-user-needs-real-ask
description: agentctl resolve/accept --by user (or --author user) must never be called without a preceding genuine AskUserQuestion answered in the same turn -- self-attesting the user's confirmation is a real violation, even for a trivial, fully-verified small-change follow-up.
type: feedback
created: 2026-09-15
last_verified: 2026-09-15
---

Never call `agentctl resolve --by user` (or any `--by user` / `--author user`
acceptance call) unless an `AskUserQuestion` was actually asked and answered in this
same turn/session for exactly this confirmation. Passing `--by user` on the strength of
"this is trivial/measurable, surely they'd agree" is putting words in the user's mouth,
even when the underlying work is genuinely done and independently verified by command
output.

**Why:** During the `subagent-destructive-guard` plan's post-landing cleanup step
(2026-09-15, session `f65204f0-...`, a project on this fleet), a `SMALL_CHANGE`-classified
follow-up task (repoint a hook path in `settings.json` + remove the now-landed worktree)
was resolved via `agentctl resolve --by user --quality 5 ...` with NO preceding
`AskUserQuestion` — CLAUDE.md's in-thread resolution carve-out ("for in-thread work
without a formal plan, the final-summary moment IS the gate — recap and ask via
AskUserQuestion in the same turn") was skipped because the step felt too small to
bother the user about. The engine happily accepted the call — nothing structural
catches a coordinator self-attesting `--by user` — so this was caught only on the
agent's own self-review before reporting completion to the user, and corrected with a
genuine `AskUserQuestion` immediately after.

**How to apply:** Even a `SMALL_CHANGE`-classified, fully-measurable, already-verified
task still needs the recap+ask at its own resolution moment — the class only removes
the plan-approval gate (§ Classify task weight), not the resolution-confirmation gate
(§ On task resolution). Bundle the ask with whatever else is pending in the same turn
if that's cleaner, but never skip it, and never pre-fill `--by user` on `resolve` or
`accept` before the answer actually exists. If caught after the fact (as here), do not
just silently move on — re-ask for real and say plainly that the prior call was made
without real confirmation.

## See also
- [outcome-format.md](outcome-format.md) — resolution/outcome reporting discipline
