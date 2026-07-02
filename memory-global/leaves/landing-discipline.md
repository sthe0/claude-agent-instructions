---
name: landing-discipline
description: At the resolution gate, committed work must reach its terminal VCS state — trunk/main — not sit on a personal branch; the full checkpoint-vs-delivered distinction, land mechanisms, and the AskUserQuestion bundling rule.
type: feedback
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-02
---

# Landing discipline — push, then land into trunk/main at the resolution gate

The short rule and its trigger live in CLAUDE.md § On task resolution; this leaf carries the full narrative, the delivery mechanisms, and the bundling contract.

## Difficulty

`resolve` records task-done but has **no VCS-integration beat**, so committed work strands — first **locally** (unpushed: no remote backup, defeating the "push after each commit" safety net), then on a **personal remote branch reported as "delivered" while never landed into trunk** ("подвисла ветка"). The sharpest form: when landing isn't a trivial fast-forward — a review-gated repo lands via PR, **never** a fast-forward — a fast-forward-only rule silently excludes the case and the branch is called done while it sits outside trunk.

## Guidance

**The terminal state is trunk/main, not a personal branch** — work parked on a personal remote branch is a *checkpoint*, never "delivered"; striving to land it is the default, **never a passive "tell me if you want to push"**.

Bundle the delivering step into the resolution `AskUserQuestion` with the delivering option **first and `(Recommended)`**:

- **(a)** if the branch is unpushed / ahead of upstream, **push** it first — pushing a personal / working branch is pre-authorized (CLAUDE.md § Acting without asking #4), a checkpoint you just do;
- **(b)** then **land into trunk/main via that repo's landing mechanism** — a git fast-forward (`scripts/land-branch.py`: ref-only `git push <remote> <branch>:<trunk>` ff + `push --delete` + `git branch -f`, refusing non-ff), *or* a PR create→publish→merge for a **review-gated repo** (a GitHub Core PR the owner merges; org-specific PR ship/merge commands live in **project memory**, not Core — org specifics stay out of Core prose).

**Landing is not contingent on a trivial fast-forward**: if trunk moved or the repo gates on review, that's a landing *path* (rebase / PR), not a licence to leave the branch stranded. Trunk/main-push needs explicit confirmation, so it rides that same click-gate — but the default you present is **landing, recommended**.

Never hand-roll `checkout` / `reset --hard` / `clean` on a shared tree; leave any parallel-session uncommitted WIP untouched.

`hook-resolution-reminder.py` surfaces this for **git** (landable or unpushed branch); an org's common infra surfaces it for a review-gated personal branch ahead of trunk — those org specifics stay out of Core and live in project memory.

## See also

- `~/.claude/CLAUDE.md` § On task resolution — the short rule + pointer that loads this leaf.
- [[acting-without-asking]] § carve-out #4 — personal-branch push is pre-authorized.
- `scripts/hook-resolution-reminder.py` — the delivery-point mechanism that nudges the land/push at the gate and cites this leaf.
