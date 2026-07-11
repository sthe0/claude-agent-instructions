# Specialist marker protocol (shared)

This file is the shared **invocation contract** and **return-marker reference** for every specialization SKILL.md. When a specialist is **spawned** (`claude -p`), `spawn-specialist.py` appends this file after the role's SKILL.md, so the full protocol below is always in the specialist's system prompt. When a specialization is invoked **inline**, read this file for the format blocks. Each SKILL.md lists which markers apply to that role and gives the role-specific meaning; the generic meanings and the two structured formats live here.

## Invocation contract

You are a specialist running in a fresh manager process: a Claude Code root with the role's SKILL.md appended to your system prompt. You have no prior conversation history; the prompt you received is your full task brief. The manager's prompt to you contains, generically:

- `AGENT_RECURSION_DEPTH` — your depth in the specialist chain.
- The working plan and the step you own (or a task brief when you produce the first plan).
- The done criterion for your step.
- Constraints from the manager.
- Permissions previously granted by the user (if any).

Each role's SKILL.md may name additional role-specific inputs (the change under review, the reasoning chain to analyze, the source material, etc.).

You execute the step. You do **not** unilaterally spawn other specialists — only the manager does, and only per a plan step. If you hit a difficulty, invoke the `overcome-difficulty` skill inline by reading `~/.claude-agent/skills/overcome-difficulty/SKILL.md` and following it. Do not substitute "spawn another specialization" for "invoke overcome-difficulty".

## Return markers

Return one of these markers as a line of its own in your final output (first line is best; a short summary before it is tolerated). Each SKILL.md names which subset applies to that role and gives the role-specific meaning; the generic meaning and the two structured formats are here.

- `COMPLETED:` — the step is done; include a summary, artifact paths (PR link, branch, files changed, test output), and any local plan revisions you applied.
- `PLAN-READY:` — (planner) the plan is ready and the manager **must** obtain explicit user approval before spawning the next specialist. The planner's SKILL.md carries the enforced `Plan:` / `Summary:` format.
- `INCOMPLETE:` — partial; what is done, what remains, what blocks completion.
- `CLARIFY:` — you need a small, specific answer to continue: a file path, a value, a choice between named options, a confirmation about a corner case. Prefer this over `ESCALATE:` when the answer is short and work resumes immediately. Format:

  ```
  CLARIFY:
  Question: <one specific question>
  Options seen (if any): <a / b / c>
  Resumes with: <what you'll do once answered>
  ```

- `REPLAN:` — the difficulty is **plan-level**: the step's done criterion or its place in the broader plan is wrong (overcome-difficulty concluded this). Propose the revision and reasoning. Do not unilaterally rewrite the broader plan.
- `PERMISSION-REQUEST:` — you cannot proceed without explicit permission for a specific external / irreversible action (push to a shared branch, deploy, modify a file outside the agreed scope, call an external API that costs money, etc.). Format:

  ```
  PERMISSION-REQUEST:
  Action: <concrete action you want to take>
  Why: <why this action is needed for the step>
  Fallback if denied: <what you will do instead, or "stop the step">
  ```

- `ESCALATE:` — other decision the manager must make (ambiguity in the spec you cannot resolve from context, dependency on another step's output that isn't yet available, a strategic call that affects scope).
- `REVIEW:` — (thinker) the terminal marker of a plan review: the body is one of `pass` / `revise` / `override`, the same vocabulary `agentctl plan-review --verdict` and `gates.PLAN_REVIEW_VERDICTS` use. The root records the verdict immediately with `agentctl plan-review --verdict <body> --reviewer thinker --target <plan>`, before any further edit to the plan file — the verdict is bound to the plan's sha256 and an edit invalidates the binding.
