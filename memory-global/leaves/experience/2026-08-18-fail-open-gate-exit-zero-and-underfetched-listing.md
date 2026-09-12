---
name: 2026-08-18-fail-open-gate-exit-zero-and-underfetched-listing
description: Two ways a check reported success while establishing nothing, met in one task: check-org-neutral.py exits 0 both when the bodies are clean AND when no term ruleset is installed, so the caller cannot distinguish a passed gate from an unrun one (it printed 'clean: no term ruleset installed (0 rulesets discovered)' for all 18 gated files and the gate never ran); and a shell-loop pagination over the GitHub issues list returned 47 of 61 open issues with no error, while the same endpoint silently mixes pull requests into the issue list unless the caller filters 'pull_request'. Remedy for the first: treat the fail-open message as UNCHECKED, not as a pass, and substitute a real check — here a denylist grep built from the org-internal vocabulary the machine actually carries (agent-identity.local named skotty and an internal tracker queue), zero hits. Remedy for the second: paginate in code with an explicit len(batch)<per_page terminator and count the result against an independently-known total before building any conclusion on it.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user, 2026-08-18, AskUserQuestion: accepted R6 and directed the registry decision be filed as a task before resolution"
refs: [https://github.com/sthe0/claude-agent-instructions/issues/101, https://github.com/sthe0/claude-agent-instructions/issues/102, https://github.com/sthe0/claude-agent-instructions/issues/103]
created: 2026-08-18
last_verified: 2026-08-18
---

# A fail-open gate's exit 0 and a silently under-fetched listing both report success while proving nothing

## Difficulty
A verification step returned an exit code that its caller read as 'the property holds', when in fact the step had not examined the property at all — once because the checker fail-opens on a missing ruleset with exit 0 and a message nobody parses, once because a paginated fetch stopped early and returned a subset of the domain with no error. Both failures are silent by construction: the success path and the did-not-run path are the same observable.

## Order & criterion
Move every deferred item the fleet carried in volatile substrates — a session transcript, an uncommitted local queue file, fifteen unlanded remote branches — into backlog tasks in the Core issue tracker, without creating duplicates and without publishing any org-internal term to a public venue.

**Acceptance check:** measurable: five stage controls re-run at the final venue, plus two re-query scripts that rebuild the domain from its authoritative source (the filing map; git branch -r --no-merged) and check each element against the live tracker; each script demonstrated RED against a mutated world.

## Contexts

### 2026-08-18 — initial
- Where it arose: Bulk-filing 12 issues and 5 evidence comments to a public GitHub venue, plus a 15-branch triage, from a session driven by agentctl.
- Working plan: /Users/the0/.claude-agent/plans/backlog-capture-and-branch-triage.toml

## Cost
5 stages, 0 spawns, 4 acceptance-judge revise rounds (one of them a genuine arithmetic error of mine in the map's row count)

## Self-critique of the agent system
I reported the neutrality gate as run before reading its own message, and built two paginated conclusions on a listing I had not counted against an independent total. Both are the same lapse: accepting a tool's exit status as evidence about the world instead of asking what the tool actually examined.
