"""Disjointness between agentctl/grants.py's `validate_rule` (what a plan
stage may DECLARE as a grant) and hook-guard-permission-surface.py's
`decide`/`decide_detailed` (what a CONCRETE runtime call is allowed to do
without asking a human), per Stage 7's plan procedure.

Two directions, both needed because the two surfaces read different
information — a rule's own static text vs. a call's actual argument values:

  A) `validate_rule` ACCEPTS rule R => the guard never fires (asks) on the
     concrete call R's own text names. Swept directly off
     test_stage_grants.py's `_ACCEPTED_RULES` table so a future addition
     there is automatically exercised here too.

  B) the guard fires (asks) on a concrete call => the "natural" rule text
     for that exact call is refused by `validate_rule`. The validator can
     never license, as a declared grant, a call the guard would otherwise
     have to ask about.

The pinned exception the plan calls out explicitly (round-5 L7): `Bash(cat:*)`
is ACCEPTED (the rule's own text has no redirect for the validator to see),
while the CONCRETE call `cat x > <live settings>` -- which that same wildcard
rule would admit at materialization time -- still fires G1-bash. This is a
deliberate asymmetry, not a violation of direction A: `cat:*` is only ever
exercised here via its own plain command text (no redirect), never via the
redirected variant.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-guard-permission-surface.py"

_SPEC = importlib.util.spec_from_file_location("hook_guard_permission_surface", HOOK_SCRIPT)
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)

decide = guard.decide
decide_detailed = guard.decide_detailed

from agentctl.grants import GrantValidationError, validate_rule
from lib import config_root

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_stage_grants import _ACCEPTED_RULES  # noqa: E402


def _isolate_agent_home(monkeypatch, tmp_path) -> None:
    """`config_root.agent_home()` checks `CLAUDE_CONFIG_DIR` before
    `CLAUDE_AGENT_HOME` -- the live harness always sets the former, so a test
    isolating agent_home() must clear/override BOTH or the real value wins."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path))


_BASH_RULE_RE = re.compile(r"^Bash\((.*)\)$")
_EDIT_RULE_RE = re.compile(r"^Edit\((.*)\)$")


def _bash_command_from_rule(rule: str) -> str:
    m = _BASH_RULE_RE.match(rule)
    assert m, rule
    inner = m.group(1)
    if inner.endswith(":*"):
        inner = inner[: -len(":*")]
    return inner


def _edit_path_from_rule(rule: str) -> str:
    m = _EDIT_RULE_RE.match(rule)
    assert m, rule
    inner = m.group(1)
    assert inner.startswith("//"), inner
    return inner[1:]


# --- Direction A: accepted rule's own call never fires the guard -----------

_ACCEPTED_BASH_RULES = [r for r in _ACCEPTED_RULES if r.startswith("Bash(")]
_ACCEPTED_EDIT_RULES = [r for r in _ACCEPTED_RULES if r.startswith("Edit(")]


@pytest.mark.parametrize("rule", _ACCEPTED_BASH_RULES)
def test_accepted_bash_rule_never_fires_the_guard(rule):
    command = _bash_command_from_rule(rule)
    decision, branch, _ = decide_detailed("Bash", {"command": command}, "/tmp", "default", None)
    assert (decision, branch) == ("allow", None), f"{rule!r} -> {command!r} fired {branch}"


@pytest.mark.parametrize("rule", _ACCEPTED_EDIT_RULES)
def test_accepted_edit_rule_never_fires_the_guard(rule):
    path = _edit_path_from_rule(rule)
    tool_input = {"file_path": path, "old_string": "x", "new_string": "y"}
    decision, branch, _ = decide_detailed("Edit", tool_input, "/tmp", "default", lambda p: "x")
    assert (decision, branch) == ("allow", None), f"{rule!r} -> {path!r} fired {branch}"


# --- Direction B: a guard fire's natural rule text is refused --------------

