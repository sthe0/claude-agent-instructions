"""Tests for the Core hook parity table (integrations/opencode/parity.toml) and its checker.

The table's rows are pinned equal to the installer's ``DESIRED`` list, the class of a row is
computed from its event, and the strict rules S1-S4 are shown to fire on one-row fixtures
(``scripts/tests/fixtures/hook_parity/``), each a copy of the Core table with one change.
"""
from __future__ import annotations

import copy
import difflib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from lib import hook_parity  # noqa: E402

CLI = SCRIPTS / "hook-parity.py"
FIXTURES = SCRIPTS / "tests" / "fixtures" / "hook_parity"
ALL_PATHS = ["root:shell", "root:write", "root:edit", "root:read", "root:glob", "root:grep", "root:webfetch",
             "subagent:task", "root:execute", "root:execute-inner", "root:skill", "mcp:probesrv:touch",
             "plugin-tool:probe_tool", "child-run", "root:question"]

# fixture -> (script the findings name, strict rule id that must appear on that script's line)
FIXTURE_FINDINGS = {
    "denying_exempt": ("hook-guard-destructive-rm.py", "S1"),
    "denying_none": ("hook-guard-destructive-rm.py", "S1"),
    "denying_missing_mcp_path": ("hook-guard-destructive-rm.py", "S2"),
    "denying_unproven_mechanism": ("hook-guard-destructive-rm.py", "S3"),
    "denying_k7_gap": ("hook-guard-destructive-rm.py", "S2"),
    "denying_child_run_switched_off": ("hook-guard-destructive-rm.py", "S2"),
    "denying_mechanism_not_on_path": ("hook-guard-destructive-rm.py", "S2"),
    "nondenying_unfired": ("hook-telemetry-session-end.py", "S3"),
    "nondenying_none_unexempted": ("hook-reaper.py", "S4"),
    "nondenying_mechanism_not_fired_on_path": ("hook-self-critique-reminder.py", "S4"),
    "nondenying_missing_executed_path": ("hook-self-critique-reminder.py", "S4"),
}
# Fixtures whose one hunk is in the probe record's tables, not in a row: the rows relying on it are named.
HEADER_FIXTURES = {"nondenying_unfired", "nondenying_mechanism_not_fired_on_path"}


@pytest.fixture(scope="module")
def real() -> dict:
    return hook_parity.load_table(hook_parity.CORE_TABLE)


@pytest.fixture(scope="module")
def wiring() -> list[dict]:
    return hook_parity.installer_rows()


def _row(table: dict, script: str, event: str | None = None) -> dict:
    return next(r for r in table["row"] if r["script"] == script and (event is None or r["event"] == event))


def _cli(*args: str) -> tuple[int, list[str]]:
    done = subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout.splitlines() + done.stderr.splitlines()


def _strict_fixture(name: str) -> tuple[int, list[str]]:
    return _cli("check", "--layer", "core", "--strict", "--table", str(FIXTURES / f"{name}.toml"))


def _assert_strict_refuses(name: str) -> None:
    script, rule = FIXTURE_FINDINGS[name]
    code, lines = _strict_fixture(name)
    assert code == 1, lines
    assert any(line.startswith(rule + " ") and script in line for line in lines), lines


def _rules(findings) -> set[str]:
    return {f.rule for f in findings}


def test_rows_match_installer_events(real, wiring):
    keys = {hook_parity.row_key(r) for r in real["row"]}
    assert len(real["row"]) == len(keys) == len(wiring) == 44
    assert keys == {hook_parity.row_key(w) for w in wiring}
    assert hook_parity.check_layer().findings == []


def test_only_tool_refusing_events_denying(wiring):
    assert hook_parity.is_denying("PreToolUse") and hook_parity.is_denying("PermissionRequest")
    for event in ("UserPromptSubmit", "PostToolUse", "Stop", "SessionStart", "SessionEnd", "SubagentStop"):
        assert not hook_parity.is_denying(event)
    assert {w["event"] for w in wiring if hook_parity.is_denying(w["event"])} == {"PreToolUse"}


def test_class_from_event_advisory_pretooluse_denying(real, wiring):
    table = copy.deepcopy(real)
    row = _row(table, "hook-term-neutrality.py")
    assert hook_parity.is_denying(row["event"])
    row["exempt"] = {"reason": "advisory", "covers_gap": "none"}
    assert "S1" in _rules(hook_parity.check_rows(table, wiring, strict=True))


