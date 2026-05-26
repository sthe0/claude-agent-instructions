---
name: feedback-avoid-premature-optimization
description: User principle — do not add machinery (validators, sub-fields, complex abstractions) until a concrete difficulty surfaces that it would solve. Applies to every design decision in the agent system.
type: feedback
---

# Avoid premature optimization

**Rule.** When proposing a design (new field, validator, abstraction, hook, sub-folder, etc.), do not include machinery whose only justification is "we *might* want it later". Add it only when a concrete difficulty has surfaced that the machinery would resolve.

**Why:** Stated by the user on 2026-05-26 while accepting the `system-knowledge/` proposal: "Это вообще хороший принцип — не делать преждевременных оптимизаций, пока не столкнулись с затруднением, которое они снимут." The remark generalises beyond that one decision: every speculative `subject:` field, every "just in case" verifier, every "what if we extend it" abstraction is technical debt now in service of an imagined future. The token / maintenance cost is paid every session; the imagined benefit is rarely collected.

**How to apply:**

- When drafting a Turn-1 self-improvement proposal, mark each component as either *solves a concrete current difficulty* or *speculative*. Drop the speculative items unless the user explicitly asks for them.
- For new memory fields (`subject:`, `last_updated:`, `severity:`): start without them. Add when a real lookup or audit problem appears.
- For new verifiers / hooks: same. The "we should validate X" instinct needs a paired concrete failure case before the script is written.
- For new abstractions (helper script, shared module, generic interface): require ≥ 2 real consumers before extracting. One-off duplication is cheaper than premature abstraction.
- The principle composes with `policy.md § Process as code`: process-as-code applies when a deterministic rule already exists in prose and is being violated. It does **not** mandate writing code for every rule "in case it's violated later".

**Counter-cases (where machinery IS justified up front):**

- The cost of a single failure is high and unrecoverable (data loss, prod incident, missed user confirmation on push).
- The failure mode is *already observed* (today's CRON_TZ bug, today's H2 conflation).
- The mechanism is a thin wrapper around something already needed (e.g. `verify-experience-leaf.py` was justified by a recurring failure, not a hypothetical one).
