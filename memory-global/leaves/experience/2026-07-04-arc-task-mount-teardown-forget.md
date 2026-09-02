---
name: 2026-07-04-arc-task-mount-teardown-forget
description: Per-task arc mounts were torn down with plain arc unmount, which is a detach (store under ~/.arc/stores/ + registry entry in ~/.arc/mount-points persist by design) — 11 stale WARN registry entries and 4 unused stores (~6.3G) accumulated silently. Full teardown is arc unmount --forget; stale WARN entries (store already gone) additionally need --force. Before forgetting a materialized store, check unpushed state: the vcs branch -v --json listing (remote field = tracking ref), the vcs log --oneline <remote>..<branch> per branch, the vcs status -s. Sweep mechanized as junk/<user>/agents/common/scripts/arc-mounts-gc.sh (PR 14261429).
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [memory-global/leaves/system-knowledge/landing-changes-core-git-and-arc.md, https://<internal-review-host>/review/14261429]
created: 2026-07-04
last_verified: 2026-09-02
---

# Arc task-mount lifecycle must end with unmount --forget; GC mechanized

## Difficulty
Desired: task-mount teardown leaves no residue. Actual: plain arc unmount kept stores+registry entries; 11 stale WARN lines and 6.3G of dead stores accumulated across weeks, discovered only when the user ran the vcs mount listing. Root cause: the arc backend workflow creates mounts but has no teardown verb (git side has land-branch.py deleting worktree+branch; arc side had nothing), and no mechanism invoked cleanup at any lifecycle point.

## Order & criterion
1) Inspect: parse ~/.arc/mount-points + the vcs mount listing; classify garbage into stale registry entries / unused stores / orphan dirs. 2) For each unused store: remount, check unpushed branches (the vcs log <remote>..<branch>) + the vcs status -s; keep-and-report dirty ones. 3) Forget clean ones (arc unmount --forget; --force --forget for stale entries). 4) For the kept dirty store: verify its unique content against trunk byte-for-byte before deciding — the unpushed commit and uncommitted files can be fully superseded. 5) Mechanize: GC script + wiring into lifecycle (resolution-gate hook nudge; backend teardown verb).

**Acceptance check:** the vcs mount listing shows only live mounts: zero WARN lines, zero stores without a mount, orphan dirs removed; every deletion preceded by a persisted inspection log proving the store was clean or superseded.

## Contexts

### 2026-07-04 — initial
- Where it arose: Machine with arc task-mount workflow (~/task-mounts anchor model); any session that creates per-task arc mounts or reviews the vcs mount listing.
- Working plan: ~/.claude-agent/plans/arc-mount-store-cleanup-v1.toml (5 stages, all PASSED)


### 2026-07-04 — Mechanized the invocation points (same day, follow-up task autonomous-arc-mount-teardown)
- Where it arose: User pushed twice: 'why no autonomous cleanup proposal' and 'why is edit 2 text, not code' — the SI tie-breaker (existing mechanism > prose) applied; both fixes delivered as code in one VCS-trunk PR 14261837.
- Working plan: Plan autonomous-arc-mount-teardown-v1.toml (2 stages: spawn:developer PR, in_thread verification); thinker review r1=revise(6)/r2=pass; developer needed 2 spawns (first INCOMPLETE at 3 USD cap — recurring pattern, continuation via context dossier); PR adds fs-only residue nudge to hook-resolution-land-arc.py + backend_teardown_workspace() with CLAUDE_DRY_RUN + teardown-arc-mount.sh CLI; 34/34 hermetic tests; verification mount itself torn down by the new CLI (dogfood).


### 2026-09-02 — residue reached ENOSPC and broke the harness's own diagnostic channel
- Where it arose: Workstation root filesystem hit 100% (471G/492G, 138M free). Two consequences beyond 'no space': (a) the monorepo VCS mount at the ~/task-mounts anchor dropped, so every hook script referenced through it reported '/bin/sh: 1: ...hook-*.py: not found' across the fleet's other sessions — the symptom looked like broken hook wiring, not a disk problem; (b) the Bash tool could not write its own output file under the harness scratch dir, so command output was silently lost — the fix was to redirect every diagnostic into /dev/shm (RAM tmpfs) and read it with the Read tool. 13 orphaned temporary VCS stores, from ad-hoc mounts rooted under /var/tmp and /tmp rather than under the anchor, had accumulated since the 2026-07-04 sweep (~57G) — the mechanized GC from that context did not cover mounts created outside the anchor.
- Working plan: /tmp/plan-disk-cleanup.toml (3 stages, all PASSED): forget the unmounted temporary stores (57G) -> strip the build cache (~/.ya/build 100G to 2.8M) -> remove four finished-ticket /tmp dirs (8.4G). 138M free to 165G free, / from 100% to 66%. The anchor mount remounted itself once space existed; no remount needed. Two of the six planned /tmp dirs were kept after ls -ld showed same-day mtimes — the plan's material list was built from name patterns, not from mtimes, and only the pre-delete mtime check caught it.