def test_difficulty_required(real, wiring):
    for mutate in (lambda r: r.pop("difficulty"), lambda r: r.update(difficulty="  ")):
        table = copy.deepcopy(real)
        mutate(_row(table, "hook-reaper.py"))
        found = hook_parity.check_rows(table, wiring)
        assert [f.rule for f in found] == ["C21"] and found[0].script == "hook-reaper.py"


def test_exemption_requires_reason_and_gap(real, wiring):
    table = copy.deepcopy(real)
    row = _row(table, "hook-reaper.py")
    row["exempt"] = {}
    assert [f.rule for f in hook_parity.check_rows(table, wiring)] == ["C23", "C23"]
    row["exempt"] = {"reason": "why", "covers_gap": "which gap"}
    assert hook_parity.check_rows(table, wiring) == []
    _row(table, "hook-state-gate.py")["exempt"] = {"reason": "why", "covers_gap": "which gap"}
    assert _rules(hook_parity.check_rows(table, wiring)) == {"C23"}


def test_unknown_execution_path_fails(real, wiring):
    table = copy.deepcopy(real)
    row = _row(table, "hook-state-gate.py")
    row["paths"] = row["paths"] + ["root:bogus"]
    row["gaps"] = {"root:other-bogus": "switched_off:x"}
    found = hook_parity.check_rows(table, wiring)
    assert [f.rule for f in found] == ["C25", "C25"]


def test_denying_row_covers_every_path_or_names_gap(real, wiring):
    table = copy.deepcopy(real)
    row = _row(table, "hook-state-gate.py")
    row["paths"] = [p for p in row["paths"] if p != "root:webfetch"]
    found = hook_parity.check_rows(table, wiring, strict=True)
    assert [f.rule for f in found] == ["S2"] and "root:webfetch" in found[0].message
    row["gaps"] = {"root:webfetch": "switched_off:webfetch_tool"}
    assert hook_parity.check_rows(table, wiring, strict=True) == []
    _assert_strict_refuses("denying_missing_mcp_path")


def test_mechanism_must_be_proven(real, wiring):
    table = copy.deepcopy(real)
    _row(table, "hook-reaper.py")["opencode"] = "no.such.mechanism"
    assert _rules(hook_parity.check_rows(table, wiring)) == {"C24"}
    table = copy.deepcopy(real)
    next(m for m in table["probe_mechanisms"] if m["mechanism"] == "SessionStart")["fired"] = False
    found = hook_parity.check_rows(table, wiring, strict=True)
    assert _rules(found) == {"S3"} and {f.script for f in found} >= {"hook-reaper.py", "hook-session-due.py"}
    _assert_strict_refuses("denying_unproven_mechanism")


def test_strict_fixture_one_row_exempted_refused():
    _assert_strict_refuses("denying_exempt")
    code, lines = _cli("check", "--layer", "core", "--table", str(FIXTURES / "denying_exempt.toml"))
    assert code == 1 and any(line.startswith("C23 ") for line in lines)


def test_strict_fixture_mapped_none_refused():
    _assert_strict_refuses("denying_none")
    code, _ = _cli("check", "--layer", "core", "--table", str(FIXTURES / "denying_none.toml"))
    assert code == 0


def test_strict_k7_gap_refused():
    _assert_strict_refuses("denying_k7_gap")


def test_strict_child_run_gap_refused():
    _assert_strict_refuses("denying_child_run_switched_off")


def test_strict_mechanism_not_refusing_on_path_refused():
    _assert_strict_refuses("denying_mechanism_not_on_path")


def test_strict_nondenying_unfired_refused():
    _assert_strict_refuses("nondenying_unfired")


def test_strict_nondenying_not_fired_on_path_refused():
    _assert_strict_refuses("nondenying_mechanism_not_fired_on_path")
    _, lines = _strict_fixture("nondenying_mechanism_not_fired_on_path")
    assert len(lines) == 6 and all("PostToolUse" in line for line in lines)


def test_strict_nondenying_missing_executed_path_refused():
    _assert_strict_refuses("nondenying_missing_executed_path")


