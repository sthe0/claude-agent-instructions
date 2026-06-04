---
name: 2026-06-04-skill-over-mcp-dispatch
description: Encoded "prefer skill over MCP" (generic, global) + Yandex-specific community-skill/tracker/nirvana/logos-cwd-gate (project memory); logos MCP scoped out of global config. Lessons on the global-vs-project split for org-specifics and the arc-tracked junk/ symlink topology of project memory.
type: reference
resolution_confirmed_by_user: "Да, решено (Recommended)"
plan_file: none (in-thread self-improvement, AskUserQuestion-scoped)
---

# 2026-06-04 — prefer skills over MCP; logos cwd-gate

User wanted the instructions to encode: (1) local tools + community skills (`ai/artifacts/skills/**`) preferred over MCP tools, specifically for tracker and nirvana; (2) logos agents/tools/skills used only when launched from a `logos/` folder.

## Final plan as executed

Recon (skill-first-dispatch leaf, tracker-management ladder, community skill dirs, MCP scope) → `AskUserQuestion` (apply + logos-MCP-scoping) → split edits by scope → verify → commit/push global, arc-commit project memory, machine-local config. The user added a key constraint mid-flight: **community skills are Yandex-specific, so do not name them in global instructions** — keep global generic, move specifics to project memory.

- **Global (generic, no org names):** tracker-management "How to publish" ladder reordered (local/project tracker skill #1, MCP demoted to #3 read-fallback); `skill-first-dispatch.md` gained a generic "Skill over MCP" principle; CLAUDE.md line extended in place. Commit `53df184`, pushed.
- **Project memory (Yandex-specific):** `skill-dispatch-yandex.md` + `MEMORY.md` ## Logos — community-skill>MCP for tracker (`tracker`/`startrek-client`) & nirvana, intrasearch/wiki MCP as fallback, "Logos only from logos/ cwd" section. arc commit `953880100d` on branch `the0/deepagent-nirvana-cache-leaf` (not pushed).
- **Machine-local:** logos MCP moved global→project-scope in `~/.claude.json` (loads only under `arcadia/logos`, `arcadia_claude_local/logos`); backup `~/.claude.json.bak.logos-scope`.

## Difficulties

1. **Where do org-specific dispatch rules belong?** First draft put community-skill names in the global leaf + CLAUDE.md. User corrected: global instructions are org-agnostic (CLAUDE.md line 5). Re-split: generic principle global, named mappings to project memory.
2. **"Commit the project memory to arc" looked impossible at first.** `arc status .claude/agent-memory` from the arcadia mount said *"outside repository"*, and `git` found nothing. Root cause: `<arcadia>/robot/deepagent/.claude` is a **symlink** into `arcadia_claude_local/junk/the0/agents/robot/deepagent`. Arcadia's `junk/` **is** arc-tracked in trunk — but only visible as such from the `arcadia_claude_local` mount (following the symlink from the other mount leaves its repo). From the right mount, the files showed as normal arc modifications.
3. **Unrelated uncommitted files in the same dir.** The memory dir also had the user's in-progress `deepagent-415-current-state.md` and `session-checkpoint.md` modified. Committed only my two files via explicit `arc add`, leaving theirs untouched.

## Artifacts

- Global: commit `53df184` (pushed) — tracker-management, skill-first-dispatch, CLAUDE.md, token-economy item 18.
- Project memory: arc commit `953880100d` (branch `the0/deepagent-nirvana-cache-leaf`, 2 ahead, unpushed) — `skill-dispatch-yandex.md`, `MEMORY.md`.
- `~/.claude.json` logos scoping (backup `.bak.logos-scope`).

## Lessons

- **Org-specific guidance → project memory; only the generic principle → global instructions.** The reusable test: would this rule make sense on a non-Yandex machine? If it names `mcp__tracker__`, `community/`, `logos`, it's project-scope.
- **Project memory for the deepagent mounts lives in arc-tracked `arcadia_claude_local/junk/the0/agents/robot/deepagent/agent-memory/`, reached via the `.claude` symlink.** To commit it, operate from the `arcadia_claude_local` mount (not the arcadia mount, where it reads as "outside repository"); it rides the personal `junk/` branch, not a trunk PR.
- **MCP scoping:** `~/.claude.json` `projects.<cwd>.mcpServers` scopes a server to a cwd; removing from the top-level `mcpServers` stops global loading. Effect is next-session; keep a backup.
- Config-only / memory-only changes take effect next session — say so explicitly and give a verification step (here: check `/mcp` shows logos when next in a logos cwd).

## Self-critique of the agent system

`skill-first-dispatch.md` (global) already said "skill maps belong in project memory," yet I initially drafted Yandex names into the global layer anyway — the rule existed but I didn't apply it until the user pushed back. This is the recurring "capability/rule exists, trigger didn't fire" shape (see [[systemic-pattern-scan]]). Not new machinery — the rule is already written; the miss was attention. Noting it so a future org-specific edit checks the global-vs-project boundary *before* drafting. The CLAUDE.md 400-cap held this time (extended a line in place, no reclaim needed) — the earlier cap friction did not recur.

## Cost, effort, and tool usage

- In-thread, no spawns. Wall-clock ~30 min; 2 user interactions (scope+logos question; push/commit/resolution bundle).
- Tools: `Bash` (recon, arc, .claude.json edit), `Edit` (3 global + 3 project files), `Read`, `AskUserQuestion` ×2, `TaskCreate/Update` (tasks 7-10).
- Cost driver: recon to locate the project-memory VCS topology (the symlink-into-junk discovery) — a few probes, but cheaper than a wrong commit.
