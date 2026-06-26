# Hooks

> The harness-run scripts that enforce the non-skippable gates and fire the reminders — the deterministic guardians that make the coordination spine binding rather than advisory.

A hook is a script the Claude Code harness runs on an event (prompt submit, before a tool call, on stop). Because the harness — not the model — executes them, hooks can **deny** a tool call outright, which is what makes a gate non-skippable. The hook scripts live in [scripts/](../../scripts/) as `hook-*.py` and are installed into the active settings by `setup-symlinks.sh` (via `install-reminder-hooks.sh`); the settings surface they attach to is described in [settings-and-permissions.md](settings-and-permissions.md) and under [settings/](../../settings/README.md).

The hooks fall into two roles:

- **Gate guardians (hard-deny).** [hook-state-gate.py](../../scripts/hook-state-gate.py) is the load-bearing one: it denies production edits until the coordination engine reaches an execution node, enforcing the plan-approval gate (and refusing a plan-file edit during execution, which must instead route through replan). [hook-engine-start.py](../../scripts/hook-engine-start.py) auto-arms the engine on the first prompt so the gate is active from the start.
- **Reminders (nudge, never block).** A family of `hook-*.py` scripts surface the right discipline at the right moment without denying the call — among them [hook-resolution-reminder.py](../../scripts/hook-resolution-reminder.py) (ask the user to confirm resolution rather than assume it), [hook-self-improvement-reminder.py](../../scripts/hook-self-improvement-reminder.py) (a likely behavior-feedback turn), [hook-skill-first.py](../../scripts/hook-skill-first.py) (prefer a matching skill over hand-rolled Bash), [hook-retry-detector.py](../../scripts/hook-retry-detector.py) (a repeated failure is a difficulty to overcome), and the README / tracker / language / push-confirmation reminders.

The two gates the guardians protect — plan-approval and resolution — are the same two gates on the state machine described in [the coordination engine](../architecture/coordination-engine.md). `verify-agentctl.py` checks that every gate still has its guardian hook wired up.
