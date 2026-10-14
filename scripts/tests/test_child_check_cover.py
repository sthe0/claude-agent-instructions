"""A check a linked child runs is inside its parent stage's norm when a wildcard the parent holds covers it.

A child plan's engine-executed fixed texts (a stage's `verify_command` and `negative_control`,
a `final_check`) and its declared exact `Bash(<command>)` rules are commands the child's own
session runs. When the handoff holds a `Bash(<prefix>:*)` rule that covers such a text,
segment by segment, by the same matcher the harness-side cover uses
(`grants.bash_command_covered_by_rules`), the parent already authorizes the command: the
boundary no longer asks the handoff to carry the identical unresolved text, and drops the
resource a covered text alone produces (pytest's write on its venue). A text with a segment
the handoff does not cover, a segment whose target cannot be read off its text, or a
`Bash(P:*)` prefix that stops mid-word is not covered; a resource that any other origin
also produces, or that no approval could cover, is still charged.

Tests drive the engine through `agentctl` CLI argv (`cli.main`) against a real
`FileStateStore`, with the handoff written by `norm_handoff` as a spawn writes it; the table
test calls the matcher against `grants._bash_call_covered`. The five tests the stage's
negative control names as `--expect` drive only entry points the base tree has and put
`BEHAVIOUR:` on the assertion that checks the behaviour under repair, so on base they fail
there and not at collection or setup; the seven `--pass` tests pin what already holds on base.
"""
from __future__ import annotations

import os

import pytest

from agentctl import resources as agentctl_resources
from conftest import declare_venue
from test_child_approval import (
    PARENT_CHECK, Child, _assert_named, _autonomy, _covering_grants, _refusal, _spliced,
    _with_stage_grant, _with_stage_verify,
)

