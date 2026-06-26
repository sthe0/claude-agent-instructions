# Settings and permissions

> How the harness is configured — the layered settings.json, the permissions CLI, and the action taxonomy that decides which tool calls run without a prompt.

Two related surfaces control what the agent may do without asking and how the harness behaves.

**Settings.** The policy settings live in [settings/](../../settings/README.md) as [settings/base.json](../../settings/base.json), the versioned source merged per machine by `setup-symlinks.sh` (which calls `apply-settings.sh`). The merge is **additive**, so dropping a key from the source does not clear it from the live settings — a deliberate change must overwrite, not omit. This is where the harness-level knobs and the allowlist of pre-authorized tool calls are defined.

**Permissions.** The workflow-level permission grants live in [permissions/](../../permissions/README.md), driven by [scripts/permissions-cli.py](../../scripts/permissions-cli.py). These are operational config (which actions are pre-authorized for which scope), separate from memory and from the settings allowlist.

**Action taxonomy.** Side-effect-free actions are pre-authorized as a class — reads, searches, `--help`, `--dry-run`, and the read-only MCP verbs — so the agent never prompts for them. The classification is enforced by a `classify_action` verb taxonomy in the versioned settings, not re-decided per call. Plan-scope-declared actions become pre-authorized once a plan is approved (anything the plan names in its reference files, outputs, or declared operations), which is why the [plan-approval gate](../architecture/coordination-engine.md) is the boundary that matters: before it, production edits are denied; after it, the declared scope proceeds without re-asking.
