---
name: 2026-07-01-isolate-tool-config-root-per-root-auth
description: An agent-system that installs into ~/.claude collides with the user's personal Claude config of the same name — install clobbers, and personal settings leak into the system. The fix is not a merge but ISOLATION: run the system from its own config root via the CLI's own root-relocation env var (CLAUDE_CONFIG_DIR), sourced from a single-source-of-truth seam (CLAUDE_AGENT_HOME in scripts/lib/config-root.sh) that every setup script + launcher reuses; keep bare-claude=personal and claude-task/claude-agent=system via env-scoped injection. Key gotchas: CLAUDE_CONFIG_DIR relocates the ENTIRE root (CLAUDE.md, settings.json, .claude.json, projects/, sessions/) and AUTH IS PER-ROOT, so a fresh root starts unauthenticated; do NOT solve that with apiKeyHelper (an x-api-key auth LOSES the claude.ai subscription tier) — use a one-time 'CLAUDE_CONFIG_DIR=~/.claude-agent claude auth login' whose token lives encrypted in the OS keychain, copy no credential file. The default personal root's .claude.json is home-anchored ($HOME/.claude.json), not under ~/.claude.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [scripts/lib/config-root.sh, scripts/migrate-to-isolated.sh]
created: 2026-07-01
last_verified: 2026-07-01
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

## Cost
6 spawns; ~$23 attributed; multi-session (spanned a compaction). Stage C/D spawns hit budget/sandbox limits so the manager verified + committed independently.
