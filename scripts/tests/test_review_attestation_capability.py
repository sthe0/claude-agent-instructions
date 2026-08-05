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
    mod = _load_spawn_module()
    args = argparse.Namespace(permission_mode=None, kind="thinker")
    assert mod.resolve_permission_mode(args) is None


def test_an_explicit_permission_mode_still_wins():
    mod = _load_spawn_module()
    args = argparse.Namespace(permission_mode="plan", kind="developer")
    assert mod.resolve_permission_mode(args) == "plan"
