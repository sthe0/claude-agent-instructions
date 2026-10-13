"""Tests for hook-guard-agent-specialist-stand-in.py — deny an Agent/Task call whose
prompt or description names a `skills/.../SKILL.md` path.

Every test drives the hook as a subprocess through its stdin/stdout contract, so the
mutation proof can break the production matcher and see the test go RED.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hook-guard-agent-specialist-stand-in.py"


def _run(stdin: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(HOOK)], input=stdin,
                          capture_output=True, text=True, timeout=30)
    return proc.returncode, proc.stdout


def _call(tool_name: str = "Agent", **tool_input) -> tuple[int, str]:
    return _run(json.dumps({"tool_name": tool_name, "tool_input": tool_input}))


def _denied(stdout: str) -> dict:
    out = json.loads(stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    return out


@pytest.mark.parametrize("tool_name", ["Agent", "Task"])
def test_denies_prompt_naming_a_skill_md_path(tool_name):
    code, stdout = _call(
        tool_name,
        prompt="Read ~/.claude-agent/skills/specializations/planner/SKILL.md and act as it.",
        subagent_type="general-purpose",
    )
    assert code == 0
    reason = _denied(stdout)["permissionDecisionReason"]
    assert "spawn-specialist.py" in reason and "Skill" in reason
    assert "skills/specializations/planner/SKILL.md" in reason


def test_denies_top_level_skill_path_in_description():
    code, stdout = _call(description="follow skills/self-improvement/SKILL.md",
                         prompt="do the thing")
    assert code == 0
    _denied(stdout)


def test_denies_a_specialist_skills_dir_path_whatever_the_subagent_type():
    code, stdout = _call(
        prompt="Your role file: skills/specializations/developer/SKILL.md. Implement stage 2.",
        subagent_type="Explore",
    )
    assert code == 0
    _denied(stdout)


@pytest.mark.parametrize("prompt", [
    "Find every caller of resolve_command under scripts/ and report file:line.",
    "Poll the job status every minute and report transitions.",
    "Summarize how skills are indexed; do not edit anything.",
    "Read scripts/README.md and list the hook scripts.",
])
def test_allows_ordinary_exploration_and_polling(prompt):
    code, stdout = _call(prompt=prompt, description="explore", subagent_type="Explore")
    assert code == 0
    assert stdout == ""


@pytest.mark.parametrize("prompt", [
    "Read the SKILL.md of the planner and act as it.",
    "Open skills/*/SKILL.md and list the headings.",
])
def test_allows_a_bare_skill_md_name_and_a_glob(prompt):
    code, stdout = _call(prompt=prompt, subagent_type="Explore")
    assert (code, stdout) == (0, "")


def test_denies_task_with_the_path_only_in_description():
    code, stdout = _call("Task", description="act per skills/specializations/thinker/SKILL.md",
                         prompt="go")
    assert code == 0
    _denied(stdout)


def test_allows_a_non_agent_tool_naming_a_skill_path():
    code, stdout = _call("Bash", command="cat skills/specializations/planner/SKILL.md")
    assert (code, stdout) == (0, "")
    code, stdout = _call("Read", file_path="skills/developer/SKILL.md")
    assert (code, stdout) == (0, "")


@pytest.mark.parametrize("stdin", [
    "",
    "not json",
    "[]",
    "null",
    "{}",
    json.dumps({"tool_name": "Agent"}),
    json.dumps({"tool_name": "Agent", "tool_input": None}),
    json.dumps({"tool_name": "Agent", "tool_input": {"prompt": 7, "description": ["x"]}}),
    json.dumps({"tool_name": "Agent", "tool_input": "skills/a/SKILL.md"}),
])
def test_never_raises_and_allows_on_empty_or_garbage_payload(stdin):
    code, stdout = _run(stdin)
    assert code == 0
    assert stdout == ""
