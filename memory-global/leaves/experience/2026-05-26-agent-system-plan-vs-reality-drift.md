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

## Common core & variations
**Common:** every fix is the same shape — *enumerate reality and assert it against the description*, converting silent drift into a hard failure or an automatically-firing nudge. Naming the meta-pattern (2026-05-27) is what lets one architectural mechanism replace N disconnected patches.

**Variations:**
- *Warn vs hard-block* by action frequency/cost: hard block for high-frequency local mistakes (frontmatter sentinel); warn-only for rare high-cost external actions (push) where a false block is worse than a false pass.
- *Code vs prose boundary:* enforce deterministic shape in code; leave genuinely cognitive gates (per-stage "did this really pass") in prose.
- *Contract bidirectionality* (2026-06-11) vs *firing hook* (2026-05-26) vs *proactive scan discipline* (2026-05-27) — three surfaces of the same enumerate-and-assert principle.
- Residual gap noted but not fixed at 2026-06-11: the bidirectional check covers contract + README but not `install-reminder-hooks.sh` (canonical wiring), so a hook documented-but-unwired still slips to a fresh machine.

## Cost
- All four occurrences in-thread (small-change carve-out) except 2026-06-11, which spawned one `developer` (opus, large tier, ~7 min, all edits in-scope).
- 2026-05-27 was the heaviest: ~5 h wall-clock, ~$4–6, 9 commits — driven by parsing 9 transcript `jsonl` files (largest 6.4 MB) and repeated CLAUDE.md re-reads at the 400-line cap.
- Recurring secondary friction across 2026-05-27 / and the verify-axis sub-trap: `cost-report.py --since` / `tool-usage-report.py --since` reject human date strings (require ISO), making the cost section harder to fill mechanically.
