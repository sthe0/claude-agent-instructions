---
name: capability-before-offload
description: When you hold both the tools and the rights to carry out a step, do it — never hand the user a manual click as a substitute for a capability you have; verify a claimed "no CLI path" with a memory check and a --help check first.
type: feedback
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-02
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

## See also

- `~/.claude-agent/CLAUDE.md` § Acting without asking — the short pointer that loads this leaf.
- [[doubt-own-snapshot]] — the perception-side twin: doubt your own stale snapshot before doubting the user's requirement.
- [[acting-without-asking]] — the pre-authorization carve-outs that make "just do it" safe.
