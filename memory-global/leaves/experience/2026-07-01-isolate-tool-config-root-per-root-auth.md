---
name: 2026-07-01-isolate-tool-config-root-per-root-auth
description: An agent-system that installs into ~/.claude collides with the user's personal Claude config of the same name — install clobbers, and personal settings leak into the system. The fix is not a merge but ISOLATION: run the system from its own config root via the CLI's own root-relocation env var (CLAUDE_CONFIG_DIR), sourced from a single-source-of-truth seam (CLAUDE_AGENT_HOME in scripts/lib/config-root.sh) that every setup script + launcher reuses; keep bare-claude=personal and claude-task/claude-agent=system via env-scoped injection. Key gotchas: CLAUDE_CONFIG_DIR relocates the ENTIRE root (CLAUDE.md, settings.json, .claude.json, projects/, sessions/) and AUTH IS PER-ROOT, so a fresh root starts unauthenticated; do NOT solve that with apiKeyHelper (an x-api-key auth LOSES the claude.ai subscription tier) — use a one-time 'CLAUDE_CONFIG_DIR=~/.claude-agent claude auth login' whose token lives encrypted in the OS keychain, copy no credential file. The default personal root's .claude.json is home-anchored ($HOME/.claude.json), not under ~/.claude.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [scripts/lib/config-root.sh, scripts/migrate-to-isolated.sh, scripts/sync-instructions-repo.sh]
created: 2026-07-01
last_verified: 2026-07-02
---

# Isolate a tool's config root instead of clobbering a same-named personal one; auth is per-root

## Difficulty
Installing a tool into a config dir that a same-named pre-existing personal config already owns: the install clobbers the personal config and the personal config's settings interfere with the tool. Naive merges entangle the two irreversibly.

## Order & criterion
1) empirically establish what the CLI's root-relocation env var actually relocates + where auth lives (per-root or shared); 2) introduce a single-source-of-truth root seam and parameterize every setup target through it; 3) launch the system under that root via env-scoped injection, add a plain system launcher, keep bare-claude personal; 4) migration for existing in-place installs (preview-default, backup, idempotent) + non-clobber hardening; 5) docs + end-to-end verification.

**Acceptance check:** Install into a tmp root leaves personal ~/.claude byte-identical (FC1); a session on the isolated root writes .claude.json there not in ~/.claude (FC2); launchers inject CLAUDE_CONFIG_DIR + define the system launcher (FC3); no residual install-target hardcode (FC4). All rc=0.

## Contexts

### 2026-07-01 — Isolating the agent-system config root from the user's personal ~/.claude
- Where it arose: claude-agent-instructions Core: scripts/lib/config-root.sh, setup-symlinks.sh, claude-launchers.sh, doctor.sh, migrate-to-isolated.sh
- Working plan: 5-stage substantive plan (B-E + a service Stage A for empirical auth discovery), engine-tracked TOML isolate-agent-config-root.toml; commits fbc0e07 (seam+parameterize), e6d2092 (run under CLAUDE_CONFIG_DIR + claude-agent launcher), 3696728 (migrate + doctor warn), 47d0c36 (docs).

