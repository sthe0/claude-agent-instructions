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

The branches — G1 (three sub-forms: G1-edit, G1-bash, G1-state), G2, G3, G4:

  G1-edit  — an Edit, Write or MultiEdit onto a LIVE-LOADED settings document
             (`widening_targets.is_live_settings`, never a repo template such
             as this repo's own `settings/base.json`) whose diff touches a
             security-relevant key. `permissions.allow`/`permissions.deny` via
             `lib/permission_surface.widens`, `permissions.defaultMode` moving
             to a higher `_DEFAULT_MODE_RANK`, and
             `permissions.disableBypassPermissionsMode` on removal each fire
             only on an actual WIDENING — the corresponding narrowing edit
             does not fire, since narrowing needs no human gate; likewise
             `enableAllProjectMcpServers` false/absent -> true and
             `enabledMcpjsonServers` gaining an entry. `env` fires only on a
             change to a credential/routing-pattern KEY (`ANTHROPIC_*`,
             `CLAUDE_CODE_USE_*`, or containing `_BASE_URL`/`API_KEY`/
             `_PROXY`) — an allow-list, not "any key changed": round-2
             should-fix S2 found 8 of the 9 `G1-keys-calibration` fires were
             ordinary env edits (autocompact window, AFK timeout, output-
             length cap), none of them security-relevant. Any of `hooks`,
             `apiKeyHelper`, `extraKnownMarketplaces`,
             `allowManagedPermissionRulesOnly` fires on ANY change instead —
             none of these four has a narrow/widen direction a two-document
             diff alone can judge; https://code.claude.com/docs/en/settings
             documents `permissions`, `hooks` and `apiKeyHelper` as live-
             reloaded keys and lists the rest as security-sensitive. An
             unreadable base file, an edit that cannot be reproduced against
             it (Edit/Write's `old_string` absent or not found, MultiEdit's
             `edits` array failing to apply), or either side failing to parse
             as a JSON object => allow.
  G1-bash  — a Bash write target (`lib/bash_write_targets.command_write_
             targets`) that is a live-loaded settings document. No diff check:
             a shell write into a live settings document has no readable
             "before" the way an Edit's `old_string` does, so any such write
             fires.
  G1-state — an Edit, Write, MultiEdit, NotebookEdit or Bash write target
             under agentctl's own state directory
             (`widening_targets.is_agentctl_state_path`) — writing there could
             forge a stage outcome or gate record.
  G2       — a Bash segment (`_g2_segments`, which recurses into any
             `sh|bash|zsh -c PAYLOAD` invocation — interpreter possibly
             hidden behind a wrapper (`timeout 5 bash -c ...`) and `-c`
             possibly stacked in a short-flag cluster (`-lc`/`-ec`/`-xc`) —
             or an `eval PAYLOAD...` invocation, among `lib/bash_write_
             targets.segments`'s own segments, so a wrapped `claude` call
             hidden inside one shell -c/eval layer is still checked)
             whose leading program, after wrapper-stripping, is `claude`
             (`widening_targets.is_claude_program` — covers a bare/absolute/
             `claude-code`-aliased spelling, a wrapper form, an install-path
             spelling, and a package-runner form) carrying a widening flag:
             `--dangerously-skip-permissions`,
             `--allow-dangerously-skip-permissions`, `--permission-mode`,
             `--add-dir`, `--settings`, `--mcp-config`, `--allowedTools`,
             `--allowed-tools`. A raw ad-hoc `claude` invocation naming one of
             these flags bypasses the plan-stage grant channel regardless of
             the flag's value, which is why the value itself is never
             inspected — except for `--permission-mode`, whose `default`/
             `plan` values NARROW rather than widen and so are exempt
             (`_G2_PERMISSION_MODE_EXEMPT_VALUES`).
  G3       — an Edit, Write, MultiEdit or NotebookEdit onto, or a Bash write
             target under, a persistent-launch-registration surface
             (`widening_targets.is_launch_surface`), or a Bash command
             invoking `crontab` (`widening_targets.is_crontab_target`) —
             either could make a spawned child install something that runs
             again after this session ends.
  G4       — a Bash segment, recursed through the same `_g2_segments` shell
             -c/eval layer as G2, invoking `agentctl resolve-permission` (via
             `widening_targets.agentctl_invocation_verb`) with
             `--decision granted` (bare or glued `--decision=granted`; not
             `--decision denied`, which never fires). A real call almost
             always answers a PENDING request via `--session` rather than
             repeating `--rule` on the command line, so the ask message names
             the `--rule` value(s) when given, else the pending request's own
             action read from agentctl's session state through the injected
             `read_file` (falling back to naming the session when that state
             is unavailable) — and the `--stage` of the granting command,
             since after this guard ships that prompt is the user's one
             decision on an out-of-scope grant.

Edit, Write, MultiEdit, NotebookEdit and Bash are all inspected. Write
carries its whole `after` document as `content`, so G1-edit reuses the same
security-relevant-diff check with no old_string/new_string reconstruction.
MultiEdit's `edits` array is applied in order to reconstruct the after-text.
NotebookEdit is checked by `notebook_path` alone (G1-state, G3) — a notebook
is not a settings document, so G1-edit does not apply to it.

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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import bash_write_targets, config_root, git_cwd, permission_surface, widening_targets  # noqa: E402

# Security-relevant settings keys other than `permissions` itself (handled
# separately via `permission_surface.widens`, which fires only on a WIDENING
# change). Confirmed live-reloaded / security-sensitive keys per
# https://code.claude.com/docs/en/settings: `hooks` and `apiKeyHelper` are
# named as live-reloaded alongside `permissions`; `extraKnownMarketplaces` and
# `allowManagedPermissionRulesOnly` are named as security-sensitive keys with
# managed-settings-override behavior. Any change to one of these fires,
# regardless of direction — unlike `permissions.allow`/`deny`, none of them
# has a narrow/widen reading a two-document diff alone can judge.
#
# `env` used to be here too, firing on ANY change to that dict — round-2
# should-fix S2: a calibration run measured 8 of the 9 `G1-keys-calibration`
# fires were autocompact-window, AFK-timeout or output-length env edits, none
# of them security-relevant. `env` is handled separately below by
# `_env_security_relevant_diff`, an allow-list of credential/routing key
# PATTERNS rather than "any change to any key" — the same over-report-not-
# under-report posture applied per-key instead of to the whole dict.
_OTHER_SECURITY_KEYS = (
    "hooks",
    "apiKeyHelper",
    "extraKnownMarketplaces",
    "allowManagedPermissionRulesOnly",
)

# S2: named env-key patterns that ARE security-relevant -- credential,
# base-URL, proxy and provider-routing variables. Everything else in `env`
# (output-length caps, timeouts, the autocompact window, telemetry toggles)
# is deliberately NOT matched here, unlike the deny-nothing `_OTHER_SECURITY_KEYS`
# keys above: this dict changes routinely for non-security reasons, so it
# needs an allow-list of what DOES matter rather than firing on everything.
_ENV_SECURITY_KEY_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
_ENV_SECURITY_KEY_SUBSTRINGS = ("_BASE_URL", "API_KEY", "_PROXY")

# S2: `permissions.defaultMode` values ranked narrow (0) to wide (4). A
# change to a HIGHER rank widens; same-or-lower narrows and is not reported.
# A mode absent from this table (a future harness addition) is unranked, and
# any change touching it fires conservatively rather than being silently
# treated as safe. `bypassPermissions` outranks `auto` (round-3 should-fix
# 3): `auto` still applies the session's allow/deny rules and only skips the
# ask, while `bypassPermissions` skips the rules themselves -- tying them at
# the same rank meant an `auto` -> `bypassPermissions` edit went unreported.
_DEFAULT_MODE_RANK = {
    "plan": 0,
    "default": 1,
    "acceptEdits": 2,
    "auto": 3,
    "bypassPermissions": 4,
}

_SETTINGS_REFERENCE_URL = "https://code.claude.com/docs/en/settings"

# G2: a `claude` invocation carrying one of these bypasses the plan-stage
# grant channel. Matched on both the bare `flag` and glued `flag=value`
# forms (`_flag_present` below). Every flag here except `--permission-mode`
# fires on presence alone, regardless of value — `--permission-mode` is the
# one exception, since `default`/`plan` NARROW permissions rather than widen
# them (`_G2_PERMISSION_MODE_EXEMPT_VALUES`).
_G2_WIDENING_FLAGS = frozenset({
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--permission-mode",
    "--add-dir",
    "--settings",
    "--mcp-config",
    "--allowedTools",
    "--allowed-tools",
})

_G2_PERMISSION_MODE_EXEMPT_VALUES = frozenset({"default", "plan"})


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


def _is_security_relevant_env_key(key: str) -> bool:
    return key.startswith(_ENV_SECURITY_KEY_PREFIXES) or any(
        s in key for s in _ENV_SECURITY_KEY_SUBSTRINGS
    )


def _env_security_relevant_diff(old_env, new_env) -> bool:
    """S2: unlike `_OTHER_SECURITY_KEYS`, `env` fires only on a change to a
    key matching `_is_security_relevant_env_key` -- an allow-list of
    credential/routing patterns, not "any key changed"."""
    old_env = old_env if isinstance(old_env, dict) else {}
    new_env = new_env if isinstance(new_env, dict) else {}
    keys = set(old_env) | set(new_env)
    return any(
        _is_security_relevant_env_key(key) and old_env.get(key) != new_env.get(key)
        for key in keys
    )


def _default_mode_widens(old_mode, new_mode) -> bool:
    """S2: `permissions.defaultMode` widens only when it moves to a HIGHER
    `_DEFAULT_MODE_RANK`; an unranked mode on either side fires
    conservatively since its rank relative to the other is unknown."""
    if old_mode == new_mode:
        return False
    old_rank = _DEFAULT_MODE_RANK.get(old_mode)
    new_rank = _DEFAULT_MODE_RANK.get(new_mode)
    if old_rank is None or new_rank is None:
        return True
    return new_rank > old_rank


def _bypass_permissions_widens(old_value, new_value) -> bool:
    """S2: `permissions.disableBypassPermissionsMode` widens on REMOVAL --
    going from a truthy (disabling) value to a falsy/absent one re-enables
    `bypassPermissions`. Setting it (the reverse) narrows and is not fired."""
    return bool(old_value) and not bool(new_value)


def _enable_all_mcp_widens(old_value, new_value) -> bool:
    """S2: `enableAllProjectMcpServers` widens on false/absent -> true."""
    return bool(new_value) and not bool(old_value)


def _enabled_mcp_servers_widens(old_value, new_value) -> bool:
    """S2: `enabledMcpjsonServers` widens when an entry is ADDED, mirroring
    `permission_surface.widens`'s own allow-list treatment."""
    old_list = old_value if isinstance(old_value, list) else []
    new_list = new_value if isinstance(new_value, list) else []
    return any(entry not in old_list for entry in new_list)


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
    if any(old_doc.get(key) != new_doc.get(key) for key in _OTHER_SECURITY_KEYS):
        return True
    if _env_security_relevant_diff(old_doc.get("env"), new_doc.get("env")):
        return True
    old_perms = old_doc.get("permissions") if isinstance(old_doc.get("permissions"), dict) else {}
    new_perms = new_doc.get("permissions") if isinstance(new_doc.get("permissions"), dict) else {}
    if _default_mode_widens(old_perms.get("defaultMode"), new_perms.get("defaultMode")):
        return True
    if _bypass_permissions_widens(
        old_perms.get("disableBypassPermissionsMode"), new_perms.get("disableBypassPermissionsMode")
    ):
        return True
    if _enable_all_mcp_widens(
        old_doc.get("enableAllProjectMcpServers"), new_doc.get("enableAllProjectMcpServers")
    ):
        return True
    if _enabled_mcp_servers_widens(old_doc.get("enabledMcpjsonServers"), new_doc.get("enabledMcpjsonServers")):
        return True
    return False


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


def _g1_write(tool_input: dict, read_file) -> str | None:
    """Same G1-edit predicate as `_g1_edit`, for a Write tool_use — Write
    already supplies the whole `after` text directly as `content`, so no
    old_string/new_string reconstruction is needed."""
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    if not widening_targets.is_live_settings(file_path):
        return None
    new_text = tool_input.get("content")
    if not isinstance(new_text, str):
        return None
    try:
        old_text = read_file(file_path)
    except Exception:
        return None
    if not isinstance(old_text, str):
        return None
    if _security_relevant_diff(old_text, new_text):
        return file_path
    return None


def _apply_multi_edit(old_text: str, edits: list) -> str | None:
    text = old_text
    for edit in edits:
        if not isinstance(edit, dict):
            return None
        applied = _apply_edit(text, edit)
        if applied is None:
            return None
        text = applied
    return text


def _g1_multi_edit(tool_input: dict, read_file) -> str | None:
    """Same G1-edit predicate, for a MultiEdit tool_use — its `edits` array
    is applied in order to reconstruct the after-text `_apply_edit` would
    otherwise produce from a single old_string/new_string pair."""
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    if not widening_targets.is_live_settings(file_path):
        return None
    edits = tool_input.get("edits")
    if not isinstance(edits, list) or not edits:
        return None
    try:
        old_text = read_file(file_path)
    except Exception:
        return None
    if not isinstance(old_text, str):
        return None
    new_text = _apply_multi_edit(old_text, edits)
    if new_text is None:
        return None
    if _security_relevant_diff(old_text, new_text):
        return file_path
    return None


def _g1_state_edit(tool_input: dict) -> str | None:
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and widening_targets.is_agentctl_state_path(file_path):
        return file_path
    return None


def _g1_state_notebook(tool_input: dict) -> str | None:
    notebook_path = tool_input.get("notebook_path")
    if isinstance(notebook_path, str) and widening_targets.is_agentctl_state_path(notebook_path):
        return notebook_path
    return None


def _g1_state_bash(command: str, cwd: str) -> str | None:
    for target in _bash_write_targets(command, cwd):
        if widening_targets.is_agentctl_state_path(target):
            return target
    return None


_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh"})


def _short_flag_cluster_has_c(tok: str) -> bool:
    """True for a short-flag cluster token (`-c`, `-lc`, `-ec`, `-xc`, ...)
    that includes `c` — the shell's "read commands from the following operand"
    flag, matched even when stacked with another short flag (`-l`/`-e`/`-x`)
    rather than passed bare. `--`-prefixed (long) flags never match."""
    return tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]


