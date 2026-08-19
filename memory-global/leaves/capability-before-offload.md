---
name: capability-before-offload
description: When you hold both the tools and the rights to carry out a step, do it — never hand the user a manual click, a decision, a deferral, or a knowledge question as a substitute for a capability you have; verify a claimed "no CLI path" with a memory check and a --help check first.
type: feedback
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-08-19
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

### Question-offload: a question you could have answered is not an escalation

*To achieve escalations that spend the user's attention only on what is genuinely theirs, exhaust the channels you hold before the question reaches them.* The fourth offload axis, and the one the other three do not cover: the offloaded thing is neither an action, a decision, nor a deferral, but the **work of finding out**. A **knowledge** question — what a system does, how an access is modelled, which flag exists — is closable by channels you already hold: memory, the repo, docs/web search, an MCP, and, when the project defines one, a **domain-expert subagent** (a project-local `.claude/agents/*.md`). A subagent listed in the session is a held capability exactly like a CLI, so putting its question to the user instead is this rule's plain violation. Separate the kinds before escalating: a **decision, permission or preference** is the user's to give and carries no research precondition — asking it directly is correct, and gating it behind research would be its own waste.

The engine codes the discipline for one population only: `question-dispose --to escalated` refuses on empty `own_research` (authority `premise.validate_questions`), binding questions recorded in the `premise` bag at the `plan_approval` gate. Questions arising **mid-execution or at plan review** are outside that binding, so the same three acts — `question-raise` → `question-research` → `question-dispose` — are yours to run there. A project rule naming a specific expert ("ask the domain guru before the user") is an instance of this axis, not a separate norm.

### Deferral-offload: a queue entry is not a resolution

*To achieve removal of a difficulty at its cheapest moment, fix a localized defect when you find it instead of filing it.* The third offload axis: the recipient is neither the user nor a gate but a **future session**. Filing does not remove a difficulty — it moves it into a queue and adds the cost of rediscovering it. The moment of discovery is the cheapest moment to fix: localization is done, the files are read, the tree is live, the suite is warm. So when a defect is found outside the approved plan's scope, compare **fix-now** against **file + rediscover + re-localize + fix-later**; when fix-now is cheaper and within the session's capability, it is the **first and recommended** option, and filing is for what is genuinely not now-doable (needs another owner, needs a decision you do not hold, or is substantive enough to need its own plan). **Never present a disposition whose every branch defers.** "Out of the approved plan's scope" bounds what you may do *without asking* — it is not a claim about feasibility, and reading it as one is what produces a file-only option set.

## See also

- `~/.claude-agent/CLAUDE.md` § Acting without asking — the short pointer that loads this leaf.
- [[doubt-own-snapshot]] — the perception-side twin: doubt your own stale snapshot before doubting the user's requirement.
- [[acting-without-asking]] — the pre-authorization carve-outs that make "just do it" safe.
- [[flat-max-billing-cost-framing]] — the cost-framing twin: a spawn's dollar figure is telemetry on a flat plan, never a reason to escalate.