### 2026-07-01 — Migration was discoverable only via doctor; make it AUTOMATIC at the pull seam (interactive) / loud-notify (headless)
- Where it arose: Core task sync-auto-migrate-isolated: scripts/sync-instructions-repo.sh, scripts/lib/config-root.sh, scripts/doctor.sh + tests. Follow-up to the isolation work above.
- **The gap:** the migration step 4 above (migrate-to-isolated.sh, preview-default) was correct but only *discoverable* by a user who happened to run `doctor.sh`. The normal way people take Core updates — `sync-instructions-repo.sh pull` (and its cron/systemd timer) — was a pure git reconcile that ran no migration and gave ZERO signal, so an existing in-place machine stayed on the old layout indefinitely. Lesson: when you ship a one-time migration, wire its TRIGGER into the path users already traverse (the sync/update seam), don't leave it as a manual side-command gated behind a diagnostic.
- **The durable design principle — gate a destructive auto-action on execution context, not just detection.** Auto-running the (idempotent, backed-up) migration is right in an INTERACTIVE terminal but wrong in cron/headless, where unattended file moves surprise. So `maybe_migrate_isolated` runs the migration only when `is_interactive` (`[[ -t 0 && -t 1 ]]`, with `CLAUDE_SYNC_FORCE_INTERACTIVE` / `CLAUDE_SYNC_NONINTERACTIVE` seams for tests/automation); in headless it emits ONLY a loud `ACTION NEEDED` log line with the exact command. Same detector for both — a single shared `agent_legacy_inplace_layout` helper factored into `config-root.sh` (the seam every script already sources), replacing the duplicated inline scan that had grown independently in `doctor.sh` and `migrate-to-isolated.sh`.
- **Portability gotcha (cost the only real difficulty cycle here): macOS bash 3.2 exits a `set -euo pipefail` shell on `source <missing-file>` EVEN WITH `|| true`.** `source "$REPO/scripts/lib/config-root.sh" 2>/dev/null || true` looked safe but terminated the whole script the moment the file was absent (surfaced when tests pointed `$REPO` at a bare clone with no `scripts/` tree — every test false-failed with empty stderr). `bash 3.2.57`'s `.`/`source` builtin, on a not-found file in a non-interactive shell, exits the shell before the `||` runs. Fix: guard with an explicit existence test — `if [[ -f "$lib" ]]; then source "$lib"; fi` — never rely on `|| true` to neutralize a missing-source on bash 3.2. (Linux bash 4/5 tolerates the `|| true`, so this only bites on macOS dev machines — exactly where tests run.)
- **Hermetic test seam:** the migrate/setup binaries are indirected through env vars (`CLAUDE_MIGRATE_BIN`, `SETUP_SYMLINKS_BIN`, mirroring onboard.sh's `SETUP_SYMLINKS_BIN`/`DOCTOR_BIN`) so tests substitute marker-touching stubs and never mutate the real HOME/repo; the test clone is seeded with a copy of config-root.sh so the sourced detector is actually defined. log() tees to STDOUT (not stderr) — assert on stdout (or combined).
- Verification: bash -n on all changed scripts; test_config_root.py (4 new detector tests) + test_sync_instructions_repo.py (3 new: interactive auto-migrate, headless notify-only, no-legacy no-op) green; verify-all.py 14/14.


### 2026-07-02 — isolated-install-audit: full-system sweep + remote arc layer
- Where it arose: claude-agent-instructions main c9dd276..e08a6b7; the0.klg.yp-c.yandex.net; arc junk/the0/agents PR 14223053 merged to trunk
- Working plan: Sweep every writer/reader to read-time root resolution (CLAUDE_CONFIG_DIR -> CLAUDE_AGENT_HOME -> ~/.claude-agent if present -> legacy ~/.claude) via one seam (lib/config-root.sh agent_home_read + lib/config_root.py); add a mechanical enumerator test (regex over all *.sh/*.py + currency-checked allowlist of 7 intentional legacy fallbacks) so new hardcodes fail CI; fix silently-broken CLAUDE.md @-imports still pointing at ~/.claude; reconcile the remote machine (re-run fixed setup-project-memory.sh per live mount, merge both-sides-content memory into the in-tree store before relinking, remove dangling legacy symlinks, migrate legacy realdirs); patch the arc layer at its CANONICAL trunk location (common/scripts + projects/), not the stale per-branch copy.

## Common core & variations
**Common:** The migration is only done when (a) every writer resolves the root at read time through the single seam, (b) a mechanical enumerator makes regressions impossible, and (c) every machine's live state (symlinks, legacy realdirs, staged copies) is reconciled — prose fixes without (b) and (c) rot silently.

**Variations:** Arc-layer trap: storage layout had moved on trunk (robot/deepagent/scripts -> common/scripts + projects/), so the first patch landed on a DEAD copy on a stale working branch, and stash-apply onto fresh trunk resurrected deleted files as whole-file conflicts — locate the canonical trunk path (find / arc show trunk:) before patching a long-lived mount. Remote memory trap: per-cwd auto-memory realdirs duplicated byte-identical personal leaves across cwd hashes; merge into the in-tree store once, back up realdirs, let the setup script relink. Delivery gates: the auto-mode classifier independently re-gates arc pr merge --now --force even when the user's AskUserQuestion option named self-ship (bundle the merge as its own click-gate option); text before AskUserQuestion in the same turn was dropped again — background-sleep timer split (recap as final message, buttons on wake) worked.

## Cost
6 spawns; ~$23 attributed; multi-session (spanned a compaction). Stage C/D spawns hit budget/sandbox limits so the manager verified + committed independently.