def test_g1_edit_fire_pairs_with_refused_edit_rule(tmp_path):
    target = tmp_path / ".claude-agent" / "settings.json"
    old_text = '{"hooks": {}}'
    new_text = '{"hooks": {"PreToolUse": []}}'
    tool_input = {"file_path": str(target), "old_string": old_text, "new_string": new_text}
    decision, branch, _ = decide_detailed(
        "Edit", tool_input, str(tmp_path), "default", lambda p: old_text,
    )
    assert (decision, branch) == ("ask", "G1-edit")

    with pytest.raises(GrantValidationError):
        validate_rule(f"Edit(//{target})")


def test_g1_bash_fire_pairs_with_refused_redirect_rule(tmp_path):
    command = f"cat x > {tmp_path}/.claude-agent/settings.json"
    decision, branch, _ = decide_detailed("Bash", {"command": command}, str(tmp_path), "default", None)
    assert (decision, branch) == ("ask", "G1-bash")

    with pytest.raises(GrantValidationError):
        validate_rule(f"Bash({command}:*)")


def test_g1_bash_cat_wildcard_is_the_pinned_accept_but_concrete_redirect_still_fires(tmp_path):
    """Round-5 L7, pinned: `Bash(cat:*)`'s own rule text names no redirect, so
    the validator accepts it as grantable -- but the wildcard admits a
    concrete redirected call the guard checks independently, against the
    actual command text, and that call still fires G1-bash. Mode-independent
    (decide() never consults permission_mode) in both directions."""
    validate_rule("Bash(cat:*)")  # accepted -- must not raise

    plain = "cat x"
    redirected = f"cat x > {tmp_path}/.claude-agent/settings.json"
    for mode in ("default", "acceptEdits", "bypassPermissions", "plan", None):
        assert decide("Bash", {"command": plain}, str(tmp_path), mode, None) == "allow"
        assert decide("Bash", {"command": redirected}, str(tmp_path), mode, None) == "ask"


def test_g1_state_fire_pairs_with_refused_state_redirect_rule(tmp_path, monkeypatch):
    _isolate_agent_home(monkeypatch, tmp_path)
    state_dir = config_root.agentctl_state_dir()
    command = f"echo hi >> {state_dir}/session.json"
    decision, branch, _ = decide_detailed("Bash", {"command": command}, str(tmp_path), "default", None)
    assert (decision, branch) == ("ask", "G1-state")

    with pytest.raises(GrantValidationError):
        validate_rule(f"Bash({command}:*)")


def test_g2_fire_pairs_with_refused_claude_invocation_rule(tmp_path):
    command = "claude --dangerously-skip-permissions"
    decision, branch, _ = decide_detailed("Bash", {"command": command}, str(tmp_path), "default", None)
    assert (decision, branch) == ("ask", "G2")

    with pytest.raises(GrantValidationError):
        validate_rule(f"Bash({command}:*)")


def test_g3_fire_pairs_with_refused_crontab_rule(tmp_path):
    command = "crontab -e"
    decision, branch, _ = decide_detailed("Bash", {"command": command}, str(tmp_path), "default", None)
    assert (decision, branch) == ("ask", "G3")

    with pytest.raises(GrantValidationError):
        validate_rule(f"Bash({command}:*)")


def test_g4_fire_pairs_with_refused_user_authority_verb_rule(tmp_path):
    command = (
        "python3 -m agentctl resolve-permission --rule 'Bash(rm:*)' "
        "--stage 3 --decision granted"
    )
    decision, branch, _ = decide_detailed("Bash", {"command": command}, str(tmp_path), "default", None)
    assert (decision, branch) == ("ask", "G4")

    # Structural, not incidental: the validator refuses ANY rule invoking
    # `resolve-permission` regardless of the rest of its arguments (it is a
    # user-authority verb), so no accepted grant could ever have licensed
    # this call -- G4 firing is a defense-in-depth backstop for a call the
    # grant channel can never legitimately cover, not a materialization gap.
    with pytest.raises(GrantValidationError):
        validate_rule(f"Bash({command}:*)")
    with pytest.raises(GrantValidationError):
        validate_rule("Bash(python3 -m agentctl resolve-permission:*)")
