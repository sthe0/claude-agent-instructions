---
name: 2026-06-04-org-specific-vs-global-placement
description: Difficulty — org-specific dispatch rules drafted into global (org-agnostic) instructions instead of project memory; plus the arc-tracked junk/ symlink topology that makes a project's memory read as "outside repository" from the wrong mount.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "Да, решено (Recommended)"
refs: [2026-05-26-agent-system-plan-vs-reality-drift]
created: 2026-06-11
last_verified: 2026-06-11
---

# Org-specific vs. global instruction placement

## Difficulty
Encoding a dispatch rule (prefer local/community skills over MCP for tracker/the orchestration platform; logos only from a `logos/` cwd), the first draft put org-specific names (`mcp__tracker__`, `community/`, `logos`) into the *global* org-agnostic instructions. The rule "skill maps belong in project memory" already existed in `skill-first-dispatch.md` but didn't fire until the user pushed back. Secondary: committing the project memory looked impossible — the VCS status command from the VCS monorepo mount said "outside repository".

## Order & criterion
Org-specific guidance → project memory; only the generic principle → global. **Reusable test:** would this rule make sense on a machine outside this org? If it names an org tool, it is project-scope. Commit that project's memory from the `arcadia_claude_local` mount (it rides the personal `junk/` branch, not a trunk PR). **Acceptance check:** global instructions contain no org names; the project-memory commit lands cleanly from the correct mount.

## Contexts

### 2026-06-04 — skill-over-MCP dispatch + logos cwd-gate
Split by scope: generic "skill over MCP" principle + tracker-management ladder reorder → global (commit `53df184`, pushed); an org-specific skill-dispatch memory leaf + logos-cwd-gate → project memory (a VCS commit `953880100d`); logos MCP scoped global→project in `~/.claude.json`. Root cause of the "outside repository" confusion: the project's `.claude` path under the VCS monorepo mount is a **symlink** into the personal-storage `junk/<user>/agents/projects/<org-path>/` tree's project directory; that `junk/` tree is VCS-tracked but only visible as such from the `arcadia_claude_local` mount (following the symlink from the other mount leaves its repo). Committed only the two intended files via explicit path-scoped staging, leaving the user's in-progress checkpoint files untouched.

## Cost
In-thread, no spawns, ~30 min. Cost driver: recon to locate the project-memory VCS topology (the symlink-into-`junk` discovery) — a few probes, far cheaper than a wrong commit. The miss itself was the recurring [[2026-05-26-agent-system-plan-vs-reality-drift]] shape (rule existed, attention didn't apply it) — no new machinery, just check the global-vs-project boundary *before* drafting.