_PLUS_OPTION = re.compile(r"\+[A-Za-z]+")
_SHELL_OPTIONS_WITH_ARGUMENT = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})


def _shell_c_payloads(seg: list[str]) -> list[str]:
    """Payload strings from a `sh|bash|zsh -c PAYLOAD` invocation, or from an
    `eval PAYLOAD...` invocation — empty when `seg` is neither.

    round-2 should-fix S4: the previous exact `"-c"` match missed a stacked
    short-flag cluster (`-lc`, `-ec`, `-xc`) and a shell hidden behind a
    wrapper (`timeout 5 bash -c ...`, `env X=1 sh -c ...`) — `strip_wrappers`
    runs first so the interpreter surfaces the same way `is_claude_program`
    already relies on it to. `eval` is checked BEFORE `strip_wrappers` runs,
    since `eval` is itself one of `strip_wrappers`'s own known wrapper tokens
    and would otherwise be consumed and lost rather than recognized here; its
    operands are rejoined with a space since `eval` itself concatenates them
    via IFS before running the result."""
    if seg and widening_targets.program_name(seg[0]).casefold() == "eval":
        payload_tokens = seg[1:]
        return [" ".join(payload_tokens)] if payload_tokens else []
    stripped = widening_targets.strip_wrappers(seg)
    if not stripped:
        return []
    program = widening_targets.program_name(stripped[0]).casefold()
    if program not in _SHELL_INTERPRETERS:
        return []
    i = 1
    while i < len(stripped):
        tok = stripped[i]
        if tok == "--":
            break
        if not (tok.startswith("-") or _PLUS_OPTION.fullmatch(tok)):
            # round-3 nit 3: a shell parses its own flags only up to the
            # first operand -- `bash script.sh -c x` runs a SCRIPT, and that
            # script's own `-c x` are ITS arguments, not `bash`'s.
            break
        if tok.startswith("-") and _short_flag_cluster_has_c(tok):
            return [stripped[i + 1]] if i + 1 < len(stripped) else []
        # `-o pipefail`, `+O extglob`, `--rcfile F`: the option's own argument
        # is not an operand, so skip it rather than stop on it.
        i += 2 if tok in _SHELL_OPTIONS_WITH_ARGUMENT else 1
    return []