def test_nondenying_none_requires_exemption(real, wiring):
    _assert_strict_refuses("nondenying_none_unexempted")
    table = copy.deepcopy(real)
    row = _row(table, "hook-reaper.py")
    row["opencode"] = "none"
    row["exempt"] = {"reason": "no carrier", "covers_gap": "reaping is not a gate"}
    assert hook_parity.check_rows(table, wiring, strict=True) == []


def test_strict_nondenying_fired_passes(real, wiring):
    assert hook_parity.check_layer(strict=True).findings == []
    child = next(p for p in real["probe_paths"] if p["id"] == "child-run")
    assert "fired_by" not in child
    for row in real["row"]:
        if row["event"] == "PostToolUse":
            assert "child-run" not in row["paths"] and len(row["paths"]) == len(ALL_PATHS) - 1
        if hook_parity.is_denying(row["event"]):
            assert row["paths"] == ALL_PATHS and "exempt" not in row and "gaps" not in row


@pytest.mark.parametrize("name", sorted(FIXTURE_FINDINGS))
def test_every_fixture_fails_strict_and_differs_by_one_hunk(name, real):
    _assert_strict_refuses(name)
    base = hook_parity.CORE_TABLE.read_text(encoding="utf-8").splitlines()
    fixture = (FIXTURES / f"{name}.toml").read_text(encoding="utf-8").splitlines()
    ops = [op for op in difflib.SequenceMatcher(None, base, fixture, autojunk=False).get_opcodes()
           if op[0] != "equal"]
    assert len(ops) == 1, ops
    _, i1, i2, _, _ = ops[0]
    assert not any(line.startswith("[[") for line in base[i1:i2])
    heading = next(line for line in reversed(base[:i1 + 1]) if line.startswith("[["))
    assert heading == ("[[probe_paths]]" if name == "nondenying_mechanism_not_fired_on_path"
                       else "[[probe_mechanisms]]" if name == "nondenying_unfired" else "[[row]]")
    assert (name in HEADER_FIXTURES) == (heading != "[[row]]")


def test_all_fixtures_are_covered():
    assert {p.stem for p in FIXTURES.glob("*.toml")} == set(FIXTURE_FINDINGS)


def test_real_table_passes_strict_cli():
    code, lines = _cli("check", "--layer", "core", "--strict")
    assert code == 0 and lines == ["hook-parity: layer core ok (strict)"]


def test_probe_paths_match_record(real):
    try:
        paths, mechanisms = hook_parity.pinned_probe_tables()
    except (RuntimeError, OSError, ImportError) as error:
        pytest.skip(f"no pinned mechanisms record on this machine: {error}")
    assert real["probe_paths"] == paths
    assert real["probe_mechanisms"] == mechanisms
    assert len(real["probe_paths"]) == len(paths) == len(ALL_PATHS)
    assert [p["id"] for p in paths] == ALL_PATHS


# ---- per-layer tables and the session-start notice ----

def _toml_value(value) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
    return json.dumps(value)


def _layer_table(rows: list[dict]) -> str:
    header = '[[probe_mechanisms]]\nmechanism = "SessionStart"\nfired = true\ncan_refuse = false\n'
    parts = [header]
    for r in rows:
        parts.append("[[row]]\n" + "".join(f"{k} = {_toml_value(v)}\n" for k, v in r.items()))
    return "\n".join(parts)


def _row_for(script: str, **extra) -> dict:
    return {"script": script, "event": "SessionStart", "timeout_s": 5, "opencode": "session.created",
            "paths": [], "difficulty": "a difficulty", **extra}


def _settings(path: Path, *scripts: str) -> Path:
    hooks = {"SessionStart": [{"hooks": [{"type": "command", "command": f"/x/{s}", "timeout": 5}
                                         for s in scripts]}]} if scripts else {}
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return path


def _storage(tmp_path: Path, name: str, scripts: list[str], rows: list[dict] | None) -> Path:
    storage = tmp_path / name
    storage.mkdir()
    _settings(storage / "settings.json", *scripts)
    if rows is not None:
        (storage / hook_parity.TABLE_NAME).write_text(_layer_table(rows), encoding="utf-8")
    return storage


