---
name: capability-before-offload
description: When you hold both the tools and the rights to carry out a step, do it — never hand the user a manual click as a substitute for a capability you have; verify a claimed "no CLI path" with a memory check and a --help check first.
type: feedback
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-28
---

# Don't offload to the user an action you can perform yourself

The short rule lives in CLAUDE.md § Acting without asking; this leaf carries the full narrative and the two-check requirement.

## Difficulty

Repeatedly stalling on a doable land / merge / ship and pushing manual work onto the user — who then has to notice, context-switch, and click — when memory already documents the exact command and the tool already exposes it (the recurring *"you have the instructions, tools and rights to do this yourself"* correction). This is the acting-side twin of [[doubt-own-snapshot]]: doubt your own claim of *"can't"*, not the user's expectation that you can.

## Guidance

When you hold the tools *and* the rights to carry out a step the user asked for (a merge, a ship/approve, a config change, a lookup), **do it** — never hand the user a manual click or UI action as a substitute for a capability you have.

Before concluding a domain operation *"has no CLI path"* and sending the user into a web UI, you **must** first:

- **(a)** consult project / global memory for that operation, and
- **(b)** check `<tool> <subcommand> --help` for the subcommand.

A capability gap asserted without both checks is unverified.

### Decision-offload, not only execution-offload

*To achieve autonomous within-authority gate resolution, resolve engine gates yourself instead of escalating their mechanics.* The rule covers offloaded **decisions**, not only offloaded **execution**. When an engine gate (`agentctl code-review` / `stage-review`, or any blocked directive) stops your action and you hold a within-authority resolution — an `override` verdict carrying a **named escape-class**, a coordinator `stage-review --verdict pass` — **decide and record it yourself**. Escalating *which gate-clearing path to take* offloads a decision that is exactly the coordinator cognition the engine defers to; it matches none of the CLAUDE.md § Escalation triggers (the criterion is defined, access is held, there is no risk-bearing strategy fork). And never let *"the alternative path costs a spawn"* justify the ask — on a flat plan a spawn is telemetry, not money ([[flat-max-billing-cost-framing]]). The structural end-state is the engine annotating a blocked gate as coordinator-resolvable; until then the naming here carries the salience.

## See also

- `~/.claude-agent/CLAUDE.md` § Acting without asking — the short pointer that loads this leaf.
- [[doubt-own-snapshot]] — the perception-side twin: doubt your own stale snapshot before doubting the user's requirement.
- [[acting-without-asking]] — the pre-authorization carve-outs that make "just do it" safe.
- [[flat-max-billing-cost-framing]] — the cost-framing twin: a spawn's dollar figure is telemetry on a flat plan, never a reason to escalate.