### 2026-09-02 — a torn-down mount broke machine-local config that symlinked INTO it
- Where it arose: Any machine whose ~/.config carries symlinks into a per-ticket monorepo VCS mount. Here: all three ~/.config/claude/auth-profiles.d/*.sh were symlinks into ~/task-mounts/<TICKET-KEY>-.../junk/<user>/agents/common/project-entry/auth-profiles.d/. That mount was torn down; the dir remained but empty (mountpoint says 'not a mountpoint'), so every symlink dangled. Two symptoms, one cause, and NEITHER named a mount: (a) claude-personal printed '_auth_apply: profile file not found' and returned 1 WITHOUT exec'ing the binary (auth-profiles.sh _auth_apply_ns tests [[ ! -f ]] then 'return 1'), so the launcher looked like it 'stopped running commands'; (b) in a FRESH shell claude-personal did not exist at all -- claude-launchers.sh generates one function per profile at source time from _auth_list_ns, which filters via [[ -f ]], so a dangling symlink silently drops the launcher. The user's live shell still had the function from when the mount was alive, which is why the failure looked intermittent between shells. A red herring to skip next time: the dispatcher's 'no task specified' banner on 'claude-personal --resume <uuid>' is BY DESIGN (agent-dispatch.sh treats any leading '-' token except --new/--init as in-place launch) and still forwards the flag -- it was not the resume bug it appeared to be. The profile files exist in EVERY mount (junk/<user>/agents/common/project-entry/auth-profiles.d/), so the chosen target was arbitrary: whichever mount was cwd at install time. Fix: repoint the symlinks at the long-lived ~/task-mounts/main (user's pick over copying files locally, accepting that main is a VCS mount too). Verify with 'bash -lic' NOT 'bash -lc' -- .bashrc returns early for non-interactive shells, so -lc reports 'command not found' for a launcher that is actually fine.
- Working plan: in-thread diagnosis, no formal plan: locate launcher -> read claude-launchers.sh + auth-profiles.sh + agent-dispatch.sh -> confirm dangling symlink + dead mount -> repoint 3 symlinks to main -> verify via bash -lic + CLAUDE_LAUNCH_DRYRUN=1
## Common core & variations
**Common:** The mount lifecycle leaves residue that no single cleanup verb owns, and each context surfaces a new class of it (per-task stores under the anchor in 2026-07; ad-hoc mounts outside it in 2026-09) — so a sweep must enumerate the residue's real roots, not the ones the previous sweep happened to know. A cleanup plan that names deletion targets by NAME PATTERN must re-check each target's mtime immediately before deleting: the pattern encodes an assumption about lifecycle, not an observation of it. **2026-09-02 adds the INBOUND direction:** teardown also breaks references pointing INTO the mount, which no residue sweep can see because they live outside it — so before forgetting a mount, enumerate symlinks into it from machine-local config (`~/.config`, `~/.local`). The first three contexts all swept what the mount LEFT BEHIND; this one is what the mount was HOLDING UP.

**Variations:** A tool without an invocation point is not autonomy — mechanize the trigger (gate-time nudge) and the action (lifecycle verb) in the infra layer that owns the resource; backend specifics stay in backend-side hooks (Core stays org-neutral). Newer sub-lesson: when the root filesystem is full, the harness's own tool-output channel fails silently and a dropped FUSE mount makes ordinary scripts read as 'not found' — check df before believing a file-not-found on a mount-backed path, and route diagnostics through /dev/shm to keep working. Newest sub-lesson (2026-09-02): a dangling symlink in a config dir that some loader enumerates by glob + an `[[ -f ]]` filter fails OPEN and SILENT — the feature simply disappears instead of erroring, and only in processes started AFTER the breakage, so "works in my shell, missing in a fresh one" is the signature. Never site machine-local config inside an ephemeral per-ticket mount: if the content lives in the monorepo, copy it out or anchor at the long-lived mount. When probing whether a shell launcher exists, use `bash -lic` — `.bashrc` returns early for non-interactive shells, so `bash -lc` reports "command not found" for a launcher that is perfectly fine.

## Cost
~2h wall-clock; 2 developer spawns (~$5.85, first hit $3 budget cap and returned INCOMPLETE — continuation via spawn-specialist.py --context-dossier because agentctl dispatch has no continuation channel); thinker plan review x2

## Self-critique of the agent system
SI stopped at tool+knowledge (GC script + leaf) without proposing the invocation point (hook wiring, backend teardown verb) — the named CLAUDE.md failure mode 'propose the structural form yourself'; user had to push twice ('why no autonomous cleanup', 'why text not code'). Quality rated 3/5 by user for exactly this gap.