def _g2_segments(command: str) -> list[list[str]]:
    """`bash_write_targets.segments(command)`'s segments, recursed into any
    `sh|bash|zsh -c PAYLOAD` invocation among them so a `claude` call hidden
    inside a shell -c payload is still checked — a spawned child could
    otherwise dodge G2 entirely by wrapping its widening call in one layer of
    `bash -c '...'`."""
    out: list[list[str]] = []
    for seg in bash_write_targets.segments(command):
        out.append(seg)
        for payload in _shell_c_payloads(seg):
            out.extend(_g2_segments(payload))
    return out


def _flag_present(seg: list[str], flag: str) -> bool:
    """True iff `flag` appears in `seg` in either its bare or glued
    `flag=value` form."""
    prefix = flag + "="
    return any(tok == flag or tok.startswith(prefix) for tok in seg)


def _g2_bash(command: str) -> str | None:
    for seg in _g2_segments(command):
        if not widening_targets.is_claude_program(seg):
            continue
        for flag in _G2_WIDENING_FLAGS:
            if not _flag_present(seg, flag):
                continue
            if flag == "--permission-mode":
                value = _flag_value(seg, "--permission-mode")
                if value in _G2_PERMISSION_MODE_EXEMPT_VALUES:
                    continue
            return " ".join(seg)
    return None


