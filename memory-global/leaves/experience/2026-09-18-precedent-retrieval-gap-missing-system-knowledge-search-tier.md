---
name: 2026-09-18-precedent-retrieval-gap-missing-system-knowledge-search-tier
description: A system-knowledge leaf recorded from an earlier resolved ticket already stated the correct mechanism for a recurring integration task, but a later plan for an analogous task on the same platform re-derived a false premise contradicting that leaf, because record-experience.py search --tier only supported experience|principles — there was no retrieval path into system-knowledge/ at all. Fixed by extending the tool to a third tier (reusing the existing Difficulty-section ranking convention) and making planner/SKILL.md's Research step a mandatory precedent search, naming that a single past-task leaf is a principle at generality 0 and is retrieval-eligible on its own.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user, 2026-09-17, AskUserQuestion quality 5/5"
refs: [memory-global/leaves/leaf-schema.md, memory-global/leaves/principle-leaf-schema.md, skills/specializations/planner/SKILL.md, scripts/record-experience.py]
created: 2026-09-18
last_verified: 2026-09-18
---

# Existing system-knowledge precedent was never retrieved before planning — search tool only covered two of three tiers

## Difficulty
A recurring domain difficulty was already captured in a system-knowledge leaf (created well before this task), but the plan-authoring step had no mechanized way to retrieve it: record-experience.py search's --tier flag covered only experience and principles, and planner/SKILL.md's Research table pointed at system-knowledge only as a generic browse-if-time bullet, not a concrete mandatory command. The false premise the leaf already refuted shipped into an approved plan anyway and was only caught by a live REPLAN during execution — an avoidable correction cycle that cost a full plan-review + re-approval round.

## Order & criterion
User: extend proactive precedent retrieval to cover system-knowledge leaves, and document that a single resolved-task precedent counts as a principle at generality 0 for retrieval purposes, not only a leaf that crossed the promotion threshold into a formal principle/v1.

**Acceptance check:** record-experience.py search --tier system-knowledge surfaces a known leaf at both global and project scope (tested against a real project leaf); planner/SKILL.md's Research table names it as a mandatory step; both landed on main.

## Contexts

### 2026-09-18 — initial
- Where it arose: a blind-human-labeling task on a Toloka-like platform — a developer's live REPLAN, during execution of an approved stage, surfaced that the plan wrongly assumed a direct pool-performer link, contradicting a system-knowledge leaf on the same platform written from an earlier resolved ticket.
- Working plan: self-improvement Beat 1/Beat 2: (1) confirm the leaf genuinely predates this session; (2) locate the retrieval gap (record-experience.py --tier choices, planner/SKILL.md Research row); (3) extend TIER_SECTION/search_root to a system-knowledge tier reusing the Difficulty-section convention (not a bespoke whole-body ranking basis — corrected after user's own challenge to my first draft); (4) fix the leaf's own missing difficulty-lead blockquote so it satisfies leaf-schema.md's grandfather rule; (5) make planner/SKILL.md's Research row a mandatory retrieval step naming precedent-at-generality-0; (6) add scripts/tests/test_system_knowledge_search.py; commit + land via session-isolate.sh/land-on-main.sh.

## Cost
~1 self-improvement Beat1+Beat2 cycle in-thread; 4 file edits (record-experience.py, planner/SKILL.md, yang leaf, 1 new test file); 1 commit; land-on-main.sh landing; no specialist spawn.

## Self-critique of the agent system
<!-- Language exception: the parenthetical is a verbatim quote of the user's own correcting question, kept in its original language for provenance. -->
My first Beat-1 proposal invented a bespoke 'description + whole body' ranking basis for system-knowledge instead of reusing the existing Difficulty-section mechanism already required by leaf-schema.md for that same tier — an unjustified deviation the user's single question ('А почему системное знание не выстроено вокруг затруднения?') caught immediately. Lesson: when self-improvement's own 'extend an existing mechanism rather than inventing a new one' rule applies to the subject matter under discussion, check the proposal against that rule before presenting it, not after a correction.
