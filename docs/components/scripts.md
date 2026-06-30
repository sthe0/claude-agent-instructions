# Scripts

> The executable spine of the repo — the coordination engine, the verification guards, the setup wiring, and the experience-recording tooling that live under scripts/.

Everything the repo *does* mechanically lives in [scripts/](../../scripts/): the deterministic control-flow that prose alone cannot enforce. The directory has its own machine-checked inventory — [scripts/README.md](../../scripts/README.md) — so this page is the map of what kinds of scripts there are, not a restatement of the list.

The four families:

- **The coordination engine** — [scripts/agentctl/](../../scripts/agentctl/README.md), a code state machine that owns the deterministic spine of a substantive task (classify → plan → approve → dispatch → verify → resolve). It is the canonical instance of the root principle "Separate rule from perception; determinize the rule at its proper structural level" — the engine owns the deterministic rule (control flow) while the model supplies the per-leaf perception; its architecture is described in [the coordination engine](../architecture/coordination-engine.md).
- **Verification guards** — the `verify-*.py` / `lint-*.py` scripts that keep the repo internally consistent (cross-references resolve, the README inventories match the filesystem, the engine's gates each have a guardian hook, prose stays under its size ceilings). They are run together by `verify-all.py`.
- **Setup and distribution** — `setup-symlinks.sh` and its helpers, which wire the repo into `~/.claude/` and `~/.cursor/`, merge the policy settings, and install the reminder and git hooks; `doctor.sh` confirms the runtime is ready.
- **Hooks and experience tooling** — the `hook-*.py` guardians and reminders (see [hooks.md](hooks.md)) and `record-experience.py`, which manages the experience leaves the system accumulates as it resolves tasks.

The full per-script inventory, kept in sync with the filesystem by [verify-readme.py](../../scripts/verify-readme.py), is in [scripts/README.md](../../scripts/README.md).
