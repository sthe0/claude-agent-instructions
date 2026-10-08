"""spawn-specialist.py --kind manager: the empty specialization.

The overcome-difficulty escape is a vanilla depth n+1 manager. Spawned through
the wrapper it keeps the depth cap, budget ceiling and telemetry, but receives
no role SKILL.md and no appended marker protocol, and answers in the escape's
own vocabulary (RESOLVED / INVESTIGATION / LOOP_DETECTED / PERMISSION-REQUEST)
without widening the marker set any other kind validates against.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_manager_kind", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _argv(kind: str, brief: Path) -> list[str]:
    return [
        "--kind", kind,
        "--plan", str(brief),
        "--done-criterion", "difficulty resolved",
        "--criterion-type", "acceptance-review",
        "--complexity", "low",
        "--effort", "low",
        "--dry-run",
    ]


@pytest.fixture
def brief(tmp_path) -> Path:
    path = tmp_path / "brief.txt"
    path.write_text("Difficulty: expected X, actual Y.\n", encoding="utf-8")
    return path


@pytest.fixture
def mod(tmp_path, monkeypatch):
    m = _load()
    skills = tmp_path / "skills"
    for kind in ("developer", "manager"):
        (skills / kind).mkdir(parents=True)
        (skills / kind / "SKILL.md").write_text(f"# {kind} role\n", encoding="utf-8")
    monkeypatch.setattr(m, "SKILLS_DIR", skills)
    monkeypatch.setattr(m, "log_refused", lambda *a, **k: None)
    monkeypatch.delenv("AGENTCTL_RUNTIME_HOST", raising=False)
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", "1")
    monkeypatch.chdir(tmp_path)
    return m


def test_manager_dry_run_with_plain_brief_exits_zero(mod, brief, capsys):
    assert mod.main(_argv("manager", brief)) == 0
    out = capsys.readouterr().out
    assert "Difficulty: expected X, actual Y." in out
    assert "AGENT_RECURSION_DEPTH=2" in out


def test_manager_command_carries_no_system_prompt_file(mod, brief, capsys):
    assert mod.main(_argv("manager", brief)) == 0
    command = capsys.readouterr().out.split("=== command (not executed) ===")[1]
    assert "--append-system-prompt-file" not in command


def test_other_kind_still_gets_composed_system_prompt_file(mod, brief, capsys):
    assert mod.main(_argv("developer", brief)) == 0
    command = capsys.readouterr().out.split("=== command (not executed) ===")[1]
    assert "--append-system-prompt-file" in command


def test_manager_prompt_does_not_point_at_the_specialist_marker_protocol(mod, brief, capsys):
    assert mod.main(_argv("manager", brief)) == 0
    out = capsys.readouterr().out
    assert "§ Return markers" not in out
    assert "CLARIFY" not in out


def test_manager_is_refused_at_the_depth_cap(mod, brief, monkeypatch, capsys):
    cap = mod.recursion_max(mod.parse_config_md())
    monkeypatch.setenv("AGENT_RECURSION_DEPTH", str(cap))
    assert mod.main(_argv("manager", brief)) == 3
    assert "max-recursion-depth" in capsys.readouterr().err


def test_resolved_validates_for_manager_but_is_malformed_for_developer(mod):
    from lib.planner_plan_check import check_planner_return

    text = "Found the cause.\nRESOLVED: switch the gate to containment.\n"
    forwarded, ok, marker = check_planner_return(text, "manager")
    assert ok and marker == "RESOLVED"
    forwarded, ok, marker = check_planner_return(text, "developer")
    assert not ok and marker is None
    assert forwarded.startswith("MALFORMED:")


def test_permission_request_validates_for_manager(mod):
    from lib.planner_plan_check import check_planner_return

    text = "Need to push.\nPERMISSION-REQUEST:\nAction: push\n"
    _, ok, marker = check_planner_return(text, "manager")
    assert ok and marker == "PERMISSION-REQUEST"


def test_specialist_only_marker_is_malformed_for_manager(mod):
    from lib.planner_plan_check import check_planner_return

    _, ok, marker = check_planner_return("COMPLETED: done\n", "manager")
    assert not ok and marker is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("**RESOLVED:** fixed\n", "RESOLVED"),
        ("`INVESTIGATION: partial`\n", "INVESTIGATION"),
        ("Summary first.\n## LOOP_DETECTED: same task\n", "LOOP_DETECTED"),
        ("INVESTIGATION: early\nmore\n- RESOLVED: final\n", "RESOLVED"),
        ("no marker here\n", None),
    ],
)
def test_kind_extractor_keeps_extract_marker_contract(mod, text, expected):
    from lib.planner_plan_check import MANAGER_RETURN_MARKERS, extract_kind_marker, extract_marker

    assert extract_kind_marker(text, MANAGER_RETURN_MARKERS) == expected
    swapped = text.replace(expected, "COMPLETED") if expected else text
    assert extract_marker(swapped) == ("COMPLETED" if expected else None)


def test_global_marker_set_is_unchanged():
    from lib.planner_plan_check import RETURN_MARKERS

    assert not {"RESOLVED", "INVESTIGATION", "LOOP_DETECTED"} & set(RETURN_MARKERS)


def test_extraction_hint_for_manager_is_its_own_marker_set():
    from lib import marker_extract
    from lib.planner_plan_check import MANAGER_RETURN_MARKERS

    assert marker_extract.hint_markers_for("manager") == MANAGER_RETURN_MARKERS


def test_stray_manager_skill_md_is_never_loaded(mod, brief, capsys):
    assert (mod.SKILLS_DIR / "manager" / "SKILL.md").exists()
    assert mod.main(_argv("manager", brief)) == 0
    command = capsys.readouterr().out.split("=== command (not executed) ===")[1]
    assert "skill-composed-" not in command
