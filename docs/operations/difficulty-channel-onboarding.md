# Difficulty-channel onboarding

> How to configure and use the Core-difficulty filing mechanism on a new machine (ADR-0001 §
> Difficulty-accumulation).

## Overview

When the self-improvement skill detects a Core-target difficulty on a machine that cannot push to
the repo, it files a `DifficultyRecord` to a channel the contributor already has write access to.
The author-side `core-difficulty-digest.py` then clusters reports from all channels and surfaces
flagged clusters for a batched Core change.

Two channels are supported:

| Channel | Audience | Credential |
|---|---|---|
| `startrek` (default) | internal Yandex devs | `~/.tracker-token` (OAuth write token) |
| `github` | external contributors | `GITHUB_TOKEN` env var or `gh auth login` |

**Authority is not configured here.** Whether you are an author (can push to Core) is determined
automatically via `git push --dry-run`. If that succeeds, you are an author; no flag is needed.

## Per-machine setup

`setup-symlinks.sh` calls `configure-identity.sh` automatically, which creates
`~/.claude/agent-identity.local` with the default channel (`startrek`) if the file is absent. To
switch channel, edit the file:

```bash
# For external contributors — GitHub Issues
echo "difficulty_channel=github" > ~/.claude/agent-identity.local

# For internal Yandex developers (default)
echo "difficulty_channel=startrek" > ~/.claude/agent-identity.local
```

The file is machine-local and gitignored; it is never committed.

## Credential setup

### Startrek (internal channel)

You need a Yandex OAuth token with write access to the `OOSEVENREPORT` queue:

1. Generate an OAuth token at `https://oauth.yandex-team.ru/` for `startrek:write` scope.
2. Save it to `~/.tracker-token` (plain text, one line, no trailing newline).
3. Verify: `curl -s -o /dev/null -w '%{http_code}' -H "Authorization: OAuth $(cat ~/.tracker-token)" https://st-api.yandex-team.ru/v2/queues/OOSEVENREPORT`  
   → expect `200`.

### GitHub (external channel)

You need a GitHub personal access token with `repo` scope (or use the `gh` CLI):

```bash
# Option A — env var
export GITHUB_TOKEN=<your PAT>

# Option B — gh CLI
gh auth login   # follow prompts
```

Verify: `gh api repos/sthe0/claude-agent-instructions --jq .full_name` → expect
`sthe0/claude-agent-instructions`.

## Filing a difficulty manually

Use `scripts/file-difficulty.py` directly from any machine:

```bash
python3 ~/claude-agent-instructions/scripts/file-difficulty.py \
  --target CLAUDE.md \
  --ground 'gate wording ambiguous — non-author cannot tell when approval is required' \
  --severity medium \
  --evidence 'saw two conflicting rules in §Coordination and §Classify'
```

Add `--dry-run` to print the record without submitting. Add `--channel github` or
`--channel startrek` to override the machine default.

The command prints the channel-native handle on success: a Startrek issue key (`OOSEVENREPORT-n`)
or a GitHub issue URL.

## Author-side digest

On a machine with push rights, pull and cluster all filed difficulties:

```bash
python3 ~/claude-agent-instructions/scripts/core-difficulty-digest.py \
  --channel startrek --channel github
```

The digest groups records by `functional_ground`, sums severity weights, and flags any cluster
whose mass reaches the threshold in `config.md` (`core-difficulty-mass-threshold`). A flagged
cluster is ready for a batched Core change via the normal `self-improvement` → `planner →
approval → developer` spine.

## See also

- [ADR-0001](../adr/0001-consensus-architecture.md) — the full consensus architecture and the
  difficulty-accumulation mechanism.
- [Core-difficulty mass threshold](../architecture/core-difficulty-calibration.md) — calibration
  of the flagging formula.
- [Setup and distribution](setup.md) — global machine setup (`setup-symlinks.sh`).
- `scripts/file-difficulty.py` — the submission CLI.
- `scripts/core-difficulty-digest.py` — the author-side digest CLI.
