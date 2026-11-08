"""Mechanism registry of the telemetry record: places, categories, severity bands, and the
registry of every mechanism that emits an event, derived from the live sources — never from
a hand list in the test."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from agentctl import cli
from lib import judge_ledger
from lib.telemetry import importer, sync, tables

SCRIPTS = Path(__file__).resolve().parents[1]
CATEGORIES_AT_R3 = {
    "review_budget_gate_friction", "check_authoring_defect", "flaky_or_loaded_verify", "effort_trigger_overrun",
    "implementation_defect", "wrong_premise_environment", "trunk_drift_landing", "spec_shift_or_underspecified",
    "parent_norm_defect", "dispatch_or_procedure_precondition", "other",
}
R5_COMPOSITES = {"order_phase", "plan_review_cycle", "effort_trigger", "landing_path", "telemetry"}
TELEMETRY_PARTS = {"recorder", "importer", "sync", "report"}


def _python_sources():
    return [p for p in sorted(SCRIPTS.rglob("*.py")) if "tests" not in p.parts and "__pycache__" not in p.parts]


def _desired_hook_stems() -> set:
    text = (SCRIPTS / "install-reminder-hooks.sh").read_text(encoding="utf-8")
    start = text.index("DESIRED = [")
    rows = ast.literal_eval(text[start + len("DESIRED = "):text.index("\n]\n", start) + 2])
    return {Path(row[2].split()[0]).stem for row in rows}


def _engine_gates() -> set:
    names = set()
    for path in (SCRIPTS / "agentctl").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_log_gate" and len(node.args) > 1
                    and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
                names.add(node.args[1].value)
    return names


def _literal_call_arguments(callee: str, position: int) -> set:
    values = set()
    for path in _python_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) == callee
                    and len(node.args) > position and isinstance(node.args[position], ast.Constant)
                    and isinstance(node.args[position].value, str)):
                values.add(node.args[position].value)
    return values


def _emitted_source_keys() -> set:
    keys = {f"cli:{verb}" for verb in cli.COMMANDS}
    keys |= {f"hook:{stem}" for stem in _desired_hook_stems()}
    for gate in _engine_gates():
        keys |= {f"gate:{gate}", f"hook:engine:{gate}"}
    keys |= {f"judge:{name}" for name in judge_ledger.JUDGE_NAMES}
    keys |= _literal_call_arguments("emit_event", 2)
    keys |= {f"land:{phase}" for phase in _literal_call_arguments("_emit_landing", 0)}
    keys |= {sync.SOURCE_KEY, importer.SELF_SOURCE_KEY}
    return keys


def _registry() -> dict:
    return tables.load("registry").members


def _judge_calling_hooks() -> set:
    spec = importlib.util.spec_from_file_location("judge_guard", SCRIPTS / "tests" / "test_judge_child_guard_coverage.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return {f"scripts/{p.name}" for p in sorted(SCRIPTS.glob("hook-*.py"))
            if guard._calls_a_judge(ast.parse(p.read_text(encoding="utf-8")))}


def test_categories_table_holds_r3_categories_and_other():
    assert set(tables.load("categories").members) == CATEGORIES_AT_R3


def test_severity_table_holds_bands_and_case_members():
    members = tables.load("severity").members
    bands = {n: m.attrs for n, m in members.items() if m.attrs["kind"] == "band"}
    cases = {n: m.attrs for n, m in members.items() if m.attrs["kind"] == "case"}
    assert {n: (a["from_minutes"], a.get("below_minutes"), a["mass"]) for n, a in bands.items()} == {
        "band_1": (0, 10, 1), "band_2": (10, 60, 2), "band_3": (60, None, 3)}
    assert {n: a.get("mass") for n, a in cases.items()} == {"never_closed": 3, "interval_unknown": 2, "open_running": None}


def test_part_of_is_a_list_and_acyclic():
    registry = _registry()
    for name, member in registry.items():
        parts = member.attrs["part_of"]
        assert isinstance(parts, list), name
        assert set(parts) <= set(registry), (name, parts)
        assert all(registry[p].attrs["kind"] == "composite" for p in parts), name

    def reaches(start: str, seen: tuple) -> None:
        for parent in registry[start].attrs["part_of"]:
            assert parent not in seen, f"cycle through {parent}: {seen}"
            reaches(parent, seen + (parent,))

    for name in registry:
        reaches(name, (name,))


def test_every_elementary_entry_records_its_source_keys():
    for name, member in _registry().items():
        keys = member.attrs.get("source_keys")
        if member.attrs["kind"] == "elementary":
            assert isinstance(keys, list) and keys and all(isinstance(k, str) and k for k in keys), name
        else:
            assert member.attrs["kind"] == "composite" and not keys, name


def test_every_emitted_source_key_resolves_to_one_entry():
    elementary = [m for m in _registry().values() if m.attrs["kind"] == "elementary"]
    for key in sorted(_emitted_source_keys()):
        owners = [m.name for m in elementary if key in m.attrs["source_keys"]]
        assert len(owners) == 1, (key, owners)
    declared = [k for m in elementary for k in m.attrs["source_keys"]]
    assert len(declared) == len(set(declared)), "a source key is claimed by two entries"
    assert "unknown" not in _registry(), "`unknown` is reserved for rows that name no registry member"


def test_session_end_hook_is_registered():
    stem = "hook-telemetry-session-end"
    assert stem in _desired_hook_stems()
    entry = _registry()[f"hook:{stem}"]
    assert entry.attrs["kind"] == "elementary" and entry.attrs["source_keys"] == [f"hook:{stem}"]


def test_registry_complete_over_hooks_gates_judges_commands():
    registry = _registry()
    expected = {f"hook:{stem}" for stem in _desired_hook_stems()}
    expected |= {f"gate:{gate}" for gate in _engine_gates()}
    expected |= {f"judge:{name}" for name in judge_ledger.JUDGE_NAMES}
    expected |= {f"cli:{verb}" for verb in cli.COMMANDS}
    assert expected <= set(registry), sorted(expected - set(registry))
    assert len(cli.COMMANDS) == len([n for n in registry if n.startswith("cli:")])
    callers = {module for name in judge_ledger.JUDGE_NAMES
               for module in registry[f"judge:{name}"].attrs["calling_modules"]}
    assert _judge_calling_hooks() <= callers, sorted(_judge_calling_hooks() - callers)
    for name in judge_ledger.JUDGE_NAMES:
        for module in registry[f"judge:{name}"].attrs["calling_modules"]:
            assert (SCRIPTS.parent / module).is_file(), (name, module)


def test_every_r5_composite_is_registered():
    composites = {n for n, m in _registry().items() if m.attrs["kind"] == "composite"}
    assert composites == R5_COMPOSITES


def test_every_composite_names_its_parts():
    registry = _registry()
    for composite in R5_COMPOSITES:
        parts = {n for n, m in registry.items() if composite in m.attrs["part_of"]}
        assert parts, f"{composite} has no part"
    assert registry["telemetry"].attrs["place"] == "telemetry"


def test_telemetry_mechanisms_are_registered():
    registry = _registry()
    parts = {n for n, m in registry.items() if "telemetry" in m.attrs["part_of"]}
    assert parts == {f"telemetry:{p}" for p in TELEMETRY_PARTS}
    assert {sync.SOURCE_KEY, importer.SELF_SOURCE_KEY, "telemetry:recorder"} <= {
        key for name in parts for key in registry[name].attrs["source_keys"]}


@pytest.mark.parametrize("name", sorted(R5_COMPOSITES))
def test_composite_place_is_a_places_member(name):
    assert _registry()[name].attrs["place"] in tables.load("places").members
