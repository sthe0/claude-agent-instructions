"""The unparsable-input contract of the denying hooks.

Difficulty removed: a denying hook that allows because it could not read the call it was shown turns the
OpenCode adapter's refusal into a pass, and an edit to such a hook must not move a verdict Claude Code can see.
``docs/operations/hook-unparsable-input-inventory.toml`` names every such branch of every denying hook; the
two hooks the user decided on refuse at theirs. This file holds the inventory complete, the left rows
unreachable from the bridge, and the verdicts on recorded Claude payloads equal before and after.

``fixtures/claude_hook_payloads/expected_verdicts.json`` holds the verdict of each fixture from the hooks
as ``HOOK_CONTRACT_BASE_REF`` has them. To record it again, set ``HOOK_CONTRACT_RECORD=1`` and run
``test_record_expected_verdicts`` on a checkout whose two hooks are those of the base ref.
"""
from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import hook_contract_runner as runner  # noqa: E402

BASE_REF = os.environ.get("HOOK_CONTRACT_BASE_REF", "refs/cognition-opencode/stage-6-start")


import hook_parity  # noqa: E402
import opencode_events as oe  # noqa: E402

INVENTORY = REPO / "docs" / "operations" / "hook-unparsable-input-inventory.toml"
EDITED_HOOKS = ("hook-guard-canon-readonly.py", "hook-guard-permission-surface.py")
ROW_FIELDS = ("hook", "branch", "location", "field", "fed_by", "reachable_from_claude", "disposition", "reason")
FED_BY = ("tool_argument", "payload", "stdin", "state", "environment")
REFUSAL_MARKS = ('"deny"', "'deny'", "permissionDecision", "sys.exit(2)", "return 2")


def _inventory() -> dict:
    return tomllib.loads(INVENTORY.read_text(encoding="utf-8"))


def _refused_rows() -> list[dict]:
    return [r for r in _inventory()["row"] if r["disposition"] == "refused"]


def _base_ref_or_skip() -> None:
    import subprocess
    found = subprocess.run(["git", "-C", str(REPO), "rev-parse", "-q", "--verify", BASE_REF],
                           capture_output=True, text=True)
    if found.returncode != 0:
        pytest.skip(f"{BASE_REF} is not set in this checkout")


def test_inventory_covers_every_denying_hook():
    wired = {row["script"] for row in hook_parity.installer_rows() if hook_parity.is_denying(row["event"])}
    inventory = _inventory()
    declared = {h["script"]: h["denies"] for h in inventory["hook"]}
    assert set(declared) == wired, (sorted(set(declared) ^ wired))
    with_rows = {r["hook"] for r in inventory["row"]}
    assert with_rows <= set(declared), sorted(with_rows - set(declared))
    for script, denies in declared.items():
        source = (SCRIPTS / script).read_text(encoding="utf-8")
        refusing = [m for m in REFUSAL_MARKS if m in source]
        if denies:
            assert script in with_rows, f"{script} can refuse a call and has no inventory row"
        else:
            assert script not in with_rows and not refusing, (script, refusing)


def test_inventory_rows_state_their_branch_and_their_reason():
    for row in _inventory()["row"]:
        missing = [f for f in ROW_FIELDS if row.get(f) in (None, "")]
        assert not missing, (row.get("hook"), row.get("branch"), missing)
        assert row["fed_by"] in FED_BY and isinstance(row["reachable_from_claude"], bool), row
        assert row["disposition"] in ("refused", "left"), row
        if row["disposition"] == "refused":
            assert row["hook"] in EDITED_HOOKS and not row["reachable_from_claude"], row
            assert row["fed_by"] == "tool_argument" and (runner.FIXTURES / row["fixture"]).is_file(), row
        else:
            assert row.get("bridge_call") or row.get("bridge_note"), row


def test_every_refused_row_names_an_unreadable_fixture_of_its_hook():
    for row in _refused_rows():
        assert row["fixture"].startswith("unreadable/"), row
        assert runner.load_fixture(row["fixture"])["hook"] == row["hook"], row
    refused_hooks = {r["hook"] for r in _refused_rows()}
    assert refused_hooks == set(EDITED_HOOKS)
    for name in runner.fixture_names():
        if name.startswith("unreadable/"):
            assert runner.load_fixture(name)["hook"] in refused_hooks, name


def test_left_inventory_rows_refused_by_bridge():
    rows = [r for r in _inventory()["row"]
            if r["disposition"] == "left" and r["fed_by"] == "tool_argument" and not r["reachable_from_claude"]]
    assert rows
    for row in rows:
        call = row.get("bridge_call")
        assert call, f"{row['hook']}: {row['branch']} is fed by a tool argument and has no bridge_call"
        with pytest.raises(oe.UnclassifiedArgument):
            oe.claude_input(call["tool"], call["args"], "/tmp")


def test_expected_verdicts_match_base_ref(tmp_path):
    _base_ref_or_skip()
    recorded = json.loads(runner.EXPECTED.read_text(encoding="utf-8"))
    assert recorded["base_ref"] == BASE_REF
    base_scripts = runner.scripts_at_ref(BASE_REF, tmp_path / "base")
    assert runner.verdicts(base_scripts, tmp_path / "run") == recorded["verdicts"]


def test_claude_payload_verdicts_unchanged(tmp_path):
    recorded = json.loads(runner.EXPECTED.read_text(encoding="utf-8"))["verdicts"]
    now = runner.verdicts(SCRIPTS, tmp_path)
    claude = {n: v for n, v in recorded.items() if n.startswith("claude/")}
    assert claude and {n: now[n] for n in claude} == claude


def test_unreadable_payloads_are_refused_by_the_two_hooks_and_were_allowed_before(tmp_path):
    recorded = json.loads(runner.EXPECTED.read_text(encoding="utf-8"))["verdicts"]
    unreadable = [n for n in recorded if n.startswith("unreadable/")]
    assert unreadable
    now = runner.verdicts(SCRIPTS, tmp_path)
    for name in unreadable:
        assert recorded[name] == "allow", (name, recorded[name])
        assert now[name] == "deny", (name, now[name])


def test_record_expected_verdicts(tmp_path):
    if os.environ.get("HOOK_CONTRACT_RECORD") != "1":
        pytest.skip("recording is opt-in: HOOK_CONTRACT_RECORD=1")
    recorded = {"base_ref": BASE_REF, "verdicts": runner.verdicts(SCRIPTS, tmp_path)}
    runner.EXPECTED.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8")