PYTEST_WILDCARD = "Bash(python3 -m pytest:*)"
SCRIPT_CHECK = "python3 -m unittest tests.test_mod"
OTHER_CHECK = "python3 -m unittest tests.test_other"
SCRIPT_WILDCARD = "Bash(python3 -m unittest:*)"
NARROW_WILDCARD = "Bash(python3 -m unittest tests.test_mod:*)"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
    monkeypatch.delenv("AGENTCTL_PARENT_DIFFICULTY", raising=False)
    monkeypatch.delenv("AGENTCTL_NORM_HANDOFF", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture
def child(tmp_path, monkeypatch):
    return Child(tmp_path, monkeypatch)


def _plan_text(child: Child, *, final_checks=((PARENT_CHECK, 0),)) -> str:
    return _spliced(parent_norm=child.digest, final_checks=final_checks)


def _with_stage_negative_control(text: str, command: str) -> str:
    marker = "depends_on = []\n"
    assert marker in text, "fixture changed: setup defect"
    return text.replace(
        marker, marker + f'negative_control = "{command}"\nexpected_negative_exit = 1\n', 1)


def _submit(child: Child, text: str, *, held=(), drop_venue_write: bool = False,
            venue=None) -> tuple[dict, str]:
    """Link the child to a handoff that covers every file resource of the plan (less the
    write on its venue when `drop_venue_write`) and holds the `held` rules, then submit."""
    plan = child.write(text) if venue is None else _write_at(child, text, venue)
    entries = _covering_grants(plan)
    if drop_venue_write:
        venue_path = agentctl_resources.FileResource(str(venue or child.tmp), "write").path
        entries = [e for e in entries
                   if not (e.get("path") == venue_path and e.get("mode") == "write")]
    child.link(grants=[*entries, *({"rule": rule} for rule in held)])
    return child.submit(plan), plan


def _write_at(child: Child, text: str, venue) -> str:
    path = child.tmp / "child.toml"
    path.write_text(declare_venue(text, venue), encoding="utf-8")
    return str(path)


def _assert_self_approves(d: dict, what: str) -> None:
    autonomy = _autonomy(d)
    assert autonomy.get("eligible") is True, (
        f"BEHAVIOUR: {what} is not inside the parent's norm: reasons={autonomy.get('reasons')} "
        f"unresolved={[c['source'] for c in autonomy.get('unresolved_changed_commands') or []]} "
        f"resources={[e['name'] for e in autonomy.get('extra_resources') or []]}"
    )
    assert autonomy.get("action") == "self_approve", autonomy


def _unresolved_sources(d: dict) -> list[str]:
    return [c["source"] for c in _autonomy(d).get("unresolved_changed_commands") or []]


# --- a check covered by a wildcard the handoff holds ----------------------------------


def test_stage_verify_covered_by_held_wildcard(child):
    text = _with_stage_verify(_plan_text(child), SCRIPT_CHECK)
    d, _ = _submit(child, text, held=[SCRIPT_WILDCARD])
    _assert_self_approves(d, "a stage verify_command under a wildcard the handoff holds")
    assert child.run("approve", session="child-1", by="agent")["ok"] is True


def test_negative_control_covered_by_held_wildcard(child):
    text = _with_stage_negative_control(
        _with_stage_verify(_plan_text(child), "git status"), SCRIPT_CHECK)
    d, _ = _submit(child, text, held=[SCRIPT_WILDCARD])
    _assert_self_approves(d, "a stage negative_control under a wildcard the handoff holds")


def test_final_check_covered_by_held_wildcard(child):
    text = _plan_text(child, final_checks=((PARENT_CHECK, 0), (SCRIPT_CHECK, 0)))
    d, _ = _submit(child, text, held=[SCRIPT_WILDCARD])
    _assert_self_approves(d, "a final_check under a wildcard the handoff holds")


def test_declared_exact_bash_rule_covered_by_held_wildcard(child):
    text = _with_stage_grant(_plan_text(child), f"Bash({SCRIPT_CHECK})")
    d, _ = _submit(child, text, held=[SCRIPT_WILDCARD])
    _assert_self_approves(d, "a declared exact Bash rule under a wildcard the handoff holds")


def test_pytest_stage_check_covered_by_held_pytest_wildcard(child):
    text = _with_stage_verify(_plan_text(child), "python3 -m pytest -q -x tests/test_mod.py")
    d, _ = _submit(child, text, held=[PYTEST_WILDCARD], drop_venue_write=True)
    _assert_self_approves(d, "pytest's write on its venue under a pytest wildcard the handoff holds")
    assert child.run("approve", session="child-1", by="agent")["ok"] is True


# --- what stays refused ---------------------------------------------------------------


def test_check_outside_parent_grants_still_refused(child):
    text = _with_stage_verify(_plan_text(child), OTHER_CHECK)
    d, _ = _submit(child, text, held=[NARROW_WILDCARD])
    _assert_named(d, "unresolved-not-in-handoff")
    assert OTHER_CHECK in _unresolved_sources(d)
    _refusal(child, "unresolved-not-in-handoff")


def test_pytest_check_without_held_pytest_wildcard_still_refused(child):
    text = _with_stage_verify(_plan_text(child), "python3 -m pytest -q -x tests/test_mod.py")
    d, _ = _submit(child, text, held=[SCRIPT_WILDCARD], drop_venue_write=True)
    _assert_named(d, "resource-not-in-handoff")
    _refusal(child, "resource-not-in-handoff")


def test_compound_check_with_uncovered_segment_refused(child):
    text = _with_stage_verify(_plan_text(child), f"{SCRIPT_CHECK} && {OTHER_CHECK}")
    d, _ = _submit(child, text, held=[NARROW_WILDCARD])
    _assert_named(d, "unresolved-not-in-handoff")
    _refusal(child, "unresolved-not-in-handoff")


def test_unresolvable_segment_never_covered(child):
    check = f"{SCRIPT_CHECK} $HOME"
    text = _with_stage_verify(_plan_text(child), check)
    d, _ = _submit(child, text, held=[SCRIPT_WILDCARD])
    _assert_named(d, "unresolved-not-in-handoff")
    assert check in _unresolved_sources(d)


def test_prefix_without_word_boundary_not_covered(child):
    text = _with_stage_verify(_plan_text(child), SCRIPT_CHECK)
    d, _ = _submit(child, text, held=["Bash(python3 -m unit:*)"])
    _assert_named(d, "unresolved-not-in-handoff")
    assert SCRIPT_CHECK in _unresolved_sources(d)


def test_protected_venue_write_of_covered_pytest_check_still_refused(child, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    text = _with_stage_verify(_plan_text(child), "python3 -m pytest -q -x tests/test_mod.py")
    d, _ = _submit(child, text, held=[PYTEST_WILDCARD], drop_venue_write=True, venue=home)
    _assert_named(d, "resource-not-in-handoff")
    names = [e["name"] for e in _autonomy(d).get("extra_resources") or []]
    assert any(str(home) in n for n in names), names


def _second_origin_declared(text: str) -> str:
    return _with_stage_grant(text, "Bash(python3 -m pytest -q tests/test_mod.py)")


def _second_origin_uncovered_check(text: str) -> str:
    return _with_stage_negative_control(text, "python3 -m pytest -x tests/test_mod.py")


def _second_origin_output_artifact(text: str) -> str:
    marker = 'output_artifacts = ["mod.py"]'
    assert marker in text, "fixture changed: setup defect"
    return text.replace(marker, 'output_artifacts = ["mod.py", "."]', 1)


@pytest.mark.parametrize("second_origin", [
    pytest.param(_second_origin_declared, id="declared-rule"),
    pytest.param(_second_origin_uncovered_check, id="uncovered-check"),
    pytest.param(_second_origin_output_artifact, id="output-artifact"),
])
def test_shared_venue_resource_still_charged_when_another_origin_produces_it(child, second_origin):
    held = ["Bash(python3 -m pytest -q:*)"]
    text = second_origin(_with_stage_verify(_plan_text(child), "python3 -m pytest -q -x tests/test_mod.py"))
    d, _ = _submit(child, text, held=held, drop_venue_write=True)
    _assert_named(d, "resource-not-in-handoff")
    venue = agentctl_resources.FileResource(str(child.tmp), "write").path
    names = [e["name"] for e in _autonomy(d).get("extra_resources") or []]
    assert any(venue in n for n in names), names


# --- one matcher ----------------------------------------------------------------------

#: (rules, command, covered), written from the rule's specification: a segment is covered by
#: `Bash(P:*)` iff it is P or starts with P and a space, by an exact `Bash(C)` iff it is C;
#: a command is covered iff it splits into segments and every one is covered.
MATCHER_CASES = [
    (["Bash(git status:*)"], "git status", True),
    (["Bash(git status:*)"], "git status -s", True),
    (["Bash(git status:*)"], "git statuses", False),
    (["Bash(git status)"], "git status", True),
    (["Bash(git status)"], "git status -s", False),
    (["Bash(git status:*)"], "git status && git diff", False),
    (["Bash(git status:*)", "Bash(git diff:*)"], "git status && git diff HEAD", True),
    (["Bash(git status:*)", "Bash(git diff:*)"], "git status; git diff | git log", False),
    (["Bash(git status:*)"], "git status $(true)", False),
    (["Bash(git status:*)"], "git status > out.txt", False),
    (["Bash(git status:*)"], "git status 'unbalanced", False),
    (["Bash(git status:*)"], "", False),
    ([], "git status", False),
    (["Edit(//tmp/x)", "Bash(git status:*)"], "git status", True),
    (["Edit(//tmp/x)"], "git status", False),
    (["Bash(python3 -m pytest -q:*)"], "python3 -m pytest -q tests/test_mod.py", True),
    (["Bash(python3 -m pytest -q:*)"], "python3 -m pytest -x tests/test_mod.py", False),
]


@pytest.mark.parametrize("rules,command,covered", MATCHER_CASES)
def test_boundary_cover_agrees_with_harness_matcher(rules, command, covered):
    from agentctl import grants

    harness = grants._bash_call_covered(
        grants.StageGrants(allow=[grants.RuleGrant(rule, "declared") for rule in rules]), command)
    assert harness is covered, f"the harness-side cover judged {command!r} under {rules} as {harness}"
    assert grants.bash_command_covered_by_rules(rules, command) is covered, (
        f"the shared matcher judged {command!r} under {rules} otherwise than the harness-side cover")
