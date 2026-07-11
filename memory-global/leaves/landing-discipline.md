---
name: landing-discipline
description: At the resolution gate, committed work must reach its terminal VCS state — trunk/main — not sit on a personal branch; the full checkpoint-vs-delivered distinction, land mechanisms, branch deletion as part of landing, and the AskUserQuestion bundling rule.
type: feedback
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-11
---

# Landing discipline — push, then land into trunk/main at the resolution gate

The short rule and its trigger live in CLAUDE.md § On task resolution; this leaf carries the full narrative, the delivery mechanisms, and the bundling contract.

## Difficulty

`resolve` records task-done but has **no VCS-integration beat**, so committed work strands — first **locally** (unpushed: no remote backup, defeating the "push after each commit" safety net), then on a **personal remote branch reported as "delivered" while never landed into trunk** ("подвисла ветка"). The sharpest form: when landing isn't a trivial fast-forward — a review-gated repo lands via PR, **never** a fast-forward — a fast-forward-only rule silently excludes the case and the branch is called done while it sits outside trunk.

## Guidance

**The terminal state is trunk/main, not a personal branch** — work parked on a personal remote branch is a *checkpoint*, never "delivered"; striving to land it is the default, **never a passive "tell me if you want to push"**.

Bundle the delivering step into the resolution `AskUserQuestion` with the delivering option **first and `(Recommended)`**:

- **(a)** if the branch is unpushed / ahead of upstream, **push** it first — pushing a personal / working branch is pre-authorized (CLAUDE.md § Acting without asking #4), a checkpoint you just do;
- **(b)** then **land into trunk/main via that repo's landing mechanism** — a git fast-forward (`scripts/land-branch.py`: ref-only `git push <remote> <branch>:<trunk>` ff + full branch cleanup, refusing non-ff; `--remote-only` skips the local `git branch -f` when the local trunk is checked out or pinned under foreign WIP), *or* a PR create→publish→merge for a **review-gated repo** (a GitHub Core PR the owner merges; org-specific PR ship/merge commands live in **project memory**, not Core — org specifics stay out of Core prose);
- **(c)** **branch deletion — remote, local, and its linked worktree — is PART of landing, not a separate ask** (user directive 2026-07-03: "всегда удаляй ветки после вливания"). `land-branch.py` does it by default (`--keep-branch` is the explicit opt-out); manual landing paths (PR merge, remote-only push) must end with the same deletion. `hook-resolution-reminder.py`'s merged-leftover probe nudges when a merged-but-undeleted branch remains.

**Any task-scoped scratch resource is torn down together with the branch at the gate** — not just its linked worktree. A worktree, a checked-out mount, a scratch container / VM, a temp clone: each was provisioned *for this task*, so each is cleaned up at the resolution gate alongside the branch, and the cleanup is folded into the **same** delivering `AskUserQuestion` option — never left as undisclosed local residue whose existence the user must rediscover. Two constraints ride along: (1) a live process **cannot tear down a resource it is sitting inside** (a mount / container is busy while your cwd is within it — POSIX `EBUSY`), so change out of it first; (2) tear down the resource itself, **never** force-purge its underlying store / volume, which may also hold unrelated state. The concrete teardown command for a non-git resource lives in **project memory**, not Core prose.

**"Review-gated" is defined by a distinct human reviewer, not by surface type.** A repo is review-gated when a separate person must approve the merge before it lands — that, and only that, selects PR create→publish→merge over a fast-forward. This is **orthogonal** to the executable-surface push-*confirmation* gate (CLAUDE.md § Instructions repository): "executable surface keeps a separate gate" governs *when you may push to trunk* (an explicit user OK), **not** whether you open a PR. So for a repo where you hold direct push rights and no distinct reviewer gates merge — e.g. the user's own instructions repo — **fast-forward land is the default; a PR there is the [[capability-before-offload]] anti-pattern** (an extra merge click offloaded onto the user). Reserve the PR flow for repos a separate human must review before merge. *(User correction 2026-07-11: "Зачем в PR? У тебя же есть все права".)*

**Landing is not contingent on a trivial fast-forward**: if trunk moved or the repo gates on review, that's a landing *path* (rebase / PR), not a licence to leave the branch stranded. Trunk/main-push needs explicit confirmation, so it rides that same click-gate — but the default you present is **landing, recommended**.

Never hand-roll `checkout` / `reset --hard` / `clean` on a shared tree; leave any parallel-session uncommitted WIP untouched.

`hook-resolution-reminder.py` surfaces this for **git** (landable or unpushed branch); an org's common infra surfaces it for a review-gated personal branch ahead of trunk — those org specifics stay out of Core and live in project memory.

## See also

- `~/.claude-agent/CLAUDE.md` § On task resolution — the short rule + pointer that loads this leaf.
- [[acting-without-asking]] § carve-out #4 — personal-branch push is pre-authorized.
- `scripts/hook-resolution-reminder.py` — the delivery-point mechanism that nudges the land/push at the gate and cites this leaf.
