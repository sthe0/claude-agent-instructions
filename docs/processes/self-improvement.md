# Self-improvement

> How a user's correction of agent behaviour becomes a durable change to the instructions or memory. The cognition and the concrete edits live in the self-improvement skill; this page describes when the process runs and how it flows.

The agent system is not static. When the user corrects how the agent works, that correction is captured and turned into a change to the system itself — so the same mistake is not repeated on a future task.

## What triggers it

The process fires whenever the user gives substantive feedback about agent behaviour: a correction or rejection of an action, a stated principle ("don't do that", "prefer X", "always Z"), an evaluation of agent quality, a proposal to change the instructions or workflow, or a reminder that the process should have run. An in-task correction — "you did only part of it", "wrong scope", "answer in my language" — counts as a trigger, not a mere task tweak. Neutral confirmation ("ok", "thanks") and pure questions do not. The full trigger list is in [the self-improvement skill](../../skills/self-improvement/SKILL.md).

## The two-beat flow

Self-improvement runs in the same dialog turn as the trigger, before the final reply, in two beats:

1. **Diagnosis** — work out what went wrong and what the system should have done; propose the concrete edit, and ask the user to approve it.
2. **Apply** — make the approved edit to the instructions, skill, or memory.

Editing the agent's own configuration is state-changing production work, so it rides the standard plan-approval spine like any other task; the skill's diagnosis beat is that approval gate. Only memory writes bypass it.

## Behavioural rule versus domain fact

Before recording a lesson, the process classifies it. A **behavioural rule** — an always/never, a process step, a delegation or verification habit — belongs in the instructions (CLAUDE.md or a skill). A **domain fact** — something true about a specific system or codebase — belongs in a memory leaf. A behavioural rule filed as a memory leaf is misplaced and will not be enforced. The mechanics of each destination are in [the self-improvement skill](../../skills/self-improvement/SKILL.md).

## See also

- [The self-improvement skill](../../skills/self-improvement/SKILL.md) — triggers, the two-beat flow, and the edit mechanics.
- [Resolution and experience](resolution-and-experience.md) — the sibling process that records a difficulty overcome rather than a behavioural correction.
- [Difficulty](../concepts/difficulty.md) — the divergence a correction removes.
