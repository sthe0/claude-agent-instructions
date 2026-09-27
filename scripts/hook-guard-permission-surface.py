#!/usr/bin/env python3
"""PreToolUse hook: ask a human before a spawned child widens its own permission
surface outside the plan-stage grant channel.

Difficulty removed: a spawned child's permissions should be a function of its
approved plan stage (`[stage.grants]`, materialized by `spawn-specialist.py`
into `--settings`/`--add-dir`). Nothing stopped a child from bypassing that
channel by editing a live settings document directly, invoking `claude` itself
with a widening flag, writing agentctl's own state, reaching a persistent-
launch-registration surface, or self-granting a permission request via
`agentctl resolve-permission --decision granted`. A prior guard for this
(`hook-guard-permission-self-grant.py`, commit b1d5802) was reverted because
sticky arming across invocations plus UNKNOWN=>deny on Bash produced
false positives on ordinary work. This guard is the narrowed
replacement the abandonment leaf calls for: NO cross-invocation state, NO
UNKNOWN=>deny — every branch fires only on POSITIVE identification, and
anything ambiguous, unreadable or unparsable ALLOWS.

`decide(tool_name, tool_input, cwd, permission_mode, read_file)` is a function
of its arguments only — no transcript read, no session state, no filesystem
read except through the injected `read_file`. It returns exactly `"allow"` or
`"ask"`, NEVER `"deny"`: `ask` behaves like `deny` in headless mode (no TTY to
answer it) but shows a real interactive dialog when one exists, which is the
whole point of a guard whose job is to let a HUMAN see and approve an
out-of-scope grant (docs/components/settings-and-permissions.md § Hook
decision semantics, "ask" ≡ "deny" in headless, real dialog interactively).
`permission_mode` does not change the returned decision — it is accepted for
interface parity with that decision table, not consulted, since the guard's
own answer to "is this a G-target" never depends on which mode the session is
in.

All G-target predicates come from `lib/widening_targets.py` — the SAME shape
predicates `agentctl/grants.py`'s validator refuses a grant for — so this
guard never defines a target predicate of its own and the two are provably
disjoint (`scripts/tests/test_grants_guard_disjoint.py`): what the validator
lets a grant NAME, this guard never fires on; what this guard fires on, the
validator never lets a grant name. A broad grantable rule can still permit a
concrete call that fires — `Bash(cat:*)` is accepted by `validate_rule` while
`cat x > $CLAUDE_AGENT_HOME/settings.json` fires G1-bash — that is an intended
fire, not a gap, pinned by a dedicated test.

The four branches:

  G1-edit  — an Edit onto a LIVE-LOADED settings document
             (`widening_targets.is_live_settings`, never a repo template such
             as this repo's own `settings/base.json`) whose diff touches a
             security-relevant key: `permissions.allow`/`permissions.deny` via
             `lib/permission_surface.widens` (fires only on an actual WIDENING
             — a narrowing edit removing an allow rule or adding a deny rule
             does not fire, since narrowing needs no human gate), or any of
             `hooks`, `env`, `apiKeyHelper`, `extraKnownMarketplaces`,
             `allowManagedPermissionRulesOnly` (fires on ANY change to one of
             these — none of them has a narrow/widen direction a two-document
             diff alone can judge; https://code.claude.com/docs/en/settings
             documents `permissions`, `hooks` and `apiKeyHelper` as live-
             reloaded keys and lists the rest as security-sensitive). An
             unreadable base file, an Edit whose `old_string` does not match
             the base file, or either side failing to parse as JSON => allow.
  G1-bash  — a Bash write target (`lib/bash_write_targets.command_write_
             targets`) that is a live-loaded settings document. No diff check:
             a shell write into a live settings document has no readable
             "before" the way an Edit's `old_string` does, so any such write
             fires.
  G1-state — an Edit or Bash write target under agentctl's own state
             directory (`widening_targets.is_agentctl_state_path`) — writing
             there could forge a stage outcome or gate record.
  G2       — a Bash segment (`lib/bash_write_targets.segments`) whose leading
             program, after wrapper-stripping, is `claude`
             (`widening_targets.is_claude_program` — covers a bare/absolute/
             `claude-code`-aliased spelling, a wrapper form, an install-path
             spelling, and a package-runner form) carrying a widening flag
             (`--dangerously-skip-permissions`, `--permission-mode`,
             `--add-dir`, `--settings`, `--mcp-config`). A raw ad-hoc `claude`
             invocation naming one of these flags bypasses the plan-stage
             grant channel regardless of the flag's value, which is why the
             value itself is never inspected.
  G3       — an Edit or Bash write target under a persistent-launch-
             registration surface (`widening_targets.is_launch_surface`), or a
             Bash command invoking `crontab` (`widening_targets.
             is_crontab_target`) — either could make a spawned child install
             something that runs again after this session ends.
  G4       — a Bash segment invoking `agentctl resolve-permission` (via
             `widening_targets.agentctl_invocation_verb`) with
             `--decision granted` (not `--decision denied`, which never
             fires). The ask message names the `--rule` value(s) and `--stage`
             of the granting command, since after this guard ships that
             prompt is the user's one decision on an out-of-scope grant.

Only Edit and Bash are inspected — a raw Write onto one of these targets is a
NAMED RESIDUAL, not a gap the plan's own test procedure calls for closing
here.

Every fire is logged, but ONLY inside `main()` — `decide()` itself performs no
I/O beyond the injected `read_file` and writes nothing. The fire log path is
`$CLAUDE_PERMISSION_GUARD_LOG`, default `~/.local/log/claude-permission-guard.jsonl`;
each row carries the hook input's `session_id`, `transcript_path` and
`tool_use_id`. `scripts/replay-permission-guard.py` imports `decide()` alone,
never this module's logger, so a replay run never appends to that log.

Always exits 0 — a hook crash must never wedge the workflow, mirroring
hook-guard-canon-readonly.py's own always-exit-0 / always-caught-exception
structure, adapted from that template's two-way `str | None` contract to this
guard's three-way `"allow"`/`"ask"` (never `None`, never `"deny"`) contract.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import bash_write_targets, git_cwd, permission_surface, widening_targets  # noqa: E402

# Security-relevant settings keys other than `permissions` itself (handled
# separately via `permission_surface.widens`, which fires only on a WIDENING
# change). Confirmed live-reloaded / security-sensitive keys per
# https://code.claude.com/docs/en/settings: `hooks` and `apiKeyHelper` are
# named as live-reloaded alongside `permissions`; `env` is an ordinary key
# that still controls the child's environment; `extraKnownMarketplaces` and
# `allowManagedPermissionRulesOnly` are named as security-sensitive keys with
# managed-settings-override behavior. Any change to one of these fires,
# regardless of direction — unlike `permissions.allow`/`deny`, none of them
# has a narrow/widen reading a two-document diff alone can judge.
_OTHER_SECURITY_KEYS = (
    "hooks",
    "env",
    "apiKeyHelper",
    "extraKnownMarketplaces",
    "allowManagedPermissionRulesOnly",
)

_SETTINGS_REFERENCE_URL = "https://code.claude.com/docs/en/settings"

# G2: a `claude` invocation carrying one of these bypasses the plan-stage
# grant channel regardless of its value, so the value itself is never
# inspected.
_G2_WIDENING_FLAGS = frozenset({
    "--dangerously-skip-permissions",
    "--permission-mode",
    "--add-dir",
    "--settings",
    "--mcp-config",
})


def _real_read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _apply_edit(old_text: str, tool_input: dict) -> str | None:
    """The text an Edit tool_use would produce from `old_text`, or None when
    the edit cannot be reproduced (missing/non-string old_string/new_string,
    or old_string absent from old_text) — the caller's unparsable=>allow case."""
    old_string = tool_input.get("old_string")
    new_string = tool_input.get("new_string")
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return None
    if old_string not in old_text:
        return None
    if tool_input.get("replace_all"):
        return old_text.replace(old_string, new_string)
    return old_text.replace(old_string, new_string, 1)


