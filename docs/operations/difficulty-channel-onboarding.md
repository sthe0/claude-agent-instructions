# Difficulty-channel onboarding

> How to configure and use the Core-difficulty filing mechanism on a new machine (ADR-0001 §
> Difficulty-accumulation).

## Overview

When the self-improvement skill detects a Core-target difficulty on a machine that cannot push to
the repo, it files a `DifficultyRecord` to a channel the contributor already has write access to.
The author-side `core-difficulty-digest.py` then clusters reports from all channels and surfaces
flagged clusters for a batched Core change.

Core ships **one** channel:

| Channel | Audience | Credential |
|---|---|---|
| `github` (default) | anyone with a GitHub account | `GITHUB_TOKEN` env var or `gh auth login` |

An organization whose contributors file into its own tracker attaches a second channel as a
machine-local **adapter plugin** — Core carries the port and the registry, never the org adapter.
See [Using Core in another organization](org-portability.md) § Difficulty-channel adapters and the
detect hook for the plugin contract; the rest of this page applies to a plugin channel unchanged,
substituting its own name and credential.

**Authority is not configured here.** Whether you are an author (can push to Core) is determined
automatically via `git push --dry-run`. If that succeeds, you are an author; no flag is needed.

## Per-machine setup

`setup-symlinks.sh` calls `configure-identity.sh` automatically, which creates
`~/.claude-agent/agent-identity.local` if the file is absent. The channel is **auto-detected** at that
point from host signals (via `difficulty_channel.detect`) — you normally do not set it by hand.

Detection precedence (first match wins), written into the file as `# detected:` comments so you can
see why a channel was chosen:

1. **The machine-local detect hook decides**, if one is installed and it returns a verdict. *Which*
   host signals identify an organization — a corp hostname suffix, an internal toolchain on `PATH`,
   an internal credential agent — is org data, so it lives in the hook and never in Core. A hook
   that returns no verdict defers to the neutral rules below.
2. Else, **any GitHub credential → `github`** (`~/.github-token`, `$GITHUB_TOKEN`, or `gh`).
3. Else → `github` (safe public default), with a warning that no credential was found.

If the chosen channel lacks its own write credential, the created file carries a `# detected: warning:`
line so the omission is visible before the first filing.

To **override** the detected value (or switch later), edit the `difficulty_channel=` line:

```bash
# Force the public GitHub Issues channel
echo "difficulty_channel=github" > ~/.claude-agent/agent-identity.local

# Force an org channel installed as an adapter plugin
echo "difficulty_channel=<plugin name>" > ~/.claude-agent/agent-identity.local
```

`configure-identity.sh` **never overwrites an existing file**, so a manual choice (or a prior
detection) is always preserved. The file is machine-local and gitignored; it is never committed.

## Credential setup

### GitHub (the built-in channel)

You need a GitHub personal access token with `repo` scope (or use the `gh` CLI):

```bash
# Option A — env var
export GITHUB_TOKEN=<your PAT>

# Option B — gh CLI
gh auth login   # follow prompts
```

Verify: `gh api repos/sthe0/claude-agent-instructions --jq .full_name` → expect
`sthe0/claude-agent-instructions`.

### An org channel installed as a plugin

Its credential is the plugin's own concern — Core neither reads nor validates it. Follow the
overlay's instructions, and confirm the plugin resolves before relying on it (a channel name with
no plugin file fails loudly, naming the path it searched).

## Filing a difficulty manually

Use `scripts/file-difficulty.py` directly from any machine:

```bash
python3 ~/claude-agent-instructions/scripts/file-difficulty.py \
  --target CLAUDE.md \
  --ground 'gate wording ambiguous — non-author cannot tell when approval is required' \
  --severity medium \
  --evidence 'saw two conflicting rules in §Coordination and §Classify' \
  --cost '~5 min confusion per occurrence'
```

Filing requires exactly one of `--cost` (a rough per-occurrence or per-week estimate, in
whatever unit fits) or `--cost-not-estimable REASON` (an explicit reason no estimate is
possible) — a fixable loss must never go unmeasured, and a genuinely non-estimable one must
never be silently skipped.

Add `--dry-run` to print the record without submitting. Add `--channel <name>` to override the
machine default.

The command prints the channel-native handle on success: a GitHub issue URL, or whatever handle a
plugin channel returns (typically an issue key).

## Author-side digest

On a machine with push rights, pull and cluster all filed difficulties:

```bash
python3 ~/claude-agent-instructions/scripts/core-difficulty-digest.py \
  --channel github
```

Pass `--channel` once per channel to cluster across several at a time.

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
