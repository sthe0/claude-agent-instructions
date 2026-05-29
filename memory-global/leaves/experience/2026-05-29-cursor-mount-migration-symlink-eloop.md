---
name: "migrate-cursor-namespace: invoking a script via a symlink self-linked .claude"
description: "Why running setup-local.sh through a mount's .claude symlink created a self-referential .claude (ELOOP) that broke a ticket worktree, how it was recovered, and the readlink -f fix. General footgun: a script that locates its own base dir via logical pwd must be invoked by its real path."
type: reference
resolution_confirmed_by_user: "Push и закрыть (Recommended)"
---

# Mount cursor-namespace update self-linked `.claude` (ELOOP)

Task: finish the cursor-namespace migration on the `robot/deepagent` arc mounts (wire `.cursor/agents` to the new `cursor/agents/` namespace). User asked "а маунты нужно обновить?" and chose a full `setup-local` run via `migrate-cursor-namespace.sh --all-deepagent-mounts`.

## Final plan as executed

No formal plan file (in-thread). Steps actually run:

1. Discovered 6 deepagent mounts; inspected `.cursor/agents` state (read-only) — only trunk was already on the new namespace, the rest had no agents, **zero drift** (so the migration's drift-risk did not apply).
2. Ran `migrate-cursor-namespace.sh --all-deepagent-mounts` per the user's choice → it **broke** on the first full `setup-local.sh` (DEEPAGENT-367) with `Too many levels of symbolic links`, exit 126; `set -e` aborted the rest.
3. **overcome-difficulty inline:** traced the ELOOP to a self-referential `.claude -> .claude` symlink, timestamped to the run → self-inflicted.
4. Recovered the one damaged mount: `ln -sfn <storage> <mount>/.claude`; verified `.claude/CLAUDE.md`, `link-skills.sh`, and the downstream `.mdc` / root `CLAUDE.md` symlinks resolve again.
5. Re-asked the user with the new information; switched to the **narrow** fix: `link-project-cursor-agents.sh` on the 4 ticket mounts (does not touch `.claude` → no self-link risk) — delivered the actual goal.
6. Fixed the root-cause bug in `migrate-cursor-namespace.sh` (`readlink -f` the real script path before invoking), committed `8176cf0`, pushed.

## Difficulties

- **ELOOP on a ticket worktree (self-inflicted).** Signal: `setup-local.sh: line 70: …/link-skills.sh: Too many levels of symbolic links`, exit 126. Root cause: `setup-local.sh` computes `STORAGE="$(cd "$(dirname "$0")/.." && pwd)"` with a **logical** pwd, by contract invoked as `<storage>/scripts/setup-local.sh`. `migrate-cursor-namespace.sh` invoked it as `$mount/.claude/scripts/setup-local.sh` — through the `.claude` symlink — so logical pwd resolved `STORAGE` back to `.claude` itself, and step 1 (`link "$STORAGE" "$ROOT/.claude"`) relinked `.claude` onto itself. Overcome by: localize (namei showed the self-link + fresh timestamp) → recover (storage was intact; one `ln -sfn`) → fix the caller.
- **The user's chosen path was the dangerous one.** "Full setup-local" was approved before the bug was known. After the failure, re-asked rather than retrying the same broad operation; the narrow `link-project-cursor-agents.sh` both avoids the hazard and is the minimal thing that achieves the goal (the only missing piece on the ticket mounts was step 7).

## Artifacts

- Fix commit: `8176cf0` (claude-agent-instructions) — `cursor/scripts/migrate-cursor-namespace.sh`.
- Recovery: `.claude -> ~/arcadia_claude_local/junk/the0/agents/robot/deepagent` on the DEEPAGENT-367 mount.
- All 6 mounts end consistent: `.claude` → storage (trunk + 4 tickets), `.cursor/agents` (3 each) on the new namespace.
- Failure log digest: `/tmp/cc-scratch/mounts-migrate.log`.

## Lessons

- **A script that derives its own base dir via logical `pwd` must be invoked by its real path.** Either the caller resolves it (`readlink -f`, done here) or the script uses `pwd -P` (physical). Invoking via a symlink that the script then resolves through its own location is a self-destruct path — especially when step 1 *rewrites that very symlink*.
- **`set -e` saved 3 mounts.** The abort-on-first-error contained the blast radius to one worktree. Worth preserving in migration helpers.
- **Re-ask after a failed approval.** The user's earlier "full setup-local" choice was made without the bug knowledge; surfacing the new fact and offering the narrow path was cheaper and safer than honoring the stale choice.
- **Recovery beats undo when the source of truth is intact.** The storage tree was never touched; only the mount-side symlink pointer was corrupted, so recovery was a single re-link.
- Optional follow-up (not done, out of approved scope): harden `setup-local.sh` line 26 with `pwd -P` as defense-in-depth — it lives in the `arcadia_claude_local` storage (separate arc VCS).

## Self-critique of the agent system

- `migrate-cursor-namespace.sh` (added during the cursor-namespace migration finalized earlier the same session) was shipped without being exercised against a real ticket mount whose `.claude` is a symlink-to-storage — the exact case it targets. The bug is now fixed, but the pattern is "migration helper merged without running its own happy path." Not yet a cross-leaf systemic pattern; logging it here so a future recurrence (helper script that mutates the symlinks it traverses) can be matched.
- No instruction/hook gap surfaced beyond this; `overcome-difficulty` discipline (localize before retrying an external/VCS/mount operation after failure) applied correctly and contained the damage.

## Cost, effort, and tool usage

- In-thread, no `claude -p` spawns. One difficulty cycle, fully inline.
- User interventions: 4 AskUserQuestion gates (do mounts need updating → which wiring → after-failure re-ask → push/close).
- Tools: `Bash` (discovery, run, recovery, verify), `Read`/`Edit` (setup-local inspection, migrate-script fix). No specialization spawns or skills beyond inline overcome-difficulty reasoning.
- Resource that drove cost: the `setup-local.sh` STORAGE-derivation contract — the single line `STORAGE="$(cd "$(dirname "$0")/.." && pwd)"` was the whole surprise.
