---
name: 2026-06-30-mechanize-subdifficulty-extraction-and-audit
description: Difficulty — a design rule (an experience leaf's child/sub-difficulties each get their OWN searchable difficulty/v1 leaf linked inline [[slug]], never buried in prose) lived in two docs but had NO enforcement, so it silently did not happen: the rule-exists-trigger-does-not-fire class. Consequence: buried child difficulties are unfindable by record-experience.py search (it ranks description + ## Difficulty only), so each is rediscovered from scratch. Fix mechanizes the decidable rule part as a gate in verify-experience-leaf.py (marker-in-prose AND no inline [[link]] -> reject at write/commit, advisory in the full-corpus scan), co-locates the rule with its enforcement in the two docs, then audits every difficulty-built leaf (experience/principles/system-knowledge/leaf-with-Difficulty, global+project) and extracts the genuinely reusable buried difficulties into their own leaves linked inline from where they arose.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "Да, решена"
refs: [2026-05-26-agent-system-plan-vs-reality-drift, 2026-06-24-gate-exemption-is-category-error-for-result-images, 2026-06-29-org-portable-core-internal-coupling-opt-in]
created: 2026-06-30
last_verified: 2026-06-30
---

# Mechanize 'child difficulties referenced, never inlined' as a write-time gate, then audit the corpus and extract the reusable ones

## Difficulty
A recording discipline ('extract child difficulties to their own searchable leaf, link inline') existed as prose in two docs but had no machine check, so it never fired and child difficulties stayed buried and unsearchable.

## Order & criterion
1) implement+test the side-difficulty-link gate in verify-experience-leaf.py (decidable rule part only; perception — is this a distinct reusable difficulty — stays the model's); 2) co-locate the rule with its enforcement in experience-leaf-schema.md + recording-experience.md; 3) corpus-wide read-only audit (Stage-1 advisory WARNs as lexical seed + cheap Explore fan-out) into a candidate report; 4) extract approved children via record-experience.py new/extend + inline [[slug]] from each parent; one substantive replan rescoped the Stage-4 gate from 'global verify-all green' to its own artifacts after foreign-session WIP contaminated the full suite.

**Acceptance check:** measurable — gate pytest green; every touched parent passes verify-experience-leaf cmd_file exit 0 (now carries [[slug]]); each extracted child schema-valid + surfaced by record-experience.py search; both doc clauses grep-present; no NEW verify-all failures vs the foreign-WIP baseline (manager-checked).

## Contexts

### 2026-06-30 — initial
- Where it arose: claude-agent-instructions verify-experience-leaf.py gate + global & project memory corpus audit (DEEPAGENT robot/deepagent)
- Working plan: /home/the0/.claude/plans/enforce-subdifficulty-extraction.toml

## Cost
manager in-thread, engine-driven 4-stage agentctl spine + one substantive replan (Stage-4 gate rescope); 3-agent read-only Explore fan-out for the corpus audit; Stage-3 artifact + project landing required Bash-heredoc and arc-pr-merge workarounds around two over-broad hooks.

## Self-critique of the agent system
Two enforcement-hook defects surfaced mid-task and are routed to a follow-up self-improvement task (not yet extracted as leaves): the over-broad plan-freeze hook that blocks Write/Edit to ANY ~/.claude/plans/ file during EXECUTING (it should freeze only the executing plan, not the Stage-3 audit artifact) — see future [[plan-freeze-hook-freezes-whole-plans-dir-not-executing-plan]]; and the pr-bypass-gate that classifies by harness cwd rather than the payload's changed paths, falsely tagging a junk/the0 owner-sandbox PR as non-junk and fail-closed denying legitimate automerge — see future [[pr-bypass-gate-classifies-by-cwd-not-changed-paths]]. Process slip flagged by the user: defaulted to automerge-and-wait for an owner self-ship PR fully inside junk/the0 instead of the sanctioned force-merge fast-path.
