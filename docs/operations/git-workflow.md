# Git workflow

> How to pull Core updates, make self-improvement edits, and push changes back upstream.

## Pulling updates

```bash
~/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
```

`sync-instructions-repo.sh pull` fetches from the remote, rebases local commits on top, and resolves any conflicts. Run it before starting self-improvement work to avoid edit conflicts.

## Making and committing edits

Self-improvement edits follow the standard coordination spine: the `self-improvement` skill fires, writes a plan, and dispatches `developer` to apply the changes. Commits are made after each coherent change.

When directories, scripts, or symlinks change, also update:
- `skills/self-improvement/policy.md` § File structure — the canonical file-structure inventory.
- `scripts/verify-layout-contract.sh` — the machine-checked layout contract.
- `scripts/README.md` — the machine-checked script/hook inventory.

Run `scripts/verify-readme.py --fix` to reconcile the skills/specializations row sets, then fill any `TODO` cells by hand.

## Pushing upstream

```bash
# only after explicit user confirmation
git push
```

`push` to the remote happens **only after explicit user confirmation** — this applies to both the main Claude dialog and any specialist. The push is skipped gracefully when you lack push rights (your commit stays local). Full rule: [skills/self-improvement/policy.md](../../skills/self-improvement/policy.md) § Git sync.

## See also

- [setup.md](setup.md) — initial wiring (`setup-symlinks.sh`, `doctor.sh`).
- [layer-maintenance.md](layer-maintenance.md) — rebasing a Team or Personal layer over a moving Core.