def _g3_edit(tool_input: dict) -> str | None:
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and widening_targets.is_launch_surface(file_path):
        return file_path
    return None


def _g3_notebook(tool_input: dict) -> str | None:
    notebook_path = tool_input.get("notebook_path")
    if isinstance(notebook_path, str) and widening_targets.is_launch_surface(notebook_path):
        return notebook_path
    return None


def _g3_bash(command: str, cwd: str) -> str | None:
    if widening_targets.is_crontab_target(command):
        return command
    for target in _bash_write_targets(command, cwd):
        if widening_targets.is_launch_surface(target):
            return target
    return None


def _flag_values(tokens: list[str], flag: str) -> list[str]:
    """Every value passed for `flag`, in either its bare `flag value` (two
    separate tokens) or glued `flag=value` form."""
    prefix = flag + "="
    out: list[str] = []
    for i, tok in enumerate(tokens):
        if tok == flag and i + 1 < len(tokens):
            out.append(tokens[i + 1])
        elif tok.startswith(prefix):
            out.append(tok[len(prefix):])
    return out


def _flag_value(tokens: list[str], flag: str) -> str | None:
    vals = _flag_values(tokens, flag)
    return vals[-1] if vals else None


def _g4_bash(command: str) -> tuple[list[str], str | None, str | None, str | None, str] | None:
    """round-2 should-fix S3: recurses through `_g2_segments` (the same `sh|
    bash|zsh -c`/`eval` recursion G2 uses) rather than the flat top-level
    `bash_write_targets.segments`, so a resolve-permission call hidden inside
    one shell -c layer still fires. Also returns the granting command's own
    `--session` value, since a real call answers a PENDING request and rarely
    repeats the rule on the command line — the caller resolves the pending
    request's action from that session id. round-3 nit 1: also returns the
    matched segment's own text (`" ".join(seg)`, mirroring `_g2_bash`'s return
    shape) so a caller (the replay tool) can group on the firing segment
    instead of a noisy multi-command whole line. round-3 nit 2: also returns
    the real `--scope` value (`once`/`project`/`global`/`stage` per the CLI's
    own choices — NOT `--stage`, which `resolve-permission` does not even
    accept as an argument; a real call names its scope, not a stage index)."""
    for seg in _g2_segments(command):
        invokes, verb = widening_targets.agentctl_invocation_verb(seg)
        if not invokes or verb != "resolve-permission":
            continue
        if _flag_value(seg, "--decision") != "granted":
            continue
        return (
            _flag_values(seg, "--rule"),
            _flag_value(seg, "--stage"),
            _flag_value(seg, "--session"),
            _flag_value(seg, "--scope"),
            " ".join(seg),
        )
    return None


