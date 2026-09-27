"""The plan-review gate requires an attestation the spawn mechanism must be able
to produce.

`gates.plan_review_blockers` accepts a `pass` verdict only when the reviewer
supplies a `plan_sha256` it computed from its own read of the bytes ("a reviewer
that could not read the plan cannot bind it"). That requirement is only coherent
if a spawned reviewer actually holds a hashing capability. On 2026-08-04 it did
not: no hashing verb existed in `classify.READONLY_BASH` or in
`settings/base.json`'s allow list, so every reviewer that tried to compute a
digest was refused and returned `PERMISSION-REQUEST:` instead of a verdict. Six
spawns, ~$4.2 and ~40 minutes went into diagnosing a deadlock the engine had
built into itself by demanding proof it withheld the means of.

Both halves of that pairing are pinned here, in one file, because either alone
regresses silently: a gate whose requirement is unmet has no failing test of its
own (it fails at spawn time, in a subprocess, as a permission refusal), and a
permission entry with no stated consumer looks like removable clutter.

The third case guards the other direction. The old grant `Bash(python3 -c ":*)`
was arbitrary code execution sitting in a fleet-wide, read-only-by-contract allow
list — it passed `lint-settings-base.py` only because the linter checks the verb
(`python3`) and not what follows it. Granting the narrow verb, not the general
interpreter, is the point; this test states that so a future "just re-add the
python3 one-liner form" has to argue with the reason.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from agentctl import classify

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
BASE_SETTINGS = REPO_ROOT / "settings" / "base.json"
SPAWN_SCRIPT = SCRIPTS_DIR / "spawn-specialist.py"

# The verbs a reviewer may reach for; both must be grantable, because the fleet
# spans macOS (`shasum`, from perl) and Linux (`sha256sum`, from coreutils).
HASHING_VERBS = ("shasum", "sha256sum")


def _allow_entries() -> list[str]:
    return json.loads(BASE_SETTINGS.read_text(encoding="utf-8"))["permissions"]["allow"]


def _load_spawn_module():
    spec = importlib.util.spec_from_file_location("spawn_specialist_attestation", SPAWN_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hashing_is_classified_side_effect_free():
    # A digest over bytes the caller can already read changes nothing; without
    # this the engine's own classifier calls the attestation a mutating action.
    for verb in HASHING_VERBS:
        assert verb in classify.READONLY_BASH, verb


def test_settings_base_grants_the_hashing_verbs():
    allow = _allow_entries()
    for verb in HASHING_VERBS:
        assert f"Bash({verb}:*)" in allow, verb


def test_the_gate_requiring_a_digest_and_the_grant_stay_together():
    """The binding itself: whatever the plan-review gate demands, the fleet grants.

    Stated against the gate's source rather than a copy of its wording, so
    deleting the requirement and deleting the grant stay one decision.
    """
    from agentctl import gates

    source = Path(gates.__file__).read_text(encoding="utf-8")
    demands_a_digest = "plan_sha256" in source
    grants_hashing = any(f"Bash({v}:*)" in _allow_entries() for v in HASHING_VERBS)
    assert demands_a_digest == grants_hashing, (
        "gates.py demands a reviewer-computed plan_sha256 but settings/base.json "
        "grants no hashing verb (or vice versa) — a reviewer cannot produce the "
        "attestation the gate requires, and every plan review deadlocks"
    )


def test_base_settings_grants_no_general_python_interpreter():
    # `Bash(python3 -c "...")` is arbitrary code execution; the read-only-only
    # contract of this file is about EFFECTS, and lint-settings-base.py only sees
    # the verb. Narrow verbs, never the interpreter.
    for entry in _allow_entries():
        assert not entry.startswith("Bash(python3 -c"), entry


def test_developer_spawns_ask_for_the_narrowest_write_mode():
    """`acceptEdits`, not `bypassPermissions`.

    bypassPermissions waives every permission class where only file writes are
    needed, and on a fleet whose managed layer sets
    `permissions.disableBypassPermissionsMode` it is silently ignored anyway — so
    the spawn that looked unattended in fact ran under prompts nobody could
    answer. Being inert is what made it misdirect the 2026-08-04 diagnosis.
    """
    mod = _load_spawn_module()
    args = argparse.Namespace(permission_mode=None, kind="developer")
    assert mod.resolve_permission_mode(args) == "acceptEdits"


def test_non_developer_spawns_request_no_elevated_mode():
    """thinker/planner/code-reviewer are pinned to "default" (not acceptEdits,
    not None) -- resolve_permission_mode always resolves to a concrete mode
    now that the CLI's own --permission-mode choices are narrowed to
    default/plan (see its help text)."""
    mod = _load_spawn_module()
    args = argparse.Namespace(permission_mode=None, kind="thinker")
    assert mod.resolve_permission_mode(args) == "default"


def test_an_explicit_permission_mode_still_wins():
    mod = _load_spawn_module()
    args = argparse.Namespace(permission_mode="plan", kind="developer")
    assert mod.resolve_permission_mode(args) == "plan"


def test_write_add_dir_grant_forces_accept_edits_reproducing_probe_cell():
    """Reproduces `probe-hook-decision-semantics.py`'s
    `add_dir:default_write_add_dir` cell: under plain `default` mode, a write
    add_dir's own `Edit(//path/**)` allow rule did not materialize real write
    access (docs/components/settings-and-permissions.md § Hook decision
    semantics, "D2/x written=False (expected True)"). A non-developer kind
    carrying a mode="write" add_dir grant must now resolve to `acceptEdits`,
    under which --add-dir alone already makes the directory writable, rather
    than silently landing in the broken `default` cell."""
    mod = _load_spawn_module()
    args = argparse.Namespace(permission_mode=None, kind="planner")
    engine_grants = [{"path": "/tmp/some-dir", "mode": "write", "provenance": "declared"}]
    assert mod.resolve_permission_mode(args, engine_grants) == "acceptEdits"


def test_read_only_add_dir_grant_does_not_force_accept_edits():
    mod = _load_spawn_module()
    args = argparse.Namespace(permission_mode=None, kind="planner")
    engine_grants = [{"path": "/tmp/some-dir", "mode": "read", "provenance": "declared"}]
    assert mod.resolve_permission_mode(args, engine_grants) == "default"


def test_no_engine_grants_still_resolves_default_for_non_developer_kinds():
    mod = _load_spawn_module()
    args = argparse.Namespace(permission_mode=None, kind="planner")
    assert mod.resolve_permission_mode(args, None) == "default"


# --- round-2 should-fix S1: cwd-wide deny counterweight for a forced-acceptEdits kind ---


def test_write_grant_cwd_deny_rules_fires_for_non_trusted_kind_with_write_grant(tmp_path):
    """S1: a write engine grant forces acceptEdits for e.g. `planner`, which
    would otherwise make the WHOLE cwd writable, not just the granted
    directory. `write_grant_cwd_deny_rules` must narrow that back down with
    an `Edit(//<cwd>/**)` deny."""
    mod = _load_spawn_module()
    engine_grants = [{"path": "/somewhere/else", "mode": "write", "provenance": "declared"}]
    denies = mod.write_grant_cwd_deny_rules("planner", engine_grants, str(tmp_path))
    expected = f"Edit(//{str(tmp_path).lstrip('/')}/**)"
    assert denies == [expected]


def test_write_grant_cwd_deny_rules_empty_for_trusted_kind(tmp_path):
    """developer/tech-writer already get acceptEdits unconditionally -- no
    write-grant is "forcing" anything for them, so no cwd-wide deny is
    warranted (they are the kinds that mode is supposed to serve)."""
    mod = _load_spawn_module()
    engine_grants = [{"path": "/somewhere/else", "mode": "write", "provenance": "declared"}]
    assert mod.write_grant_cwd_deny_rules("developer", engine_grants, str(tmp_path)) == []
    assert mod.write_grant_cwd_deny_rules("tech-writer", engine_grants, str(tmp_path)) == []


def test_write_grant_cwd_deny_rules_empty_without_a_write_grant(tmp_path):
    mod = _load_spawn_module()
    read_only_grants = [{"path": "/somewhere/else", "mode": "read", "provenance": "declared"}]
    assert mod.write_grant_cwd_deny_rules("planner", read_only_grants, str(tmp_path)) == []
    assert mod.write_grant_cwd_deny_rules("planner", None, str(tmp_path)) == []


def test_write_grant_cwd_deny_rules_empty_without_a_cwd():
    mod = _load_spawn_module()
    engine_grants = [{"path": "/somewhere/else", "mode": "write", "provenance": "declared"}]
    assert mod.write_grant_cwd_deny_rules("planner", engine_grants, None) == []


def test_build_child_settings_denies_cwd_for_forced_accept_edits_kind(tmp_path):
    """The deny actually reaches the child's settings payload for a
    non-trusted kind carrying a write grant OUTSIDE its own cwd."""
    mod = _load_spawn_module()
    write_dir = tmp_path / "elsewhere"
    write_dir.mkdir()
    engine_grants = [{"path": str(write_dir), "mode": "write", "provenance": "declared"}]
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    settings = mod.build_child_settings("planner", engine_grants=engine_grants, workdir=str(cwd))
    expected = f"Edit(//{str(cwd).lstrip('/')}/**)"
    assert expected in settings["permissions"]["deny"]


def test_build_child_settings_refuses_write_add_dir_under_its_own_forced_cwd_deny(tmp_path):
    """S1's other half: a non-trusted kind's write add_dir grant landing
    INSIDE its own cwd would be immediately shadowed by the cwd-wide deny
    `write_grant_cwd_deny_rules` just added -- `build_child_settings` must
    refuse this outright (via the existing `GrantShadowError` shadow check)
    rather than materialize a grant that silently does nothing."""
    mod = _load_spawn_module()
    cwd = tmp_path / "cwd"
    inner = cwd / "inner"
    inner.mkdir(parents=True)
    engine_grants = [{"path": str(inner), "mode": "write", "provenance": "declared"}]
    with pytest.raises(mod.GrantShadowError):
        mod.build_child_settings("planner", engine_grants=engine_grants, workdir=str(cwd))


def test_build_child_settings_developer_write_add_dir_under_cwd_is_not_shadowed(tmp_path):
    """The counterpart: `developer` is a trusted kind, so no cwd-wide deny is
    added for it, and a write add_dir under its own cwd is unaffected."""
    mod = _load_spawn_module()
    cwd = tmp_path / "cwd"
    inner = cwd / "inner"
    inner.mkdir(parents=True)
    engine_grants = [{"path": str(inner), "mode": "write", "provenance": "declared"}]
    settings = mod.build_child_settings("developer", engine_grants=engine_grants, workdir=str(cwd))
    deny = settings.get("permissions", {}).get("deny", [])
    cwd_wide = f"Edit(//{str(cwd).lstrip('/')}/**)"
    assert cwd_wide not in deny


def test_explicit_permission_mode_over_write_grant_warns_on_stderr(capsys):
    """S1's silent-drop nit: an explicit `--permission-mode` (default/plan)
    always wins over a write engine grant's forced acceptEdits, per
    resolve_permission_mode's own docstring -- but until now that silently
    discarded the grant's write access with no diagnostic anywhere."""
    mod = _load_spawn_module()
    args = argparse.Namespace(permission_mode="default", kind="planner")
    engine_grants = [{"path": "/tmp/some-dir", "mode": "write", "provenance": "declared"}]
    assert mod.resolve_permission_mode(args, engine_grants) == "default"
    captured = capsys.readouterr()
    assert "warning" in captured.err
    assert "write" in captured.err


def test_explicit_permission_mode_without_write_grant_is_silent(capsys):
    mod = _load_spawn_module()
    args = argparse.Namespace(permission_mode="plan", kind="developer")
    assert mod.resolve_permission_mode(args, None) == "plan"
    captured = capsys.readouterr()
    assert captured.err == ""
