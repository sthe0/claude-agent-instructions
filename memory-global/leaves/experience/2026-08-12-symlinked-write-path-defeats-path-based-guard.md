---
name: 2026-08-12-symlinked-write-path-defeats-path-based-guard
description: A guard that keeps sessions out of a canonical checkout by comparing the literal path it is handed exempts a directory that is itself a symlink INTO that checkout — so every 'personal auto-memory' write physically landed in the copy only pull may touch, and an ignore rule hid the result from status. Fix: dereference before deciding, and move the editable state into a second permanent checkout behind an env seam with a fallback, protecting that new checkout in every site that classifies checkouts by NAME.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (2026-08-12, resolution ask: «Решена, оценка 4», quality 4/5)"
tier: 1
refs: [two review requests in the internal monorepo, both landed to trunk (ids in project memory)]
created: 2026-08-12
last_verified: 2026-08-12
---

# A path-based read-only guard is defeated by a symlink: judge the RESOLVED path, and give editable state its own writable checkout

## Difficulty
The rule 'the canonical checkout is read-only to sessions; only pull changes it' was mechanized as a hook comparing the path of each Edit/Write against the canon roots, with an exemption for the agent's own personal auto-memory under the config root. But the project's memory directory under that config root is a SYMLINK into the canonical checkout. The guard read the exempt-looking source path, allowed the write, and the bytes landed in canon; an ignore rule then kept the result out of the VCS status output, so the violation was invisible from both ends — the guard reported compliance and the VCS reported a clean tree. The visible symptom was different and milder: durable facts piled up in a session journal marked 'waiting, route busy', because the only honest way to obey the rule was to not write memory at all. Two earlier attempts to fix it by widening the guard's exemption list were themselves denied by the permission layer as edits to the agent's own guard — correctly, and that denial is the signal that the exemption list was the wrong lever.

## Order & criterion
Make it structurally impossible for a session to write into a canonical checkout, while making project-memory writes ordinary again, and carry the parked facts through the new route.

**Acceptance check:** measurable: the guard returns allow for the composed memory path and deny for the same file addressed inside canon (both directions probed); a mechanical enumeration shows every composed memory dir resolves OUT of canon while every review-gated composed entry stays IN it; the parked facts are readable through the path a session actually reads AFTER the landing was merged and both checkouts pulled

## Contexts

### 2026-08-12 — initial
- Where it arose: this machine's two permanent mounts of an internal monorepo — the anchor mount (canon, refreshed only by pull) plus a new config-store mount; `scripts/hook-guard-canon-readonly.py`; and, in the shared agent-config tree carried by that monorepo, the five scripts that classify mounts by name (per-project compose, mount GC, mount teardown, the resolution-time landing hook, the symlink checker) plus that VCS's workspace backend under `project-entry/backends/`
- Working plan: ~/.claude-agent/plans/project-memory-write-route.toml — 4 stages: (1) create the writable store checkout on trunk and probe that a spawn can write to it; (2) re-point memory composition at it behind ARCADIA_STORE_NAME with an anchor fallback, protect the store in all five checkout-classifying sites enumerated mechanically, prove BOTH branches (store present / store absent) by test; (3) land, pull both checkouts, re-compose every project from the ANCHOR's own copy of the setup script, prove the guard flipped in both directions; (4) carry the three parked facts through the new route and read them back from the consumer end.

## Cost
4 spawns, $30.8 list-price (flat subscription: telemetry, not money); ~50 min of final verification; 3 independent review rounds on stage 2

## Self-critique of the agent system
Three misses worth naming. (1) The first round-3 review spawn was launched from the canonical instructions repo, so it would have reviewed the wrong tree — a spawn inherits the launching shell's cwd as its workspace, so cd BEFORE spawning is part of the review contract, not hygiene. (2) The plan pinned literal prose phrases as its checks and two of them broke for reasons unrelated to the facts (one target leaf is English and the phrase was Russian; the other sentence carried emphasis markers inside the pinned substring). (3) Worse, one pinned phrase asserted a decision that reality had already closed, so the approved check MANDATED writing a stale claim into memory; caught only by checking the ticket at the resolution gate. Recorded as a new vector on the check-authoring leaf.
