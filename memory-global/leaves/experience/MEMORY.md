# Global resolved-task experience

Chronological log of leaves recording how non-trivial cross-project tasks were resolved — final plan as executed, difficulties encountered, artifacts, lessons, self-critique, cost. Quality bar and required sections: `~/.claude/CLAUDE.md` § On task resolution.

Sub-index of `memory-global/leaves/experience/`. Pointed at from `memory-global/MEMORY.md`. Not auto-loaded by the harness.

Most recent first.

## 2026-05

- [2026-05-29 — cursor mount migration: symlink-invoked script self-linked .claude (ELOOP)](2026-05-29-cursor-mount-migration-symlink-eloop.md) — `migrate-cursor-namespace.sh --all-deepagent-mounts` invoked `setup-local.sh` through the mount's `.claude` symlink; the script's logical-`pwd` STORAGE resolved back to `.claude`, step 1 relinked `.claude` onto itself (ELOOP), breaking a ticket worktree. Recovered with one `ln -sfn` (storage intact), switched to the narrow `link-project-cursor-agents.sh`, fixed the caller with `readlink -f`. Footgun: a script that locates itself via logical pwd must be invoked by its real path.
- [2026-05-29 — mosh + forwarded agent → persistent ssh master](2026-05-29-mosh-forwarded-agent-persistent-master.md) — mosh's spawning ssh dies right after launching mosh-server, killing the forwarded agent socket. Fixed by an in-thread `mosh()` zsh wrapper that brings up a persistent `ControlMaster` ssh first, plus a host-side fallback over `/tmp/ssh-*/agent.*`. Side-quests: macOS Keychain pattern for passphrase-free id_rsa; `compinit` before yc completion. Self-critique drove an in-thread carve-out for the Closing protocol and a meta-question trigger in `hook-resolution-reminder.py`.
- [2026-05-27 — Architectural sweep](2026-05-27-architectural-sweep.md) — session-long DEEPAGENT audit produced 5 architectural improvements (allow-list parity, project-memory trigger leaves, skill-first dispatch, memory hierarchy, systemic-pattern-scan discipline) converging on "capability exists, trigger does not fire" meta-pattern. 9 commits across 2 VCSs.
- [2026-05-26 — Soft-control hooks arc](2026-05-26-soft-control-hooks-arc.md) — frontmatter sentinel + CLAUDE.md token-trim + 3 soft-control hooks (self-critique / tracker / push reminders) + 1 rejected proposal (hard cap on memory); lessons on warn-vs-block trade-off and the instruction-surfaces-vs-content-stores distinction.
- [2026-05-26 — Plan ↔ verify loop](2026-05-26-plan-verify-loop.md) — recurring mismatch between stage `Expected result image:` and actual outcome; tightened the verify cycle in CLAUDE.md.
- [2026-05-25 — Code-driven enforcement arc](2026-05-25-code-driven-enforcement-arc.md) — nine-iteration build-out of `verify-*` scripts, hooks, structured permissions, spawn wrapper, cost log + three rule additions; lessons on process-as-code pacing, verify-script ROI, JSON-over-YAML for stdlib portability, missed-leaf-at-resolution as a recurring failure mode.
- [2026-05-24 — Coordination machinery refactor](2026-05-24-coordination-refactor.md) — added task-weight triage / CLARIFY / PLAN-READY / depth cap / two-turn self-improvement / `config.md` for constants; two rounds of silent-architectural-decision corrections; lessons on consequence-of-change being a change, "config" meaning a separate file, `rg`-sweep before commit.