def _security_relevant_diff(old_text: str, new_text: str) -> bool:
    """True iff the parsed old/new JSON documents differ on a security-
    relevant key. Any parse failure, or either side not being a JSON object,
    allows (False) — the UNKNOWN/unparsable=>allow rule applied at this
    narrower scope."""
    try:
        old_doc = json.loads(old_text)
        new_doc = json.loads(new_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(old_doc, dict) or not isinstance(new_doc, dict):
        return False
    if permission_surface.widens(old_doc, new_doc):
        return True
    return any(old_doc.get(key) != new_doc.get(key) for key in _OTHER_SECURITY_KEYS)


def _bash_write_targets(command: str, cwd: str) -> list[str]:
    eff_cwd = git_cwd.effective_git_cwd(command, cwd)
    return bash_write_targets.command_write_targets(command, eff_cwd)


def _g1_edit(tool_input: dict, read_file) -> str | None:
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    if not widening_targets.is_live_settings(file_path):
        return None
    try:
        old_text = read_file(file_path)
    except Exception:
        return None
    if not isinstance(old_text, str):
        return None
    new_text = _apply_edit(old_text, tool_input)
    if new_text is None:
        return None
    if _security_relevant_diff(old_text, new_text):
        return file_path
    return None


def _g1_bash(command: str, cwd: str) -> str | None:
    for target in _bash_write_targets(command, cwd):
        if widening_targets.is_live_settings(target):
            return target
    return None


def _g1_state_edit(tool_input: dict) -> str | None:
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and widening_targets.is_agentctl_state_path(file_path):
        return file_path
    return None


def _g1_state_bash(command: str, cwd: str) -> str | None:
    for target in _bash_write_targets(command, cwd):
        if widening_targets.is_agentctl_state_path(target):
            return target
    return None


def _g2_bash(command: str) -> str | None:
    for seg in bash_write_targets.segments(command):
        if not widening_targets.is_claude_program(seg):
            continue
        if any(tok in _G2_WIDENING_FLAGS for tok in seg):
            return " ".join(seg)
    return None


def _g3_edit(tool_input: dict) -> str | None:
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and widening_targets.is_launch_surface(file_path):
        return file_path
    return None


def _g3_bash(command: str, cwd: str) -> str | None:
    if widening_targets.is_crontab_target(command):
        return command
    for target in _bash_write_targets(command, cwd):
        if widening_targets.is_launch_surface(target):
            return target
    return None


def _flag_values(tokens: list[str], flag: str) -> list[str]:
    return [tokens[i + 1] for i in range(len(tokens) - 1) if tokens[i] == flag]


def _flag_value(tokens: list[str], flag: str) -> str | None:
    vals = _flag_values(tokens, flag)
    return vals[-1] if vals else None


def _g4_bash(command: str) -> tuple[list[str], str | None] | None:
    for seg in bash_write_targets.segments(command):
        invokes, verb = widening_targets.agentctl_invocation_verb(seg)
        if not invokes or verb != "resolve-permission":
            continue
        if _flag_value(seg, "--decision") != "granted":
            continue
        return _flag_values(seg, "--rule"), _flag_value(seg, "--stage")
    return None


def _g1_edit_message(target: str) -> str:
    keys = "permissions/" + "/".join(_OTHER_SECURITY_KEYS)
    return (
        f"Edit widens the live-loaded settings document {target} on a "
        f"security-relevant key ({keys}; {_SETTINGS_REFERENCE_URL}). Route "
        f"this through the plan's own grant channel (agentctl dispatch) "
        f"instead of editing the document directly."
    )


def _g1_bash_message(target: str) -> str:
    return (
        f"Bash command writes into the live-loaded settings document "
        f"{target}. Route this through the plan's own grant channel "
        f"(agentctl dispatch) instead of writing the document directly."
    )


def _g1_state_message(target: str) -> str:
    return (
        f"Targets agentctl's own state directory ({target}), which could "
        f"forge a stage outcome or gate record. Use agentctl itself, not a "
        f"direct write."
    )


def _g2_message(seg_text: str) -> str:
    return (
        f"Bash invokes `claude` with a widening flag outside the plan's own "
        f"grant channel: {seg_text}. Route this through agentctl dispatch "
        f"instead of an ad-hoc claude invocation."
    )


def _g3_message(target: str) -> str:
    return (
        f"Targets a persistent-launch-registration surface ({target}), "
        f"which could make a spawned child install something that runs "
        f"again after this session ends."
    )


def _g4_message(rules: list[str], stage: str | None) -> str:
    rule_text = ", ".join(rules) if rules else "(none named)"
    stage_text = stage if stage else "(none named)"
    return (
        f"`agentctl resolve-permission --decision granted` grants rule(s) "
        f"{rule_text} for stage {stage_text} outside the plan's own grant "
        f"channel — this is your decision on an out-of-scope grant."
    )


def decide_detailed(
    tool_name: str,
    tool_input: dict,
    cwd: str,
    permission_mode: str | None,
    read_file,
) -> tuple[str, str | None, str | None]:
    """`(decision, branch, message)` — `decision` is `"allow"` or `"ask"`,
    never `"deny"`. `permission_mode` is accepted, never consulted (see
    module docstring). Any unexpected shape or internal exception allows.

    `decide()` is the thin single-value wrapper `main()` and every test that
    only needs the verdict use; `replay-permission-guard.py` imports THIS
    function instead because its would-fires report needs to name which
    G-branch fired — both share the one underlying implementation below, so
    the replay tool can never drift from the shipped hook's own decision."""
    del permission_mode
    try:
        if tool_name == "Edit":
            target = _g1_edit(tool_input, read_file)
            if target:
                return "ask", "G1-edit", _g1_edit_message(target)
            target = _g1_state_edit(tool_input)
            if target:
                return "ask", "G1-state", _g1_state_message(target)
            target = _g3_edit(tool_input)
            if target:
                return "ask", "G3", _g3_message(target)
            return "allow", None, None

        if tool_name == "Bash":
            command = tool_input.get("command")
            if not isinstance(command, str) or not command.strip():
                return "allow", None, None
            target = _g1_bash(command, cwd)
            if target:
                return "ask", "G1-bash", _g1_bash_message(target)
            target = _g1_state_bash(command, cwd)
            if target:
                return "ask", "G1-state", _g1_state_message(target)
            g4_hit = _g4_bash(command)
            if g4_hit:
                rules, stage = g4_hit
                return "ask", "G4", _g4_message(rules, stage)
            target = _g2_bash(command)
            if target:
                return "ask", "G2", _g2_message(target)
            target = _g3_bash(command, cwd)
            if target:
                return "ask", "G3", _g3_message(target)
            return "allow", None, None

        return "allow", None, None
    except Exception:
        return "allow", None, None


def decide(
    tool_name: str,
    tool_input: dict,
    cwd: str,
    permission_mode: str | None,
    read_file,
) -> str:
    """`"allow"` or `"ask"` — see module docstring."""
    return decide_detailed(tool_name, tool_input, cwd, permission_mode, read_file)[0]


def _log_fire(log_path: str, payload: dict, branch: str, message: str | None) -> None:
    row = {
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "tool_use_id": payload.get("tool_use_id"),
        "tool_name": payload.get("tool_name"),
        "branch": branch,
        "decision": "ask",
        "message": message,
    }
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    permission_mode = payload.get("permission_mode")

    try:
        decision, branch, message = decide_detailed(
            tool_name, tool_input, cwd, permission_mode, _real_read_file,
        )
    except Exception:
        return 0

    if decision == "ask":
        log_path = os.environ.get("CLAUDE_PERMISSION_GUARD_LOG") or str(
            Path.home() / ".local" / "log" / "claude-permission-guard.jsonl"
        )
        _log_fire(log_path, payload, branch or "unknown", message)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": message or f"{branch} widening detected",
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
