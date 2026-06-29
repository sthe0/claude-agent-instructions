---
name: 2026-05-26-agent-system-plan-vs-reality-drift
description: Recurring difficulty — the agent system's described plan (instructions, structure contracts, declared capabilities, plan stages) silently diverges from its actual behavior; the general fix is a mechanism that enumerates reality and asserts it against the description.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "<migrated 2026-06-11 to difficulty/v1; per-context confirmations preserved inline>"
refs: [2026-05-25-resolution-gate-confirm-before-record, 2026-06-04-verify-load-bearing-axis, systemic-pattern-scan]
---

# Agent-system plan vs. reality drift

## Difficulty
A rule, capability, structure contract, or plan stage exists in the agent system's *described* plan, but the system's *actual* behavior diverges from it and nothing surfaces the gap: a capability exists but its trigger never fires; an allowlist contract tolerates additions it never asserts; a CLAUDE.md rule keeps being skipped because it is prose-only; a stage's real outcome is never compared to its declared expected image. The divergence is structurally invisible until a human notices.

## Order & criterion
A mechanism that forces reality to assert against the description — **enumerate the real artifacts and assert each is described** (bidirectional contract), make the rule a firing hook/gate rather than recall-dependent prose, or run a per-stage verify loop. **Acceptance check:** a negative test proves the divergence now fails loudly — remove the real artifact / skip the step / drift the wording → the check fails *naming it*, or the hook nudges automatically.

## Contexts

### 2026-05-26 — plan-verify gate (stage outcome vs declared expected image)
Confirmed: "Да, резолвнута". Plans were ephemeral/in-conversation; stage outcomes weren't checked against a declared image. Fix: `verify-plan-file.py` enforces plan *shape* (4 sections + ≥1 `Expected result image:`), wired into `spawn-specialist.py` after `PLAN-READY:`; CLAUDE.md § Verification rewritten as two mandatory layers (per-stage against the image + final against the done criterion). Split: code enforces the deterministic shape; the cognitive per-stage gate stays prose (no deterministic "did this stage pass" check exists). Commit `cc00469`. The self-critique of this occurrence was itself the resolution-gate miss — see [[2026-05-25-resolution-gate-confirm-before-record]].

### 2026-05-26 — soft-control hooks (rule exists in CLAUDE.md, agent misses it)
Confirmed: "да". A rule lived in CLAUDE.md but was repeatedly skipped. Fix: three soft-control hooks lifting recall-prose to firing nudges — `hook-self-critique-reminder.py` (PostToolUse Write), `hook-tracker-reminder.py` (UserPromptSubmit), `hook-push-confirmation-reminder.py` (PreToolUse Bash) — plus the `resolution_confirmed_by_user` frontmatter sentinel (hard block). Commits `c3e897e`→`9eeef94`.

### 2026-05-27 — architectural sweep (naming the meta-pattern)
Confirmed: "Да, resolved". An audit of 9 transcripts surfaced ~5 sibling gaps (unused yandex-guru, 0 overcome-difficulty invocations, ~95 unused skills, no allowlist parity) that were all one shape: "capability exists, trigger does not fire." **Naming the meta-pattern** unlocked the architectural fix instead of 5 disconnected patches: project-memory trigger leaves with concrete signals, the skill-first-dispatch leaf, the memory-hierarchy sub-indexes (a precondition for cheap pattern-scanning), and the systemic-pattern-scan discipline as the proactive scanner. 9 commits across 2 VCSs.

### 2026-06-11 — structure contract tolerates additions silently
Confirmed: "Пуш + считаем решённым". `verify-layout-contract.sh` was a one-directional allowlist (asserted listed files exist / forbidden absent) but never asserted every real `hook-*.py` is *listed* — so 4 already-shipped hooks were invisible to it, present only in machine-local `settings.json`. Fix: made the check **bidirectional** (enumerate real `hook-*.py`, assert each is in contract + README); also shipped `coordinate-task.py` (coordination cycle as code with a real human `PLAN-READY:` gate). Commit `ed03fac`. Verification-axis trap caught in self-review: a first "gate test passed" was misleading — a missing argparse arg made the script err *before* the gate, and the `EXIT=0` read was `echo`'s code through a pipe (`${PIPESTATUS[0]}`); re-ran reaching the real path.