def _g4_pending_action(session: str | None, read_file) -> str | None:
    """S3: read the granting call's own pending request out of agentctl's
    session state (current root, then the legacy pre-isolation root) through
    the injected `read_file` — never a bare filesystem read, matching this
    guard's own no-I/O-outside-read_file contract. None on a missing
    session, an unreadable/unparsable file, or no `permission_request` in it;
    the caller falls back to naming the session instead."""
    if not session or read_file is None:
        return None
    filename = f"{config_root.sanitize_session_id(session)}.json"
    for state_dir in (config_root.agentctl_state_dir(), config_root.agentctl_legacy_state_dir()):
        try:
            text = read_file(str(state_dir / filename))
        except Exception:
            continue
        try:
            doc = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        request = doc.get("permission_request")
        if isinstance(request, dict):
            action = request.get("action")
            if isinstance(action, str) and action:
                return action
    return None


def _g1_edit_message(target: str) -> str:
    keys = (
        "permissions/"
        + "/".join(_OTHER_SECURITY_KEYS)
        + "/env(credential|base-url|proxy)/permissions.defaultMode/"
        "permissions.disableBypassPermissionsMode/enableAllProjectMcpServers/"
        "enabledMcpjsonServers"
    )
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


def _g4_message(
    rules: list[str], stage: str | None, session: str | None, scope: str | None, read_file,
) -> str:
    """round-2 should-fix S3: a real call answers a pending request via
    `--session`/`--decision`, not by naming the rule again, so `rules` is
    almost always empty — the old "(none named)" left the user approving
    something unnamed. When `--rule` IS given, name it directly; otherwise
    resolve the pending request's own action via `_g4_pending_action`, and
    fall back to naming the session when that lookup comes up empty.

    round-3 nit 2: a real call names its `--scope` (defaulting to `once`
    when omitted, matching the CLI's own default), never `--stage` — that
    flag does not exist on `resolve-permission` at all, so `stage` is always
    None for every real fire and is now folded into the message only when a
    (synthetic/test) command actually supplies one, instead of the old
    unconditional "stage (none named)" noise every real message carried."""
    if rules:
        subject = f"grants rule(s) {', '.join(rules)}"
    else:
        action = _g4_pending_action(session, read_file)
        if action:
            subject = f"grants the pending request: {action}"
        else:
            session_text = session if session else "(session unavailable)"
            subject = f"grants a pending request for session {session_text} (action unavailable)"
    scope_text = scope if scope else "once"
    stage_clause = f", stage {stage}" if stage else ""
    return (
        f"`agentctl resolve-permission --decision granted` {subject} at "
        f"scope {scope_text}{stage_clause} outside the plan's own grant "
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

        if tool_name == "Write":
            target = _g1_write(tool_input, read_file)
            if target:
                return "ask", "G1-edit", _g1_edit_message(target)
            target = _g1_state_edit(tool_input)
            if target:
                return "ask", "G1-state", _g1_state_message(target)
            target = _g3_edit(tool_input)
            if target:
                return "ask", "G3", _g3_message(target)
            return "allow", None, None

        if tool_name == "MultiEdit":
            target = _g1_multi_edit(tool_input, read_file)
            if target:
                return "ask", "G1-edit", _g1_edit_message(target)
            target = _g1_state_edit(tool_input)
            if target:
                return "ask", "G1-state", _g1_state_message(target)
            target = _g3_edit(tool_input)
            if target:
                return "ask", "G3", _g3_message(target)
            return "allow", None, None

        if tool_name == "NotebookEdit":
            target = _g1_state_notebook(tool_input)
            if target:
                return "ask", "G1-state", _g1_state_message(target)
            target = _g3_notebook(tool_input)
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
                rules, stage, session, scope, _g4_seg_text = g4_hit
                return "ask", "G4", _g4_message(rules, stage, session, scope, read_file)
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


def _log_target(tool_input: dict) -> str | None:
    """round-2 should-fix S5: whichever of `command`/`file_path`/`notebook_path`
    the fired tool call carries -- without it, a logged row that lost its
    `tool_use_id` (the field is optional in the hook payload) has no way back
    to the command or file that actually fired."""
    for key in ("command", "file_path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _log_fire(log_path: str, payload: dict, branch: str, message: str | None) -> None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "tool_use_id": payload.get("tool_use_id"),
        "tool_name": payload.get("tool_name"),
        "permission_mode": payload.get("permission_mode"),
        "target": _log_target(tool_input),
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