def _agent_home(tmp_path: Path, monkeypatch, *scripts: str) -> Path:
    home = tmp_path / "agent-home"
    home.mkdir()
    _settings(home / "settings.json", *scripts)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def test_per_layer_tables(tmp_path):
    root = _storage(tmp_path, "layer", ["a.py", "b.py"], [_row_for("a.py"), _row_for("b.py")])
    assert hook_parity.check_layer(root, strict=True).findings == []
    (root / hook_parity.TABLE_NAME).write_text(_layer_table([_row_for("a.py")]), encoding="utf-8")
    found = hook_parity.check_layer(root).findings
    assert [(f.rule, f.script) for f in found] == [("C20", "b.py")]


def test_layer_root_without_table_fails(tmp_path):
    root = _storage(tmp_path, "layer", ["a.py"], None)
    found = hook_parity.check_layer(root).findings
    assert [f.rule for f in found] == ["C22"]
    code, lines = _cli("check", "--layer-root", str(root))
    assert code == 1 and lines[0].startswith("C22 ")


def test_zero_row_table_accepted(tmp_path):
    root = _storage(tmp_path, "layer", [], [])
    assert hook_parity.check_layer(root, strict=True).findings == []
    root = _storage(tmp_path, "wired", ["a.py"], [])
    assert [f.rule for f in hook_parity.check_layer(root).findings] == ["C20"]


def test_session_start_notice_names_exemptions(tmp_path, monkeypatch):
    home = _agent_home(tmp_path, monkeypatch)
    exempt = {"reason": "not a gate", "covers_gap": "nothing refuses here"}
    root = _storage(tmp_path, "layer", ["a.py"], [_row_for("a.py", opencode="none", exempt=exempt)])
    lines = hook_parity.start_notice([home, root])
    assert [l for l in lines if l.startswith("parity: exempt a.py")] == [
        f"parity: exempt a.py (SessionStart) in layer {root}: not a gate; covers gap: nothing refuses here"]


def test_session_start_names_mismatched_layer(tmp_path, monkeypatch):
    home = _agent_home(tmp_path, monkeypatch)
    root = _storage(tmp_path, "layer", ["a.py", "b.py"], [_row_for("a.py")])
    lines = hook_parity.start_notice([home, root])
    assert len(lines) == 1 and lines[0].startswith(f"parity: layer {root} fails check --strict (1 findings")
    assert "b.py" in lines[0]


def test_session_start_names_tableless_layer(tmp_path, monkeypatch):
    home = _agent_home(tmp_path, monkeypatch)
    root = _storage(tmp_path, "layer", ["a.py"], None)
    assert hook_parity.start_notice([home, root]) == [f"parity: layer {root} has no hook-parity.toml (tableless)"]


def test_third_party_hook_noticed_not_carried(tmp_path, monkeypatch):
    home = _agent_home(tmp_path, monkeypatch, "hook-state-gate.py", "vendor-hook.py")
    lines = hook_parity.start_notice([home])
    assert lines == [f"parity: third-party hook vendor-hook.py (SessionStart) in {home / 'settings.json'} "
                     "is not carried to OpenCode"]


def test_start_notice_resolves_composed_root_to_storage_tables(tmp_path, monkeypatch):
    home = _agent_home(tmp_path, monkeypatch)
    storage = _storage(tmp_path, "storage", ["a.py", "b.py"], [_row_for("a.py")])
    composed = tmp_path / "composed"
    composed.mkdir()
    (composed / "settings.json").symlink_to(storage / "settings.json")
    lines = hook_parity.start_notice([home, composed])
    assert len(lines) == 1 and f"layer {storage} fails" in lines[0] and str(composed) not in lines[0]


def test_start_notice_agent_home_is_core_layer(tmp_path, monkeypatch):
    home = _agent_home(tmp_path, monkeypatch, "hook-state-gate.py")
    assert hook_parity.start_notice([home]) == []
    monkeypatch.setattr(hook_parity, "CORE_TABLE", _storage(tmp_path, "bad", [], [_row_for("x.py")])
                        / hook_parity.TABLE_NAME)
    lines = hook_parity.start_notice([home])
    assert lines and lines[0].startswith("parity: layer core fails check --strict")


def test_start_notice_without_agent_home_says_unscanned(tmp_path, monkeypatch):
    _agent_home(tmp_path, monkeypatch)
    root = _storage(tmp_path, "layer", ["a.py"], [_row_for("a.py")])
    assert hook_parity.start_notice([root]) == [
        "parity: the agent home is not among the layer roots; its settings.json was not scanned for "
        "third-party hooks"]