### 2026-06-24 — wired hook silently not executable (engine never armed)
Confirmed: "Да, решено". `hook-engine-start.py` was wired into `settings.json` and listed in the contract, but committed `100644` (no `+x`). The harness execs hooks via `/bin/sh`, so it failed every turn with "Permission denied" — the engine-start steer never appeared and `agentctl` was never started. A *runnability* divergence, distinct from the 2026-06-11 *wiring* gap: the hook was listed and wired, just inert. Fix (enumerate-and-assert again): `lint-hooks-executable.py` fails the pre-commit gate if any `hook-*.py` lacks `+x` on disk **or** in git's recorded mode, plus `setup-symlinks.sh` now chmods the whole `hook-*.py` family. Same fix bundled the engine-by-default upgrade: the hook went from nudge-only to best-effort hard auto-start (`agentctl start --if-absent`, error-swallowed, never auto-classify), so "engine runs by default" is enforced by the hook, not by the model remembering. Commit `6103b66`; live self-test confirmed a no-state prompt yields `node=CLASSIFIED`.


### 2026-06-29 — build σ-sentinel — plan assumptions unverified against repo wiring
- Where it arose: build σ-sentinel (ADR-0002, 5-stage TOML plan). Two unverified plan assumptions surfaced only at execution: (1) FORWARD-REFERENCE ORDERING — S1's doc and S2's schema-leaf linked scripts/sigma-sentinel.py, created only in S3; verify-cross-refs scans the FULL working tree, so the links read as broken refs and blocked S2's commit/verify before S3 ran. (2) WRONG REGISTRATION FILE — S4 targeted settings/base.json for the SessionStart hook, but base.json holds no hooks (machine-specific, not merged); the real registration site is scripts/install-reminder-hooks.sh's DESIRED list. Both = same drift: a stage asserted repo wiring never checked against the repo.
- Working plan: (1) Ordering rule: an output artifact must be produced no LATER than the first stage whose deliverables reference it. Resolved via difficulty cycle → no_change replan, pulling sigma-sentinel.py's creation into S2's window so it exists before any reference. (2) Before a stage targets a config/wiring file, grep the repo for how the existing peer (hook-policy-scorecard-due.py) registers, target that file, correct the canonical plan TOML, re-drive the engine. General prevention (this leaf's core fix): at plan-DESIGN time enumerate the plan's cross-references and target-file assumptions and assert them against the actual repo (verify-cross-refs full mode; grep the real registration site) BEFORE approval, not at execution.
## Common core & variations
**Common:** every fix is the same shape — *enumerate reality and assert it against the description*, converting silent drift into a hard failure or an automatically-firing nudge. Naming the meta-pattern (2026-05-27) is what lets one architectural mechanism replace N disconnected patches.

**Variations:**
- *Warn vs hard-block* by action frequency/cost: hard block for high-frequency local mistakes (frontmatter sentinel); warn-only for rare high-cost external actions (push) where a false block is worse than a false pass.
- *Code vs prose boundary:* enforce deterministic shape in code; leave genuinely cognitive gates (per-stage "did this really pass") in prose.
- *Contract bidirectionality* (2026-06-11) vs *firing hook* (2026-05-26) vs *proactive scan discipline* (2026-05-27) — three surfaces of the same enumerate-and-assert principle.
- *Silent-inert modes are plural:* a hook can be unlisted (2026-06-11 wiring gap) **or** listed-but-non-executable (2026-06-24 `+x` gap) — each needs its own enumerate-and-assert check (`verify-layout-contract.sh` for listing, `lint-hooks-executable.py` for runnability). Residual gap still open: the bidirectional check covers contract + README but not `install-reminder-hooks.sh` (canonical wiring), so a hook documented-but-unwired still slips to a fresh machine.
- *Plan-design-time drift, not just system-time* (2026-06-29): the description that drifts can be a **task plan** written ahead of execution, not only the standing system. Two sub-shapes — (a) **forward-reference ordering**: a stage's deliverable references an artifact a *later* stage creates, and a full-tree guard (`verify-cross-refs`) reads it as broken before the producer runs → rule: *produce an artifact no later than the first stage whose deliverables reference it*; (b) **wrong target file**: a stage targets an assumed config site (`settings/base.json`) instead of the real one (`install-reminder-hooks.sh`) — this is the residual gap above firing at plan-design time. Both prevented by asserting the plan's cross-refs + target-file assumptions against the actual repo (full-mode `verify-cross-refs`; grep the peer's real registration site) **before** approval.

## Cost
- All four occurrences in-thread (small-change carve-out) except 2026-06-11, which spawned one `developer` (opus, large tier, ~7 min, all edits in-scope).
- 2026-05-27 was the heaviest: ~5 h wall-clock, ~$4–6, 9 commits — driven by parsing 9 transcript `jsonl` files (largest 6.4 MB) and repeated CLAUDE.md re-reads at the 400-line cap.
- Recurring secondary friction across 2026-05-27 / and the verify-axis sub-trap: `cost-report.py --since` / `tool-usage-report.py --since` reject human date strings (require ISO), making the cost section harder to fill mechanically.
