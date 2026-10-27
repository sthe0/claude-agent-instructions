"""The resource-grant PreToolUse hook (`scripts/hook-resource-grant-check.py`): activation, the kill
switch, each decision class, per-segment rule matching, the decision log and the failure modes.

The opaque-code judge is always a stub; no `claude` process is started. The cases of the denial
fixture are replayed through the same entry point in `test_resource_grant_replay.py`.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import resource_grant_hook_support as support
import test_tool_contracts_segments as seg_tests
from agentctl import config as agentctl_config
from agentctl.dispatch import RunResult
from lib import config_root
from test_resource_grant_replay import JudgeStub

CONSTANTS = {"resource-grant-hook": "on", "resource-grant-hook-timeout-s": "20", "resource-grant-judge-timeout-s": "15"}
ROW_FIELDS = {"ts", "tool", "call_sha", "decision", "reason_class", "resources", "venue", "matched"}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("CDPATH", raising=False)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH", "AGENTCTL_RESOURCE_HOOK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))


@pytest.fixture(scope="module")
def hook():
    return support.load_hook()


@pytest.fixture(scope="module")
def spawn():
    return support.load_spawn()


class Rig:
    def __init__(self, hook, spawn, tmp_path):
        self.hook, self.spawn, self.tmp = hook, spawn, tmp_path
        self.venue = seg_tests._init_repo(tmp_path / "venue", "feat")
        seg_tests._git(self.venue, "remote", "add", "origin", str(tmp_path / "origin"))
        self.outside = tmp_path / "outside"
        self.outside.mkdir()
        self.scratch = tmp_path / support.PARENT_SESSION / "scratchpad"
        self.scratch.mkdir(parents=True)

    def child(self, **kw):
        kw.setdefault("branch", "feat")
        kw.setdefault("allow", [])
        return support.link_child(self.spawn, self.tmp / "state", self.venue, **kw)

    def call(self, child, tool="Bash", *, env=None, judge=None, constants=CONSTANTS, **tool_input):
        payload = child.payload(tool, **tool_input)
        answer = self.hook.run(payload, env or child.env, runner=judge or JudgeStub([]),
                               cache_dir=self.tmp / "judge-cache", constants=constants)
        decision = json.loads(answer)["hookSpecificOutput"]["permissionDecision"] if answer else "none"
        rows = child.log_rows()
        return decision, (rows[-1] if rows else None), answer

    def bash(self, child, command, **kw):
        return self.call(child, "Bash", command=command, **kw)


@pytest.fixture
def rig(hook, spawn, tmp_path):
    return Rig(hook, spawn, tmp_path)


# --- activation and the kill switch ---------------------------------------------------------------

def test_inactive_without_a_recursion_depth_writes_nothing(rig):
    child = rig.child()
    env = {k: v for k, v in child.env.items() if k != "AGENT_RECURSION_DEPTH"}
    decision, row, answer = rig.bash(child, f"touch {rig.outside}/f", env=env)
    assert (decision, answer, row) == ("none", "", None)
    assert not (Path(os.environ["CLAUDE_AGENT_HOME"]) / "agentctl").exists()


def test_inactive_without_a_handoff_record_is_logged_unlinked(rig):
    child = rig.child()
    decision, _, answer = rig.bash(child, f"touch {rig.outside}/f", env={"AGENT_RECURSION_DEPTH": "1"})
    assert (decision, answer) == ("none", "")
    log = config_root.agentctl_state_dir() / "resource-uses" / "_unlinked.jsonl"
    assert json.loads(log.read_text().splitlines()[-1])["reason_class"] == "inactive"


def test_inactive_when_the_record_bytes_do_not_match_the_digest(rig):
    child = rig.child()
    path, digest = child.env["AGENTCTL_NORM_HANDOFF"].rsplit(":", 1)
    Path(path).write_text(Path(path).read_text() + " ")
    decision, _, answer = rig.bash(child, f"touch {rig.outside}/f")
    assert (decision, answer) == ("none", "")


def test_config_row_off_is_the_kill_switch(rig):
    child = rig.child()
    decision, row, _ = rig.bash(child, f"touch {rig.outside}/f", constants={**CONSTANTS, "resource-grant-hook": "off"})
    assert (decision, row["reason_class"]) == ("none", "kill-switch")


def test_env_zero_is_the_kill_switch_and_env_one_beats_the_row(rig):
    child = rig.child()
    decision, row, _ = rig.bash(child, f"touch {rig.outside}/f", env={**child.env, "AGENTCTL_RESOURCE_HOOK": "0"})
    assert (decision, row["reason_class"]) == ("none", "kill-switch")
    decision, row, _ = rig.bash(child, f"touch {rig.outside}/f", env={**child.env, "AGENTCTL_RESOURCE_HOOK": "1"},
                                constants={**CONSTANTS, "resource-grant-hook": "off"})
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")


def test_config_md_carries_both_hook_rows():
    values = agentctl_config.parse_config_md()
    assert values["resource-grant-hook"] == "on"
    assert agentctl_config.resource_grant_hook_timeout_s() == int(values["resource-grant-hook-timeout-s"]) == 20


# --- Bash decisions -------------------------------------------------------------------------------

def test_covered_push_of_the_declared_branch_is_allowed_and_logged(rig):
    decision, row, _ = rig.bash(rig.child(), "git push origin feat")
    assert (decision, row["reason_class"]) == ("allow", "covered")
    assert row["tool"] == "Bash" and row["venue"] == str(rig.venue)
    assert {"kind": "vcs_ref", "op": "push", "ref": "feat", "remote": "origin"} in row["resources"]


def test_commit_under_a_wildcard_grant_matches_the_rule_pattern(rig):
    rule = f"Bash(git -C {rig.venue} commit:*)"
    decision, row, _ = rig.bash(rig.child(rules=[rule]), f"git -C {rig.venue} commit -q -m x")
    assert (decision, row["reason_class"], row["matched"]) == ("allow", "covered", [rule])


def test_uncovered_write_is_denied_with_a_permission_request_template(rig):
    child = rig.child()
    decision, row, answer = rig.bash(child, f"touch {rig.outside}/f")
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")
    reason = json.loads(answer)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "PERMISSION-REQUEST:" in reason and f"Resource: file write {rig.outside}/f" in reason
    assert f"Rule: Bash(touch {rig.outside}/f)" in reason


def test_a_push_of_an_unapproved_branch_is_denied(rig):
    child = rig.child()
    decision, row, _ = rig.bash(child, "git push origin HEAD:refs/heads/main")
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")
    assert {"kind": "vcs_ref", "op": "push", "ref": "main", "remote": "origin"} in row["resources"]


def test_unresolved_segment_makes_no_decision(rig):
    child = rig.child()
    decision, row, _ = rig.bash(child, 'echo "$(date)"')
    assert (decision, row["reason_class"]) == ("none", "undeterminable")


def test_allow_entry_matching_the_segment_leaves_it_to_the_harness(rig):
    entry = f"Bash(touch {rig.outside}/f)"
    child = rig.child(allow=[entry])
    decision, row, answer = rig.bash(child, f"touch {rig.outside}/f")
    assert (decision, answer, row["reason_class"], row["matched"]) == ("none", "", "harness-allowed", [entry])


def test_only_a_kind_baseline_entry_matches_a_write_the_record_lacks(rig):
    todays = support.link_child(rig.spawn, rig.tmp / "state-1", rig.venue, branch=None, mode="acceptEdits")
    command = "git commit -q -m x"
    decision, row, _ = rig.bash(todays, command)
    assert (decision, row["reason_class"], row["matched"]) == ("none", "harness-allowed", ["Bash(git commit:*)"])
    bare = support.link_child(rig.spawn, rig.tmp / "state-2", rig.venue, branch=None, mode="acceptEdits", allow=[])
    decision, row, _ = rig.bash(bare, command)
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")


def test_a_commit_on_a_branch_the_record_does_not_declare_is_denied(rig):
    child = rig.child(branch="other", allow=[])
    decision, row, _ = rig.bash(child, "git commit -q -m x")
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")
    assert {"kind": "vcs_ref", "op": "commit", "ref": "feat", "remote": "."} in row["resources"]


@pytest.mark.parametrize("joiner", ["&&", "||", ";", "|", "\n"])
def test_an_allow_entry_for_the_prefix_leaves_the_tail_to_the_harness(rig, joiner):
    child = rig.child(allow=["Bash(git status:*)"])
    decision, row, answer = rig.bash(child, f"git status {joiner} touch {rig.outside}/f")
    assert (decision, answer, row["reason_class"]) == ("none", "", "uncovered-write")


@pytest.mark.parametrize("joiner", ["&&", "||", ";", "|"])
def test_an_entry_matching_no_part_of_the_call_does_not_stop_the_deny(rig, joiner):
    child = rig.child(allow=["Bash(git log:*)"])
    decision, row, _ = rig.bash(child, f"git status {joiner} touch {rig.outside}/f")
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")


def test_an_allow_entry_matching_a_segment_with_a_redirect_leaves_it_to_the_harness(rig):
    child = rig.child(allow=["Bash(git status:*)"])
    decision, row, _ = rig.bash(child, f"git status > {rig.outside}/f")
    assert (decision, row["reason_class"]) == ("none", "uncovered-write")


def test_b2_a_dollar_and_a_stderr_redirect_do_not_turn_an_allowed_call_into_a_deny(rig):
    entry = "Bash(git commit:*)"
    child = rig.child(allow=[entry], branch="other")
    command = f"git commit -q -m 'cost $5' 2>&1 && touch {rig.outside}/f"
    decision, row, answer = rig.bash(child, command)
    assert (decision, answer, row["reason_class"]) == ("none", "", "uncovered-write")
    bare = rig.child(allow=[], branch="other")
    assert rig.bash(bare, command)[0] == "deny"


def test_deny_needs_the_accept_edits_mode(rig):
    child = rig.child(mode="default")
    decision, row, answer = rig.bash(child, f"touch {rig.outside}/f")
    assert (decision, answer, row["reason_class"]) == ("none", "", "uncovered-write")
    child.mode = "acceptEdits"
    assert rig.bash(child, f"touch {rig.outside}/f")[0] == "deny"


def test_s1_plan_mode_makes_no_decision(rig):
    child = rig.child()
    child.mode = "plan"
    decision, row, answer = rig.bash(child, f"touch {rig.outside}/f")
    assert (decision, answer, row["reason_class"]) == ("none", "", "plan-mode")


def test_mode_that_already_runs_the_write_suppresses_the_deny(rig):
    child = rig.child()
    decision, row, _ = rig.bash(child, "touch f")
    assert (decision, row["reason_class"], row["matched"]) == ("none", "harness-allowed", ["mode:acceptEdits"])
    assert rig.bash(child, f"touch {rig.outside}/f")[0] == "deny"


def test_b1_a_write_into_a_directory_the_child_was_started_with_is_not_denied(rig):
    evidence = rig.tmp / "evidence"
    evidence.mkdir()
    child = rig.child(evidence_dir=str(evidence))
    assert str(evidence) in child.record["add_dirs"]
    decision, row, answer = rig.bash(child, f"touch {evidence}/note.txt")
    assert (decision, answer, row["reason_class"]) == ("none", "", "uncovered-write")
    assert rig.bash(child, f"touch {rig.outside}/f")[0] == "deny"
    plan_dir = child.record["add_dirs"][0]
    assert rig.bash(child, f"touch {plan_dir}/own.toml")[0] == "none"


def test_b3_a_record_without_the_explicit_lists_never_denies(rig):
    child = rig.child()
    assert rig.bash(child, f"touch {rig.outside}/f")[0] == "deny"
    for key in ("permissions_allow", "add_dirs"):
        old = rig.child()
        path, _digest = old.env["AGENTCTL_NORM_HANDOFF"].rsplit(":", 1)
        record = json.loads(Path(path).read_text())
        record.pop(key)
        stale = support.norm_handoff.write_record(rig.tmp / f"state-without-{key}", record)
        old.env["AGENTCTL_NORM_HANDOFF"] = support.norm_handoff.format_env_value(*stale)
        decision, row, answer = rig.bash(old, f"touch {rig.outside}/f")
        assert (decision, answer, row["reason_class"]) == ("none", "", "uncovered-write"), key


def test_b3_the_state_root_comes_from_the_handoff_path(rig):
    child = rig.child()
    path, _digest = child.env["AGENTCTL_NORM_HANDOFF"].rsplit(":", 1)
    assert support.norm_handoff.state_root_of(path) == child.state_root


def test_rule_pattern_covers_the_segment_it_matches(rig):
    child = rig.child(extra_patterns=["Bash(python3 -m pytest:*)"])
    decision, row, _ = rig.bash(child, f"python3 -m pytest -q {rig.venue}/tests")
    assert (decision, row["reason_class"], row["matched"]) == ("allow", "covered", ["Bash(python3 -m pytest:*)"])


def test_rule_pattern_is_matched_per_segment_not_per_call(rig):
    child = rig.child(extra_patterns=["Bash(python3 -m pytest:*)"])
    decision, row, _ = rig.bash(child, f"python3 -m pytest -q && touch {rig.outside}/f")
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")
    assert row["matched"] == ["Bash(python3 -m pytest:*)"]


def test_rule_pattern_does_not_match_a_redirect_or_a_longer_word(rig):
    child = rig.child(extra_patterns=["Bash(python3 -m pytest:*)"])
    for command in (f"python3 -m pytest -q > {rig.outside}/f", f"python3 -m pytestx {rig.outside}/f"):
        decision, row, _ = rig.bash(child, command)
        assert decision != "allow" and "Bash(python3 -m pytest:*)" not in row["matched"], command


def test_a_read_only_segment_reads_only_inside_the_approved_resources(rig):
    (rig.scratch / "note.txt").write_text("x")
    (rig.outside / "secret.txt").write_text("x")
    child = rig.child()
    decision, row, _ = rig.bash(child, f"cat {rig.scratch}/note.txt")
    assert (decision, row["reason_class"], row["matched"]) == ("allow", "covered", ["parent-scratchpad"])
    decision, row, _ = rig.bash(child, f"cat {rig.outside}/secret.txt")
    assert (decision, row["reason_class"]) == ("none", "undeterminable")
    (rig.venue / "a.txt").write_text("x")
    decision, row, _ = rig.bash(child, f"cat {rig.venue}/a.txt")
    assert (decision, row["matched"]) == ("allow", ["record-resource"])
    assert rig.bash(child, "cat ~/.ssh/id_rsa")[0] == "none"


def test_a_scratchpad_symlink_to_the_outside_is_not_scratchpad(rig):
    (rig.outside / "secret.txt").write_text("x")
    (rig.scratch / "link").symlink_to(rig.outside)
    child = rig.child()
    assert rig.bash(child, f"cat {rig.scratch}/link/secret.txt")[0] == "none"
    assert rig.call(child, "Read", file_path=f"{rig.scratch}/link/secret.txt")[0] == "none"


def test_a_scratchpad_layout_outside_the_scratch_roots_is_not_scratchpad(rig, monkeypatch):
    elsewhere = rig.tmp / "elsewhere" / support.PARENT_SESSION / "scratchpad"
    elsewhere.mkdir(parents=True)
    (elsewhere / "n.txt").write_text("x")
    monkeypatch.setattr(rig.hook, "scratch_roots", lambda: {str(rig.tmp / "scratch-root")})
    child = rig.child()
    assert rig.bash(child, f"cat {elsewhere}/n.txt")[0] == "none"
    assert rig.call(child, "Read", file_path=f"{elsewhere}/n.txt")[0] == "none"


def test_scratchpad_of_another_session_is_not_the_parents(rig):
    other = rig.tmp / "other-session" / "scratchpad"
    other.mkdir(parents=True)
    (other / "n.txt").write_text("x")
    assert rig.bash(rig.child(), f"cat {other}/n.txt")[0] == "none"


# --- opaque code ----------------------------------------------------------------------------------

STDIN_CODE = "python3 - <<'E'\nimport json\nprint(json.dumps({}))\nE"


def test_opaque_code_whose_judged_effects_are_covered_is_allowed(rig):
    judge = JudgeStub([{"kind": "file", "op": "write", "path": f"{rig.venue}/out.txt"}])
    decision, row, _ = rig.bash(rig.child(), STDIN_CODE, judge=judge)
    assert (decision, row["reason_class"]) == ("allow", "opaque-judged") and judge.calls == 1


def test_s3_opaque_code_that_writes_nothing_is_not_allowed_on_its_reads(rig):
    judge = JudgeStub([])
    decision, row, answer = rig.bash(rig.child(), STDIN_CODE, judge=judge)
    assert (decision, answer, row["reason_class"]) == ("none", "", "undeterminable") and judge.calls == 1


def test_s2_the_judge_gets_the_judge_timeout_and_one_deadline_for_the_whole_call(rig, hook, monkeypatch):
    seen = []

    def judge(argv, *, timeout, stdin=""):
        seen.append(timeout)
        return RunResult(0, json.dumps({"effects": [{"kind": "file", "op": "write", "path": f"{rig.venue}/o"}],
                                        "undeterminable": False, "reason": "x"}), "", timed_out=False)

    monkeypatch.setattr(hook.advisor, "code_effects_judge_timeout_s", lambda: 7)
    rig.bash(rig.child(), STDIN_CODE, judge=judge, constants={**CONSTANTS, "resource-grant-hook-timeout-s": "20"})
    assert seen == [7]
    rig.bash(rig.child(), STDIN_CODE.replace("json", "csv"), judge=judge,
             constants={**CONSTANTS, "resource-grant-hook-timeout-s": "8"})
    assert seen[-1] == 2
    assert hook.JudgeBudget(total_s=3, judge_s=15).next_timeout() <= 3
    assert hook.JudgeBudget(total_s=0, judge_s=15).next_timeout() < 1


def test_s2_a_spent_deadline_makes_the_next_opaque_segment_undeterminable(rig, hook, monkeypatch):
    calls = []
    monkeypatch.setattr(hook.JudgeBudget, "next_timeout", lambda self: 0)
    decision, row, _ = rig.bash(rig.child(), STDIN_CODE, judge=lambda *a, **k: calls.append(1))
    assert (decision, row["reason_class"], calls) == ("none", "undeterminable", [])


def test_opaque_code_whose_judged_effect_is_uncovered_is_denied(rig):
    effect = {"kind": "file", "op": "write", "path": f"{rig.outside}/f"}
    decision, row, _ = rig.bash(rig.child(), STDIN_CODE, judge=JudgeStub([effect]))
    assert (decision, row["reason_class"]) == ("deny", "uncovered-write")
    assert {"kind": "file", "mode": "write", "path": f"{rig.outside}/f"} in row["resources"]


def test_a_judge_that_fails_is_never_an_allow(rig):
    def broken(argv, *, timeout, stdin=""):
        return RunResult(1, "", "boom", timed_out=True)

    decision, row, answer = rig.bash(rig.child(), STDIN_CODE, judge=broken)
    assert (decision, answer, row["reason_class"]) == ("none", "", "undeterminable")


def test_python_environment_in_the_hook_process_makes_opaque_code_undeterminable(rig, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(rig.outside))
    judge = JudgeStub([])
    decision, row, _ = rig.bash(rig.child(), STDIN_CODE, judge=judge)
    assert (decision, row["reason_class"]) == ("none", "undeterminable") and judge.calls == 0


# --- B4: variables and hooks the hook cannot verify -------------------------------------------------

def test_b4_a_cd_into_a_relative_directory_is_undeterminable_because_cdpath_is_unknown(rig):
    (rig.venue / "sub").mkdir()
    decision, row, answer = rig.bash(rig.child(), "cd sub && touch f")
    assert (decision, answer, row["reason_class"]) == ("none", "", "undeterminable")
    decision, row, _ = rig.bash(rig.child(), f"cd {rig.venue}/sub && touch f")
    assert (decision, row["reason_class"]) == ("allow", "covered")


@pytest.mark.parametrize("name", ["GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"])
def test_b4_a_git_variable_in_the_environment_makes_a_git_segment_undeterminable(rig, name):
    child = rig.child()
    decision, row, _ = rig.bash(child, "git push origin feat")
    assert (decision, row["reason_class"]) == ("allow", "covered")
    decision, row, answer = rig.bash(child, "git push origin feat", env={**child.env, name: str(rig.outside)})
    assert (decision, answer, row["reason_class"]) == ("none", "", "undeterminable")


def test_b4_a_python_variable_in_the_environment_makes_a_python_segment_undeterminable(rig):
    child = rig.child()
    command = f"python3 {rig.venue}/tool.py"
    (rig.venue / "tool.py").write_text("print(1)\n")
    judge = JudgeStub([{"kind": "file", "op": "write", "path": f"{rig.venue}/o"}])
    assert rig.bash(child, command, judge=judge)[0] == "allow"
    decision, row, _ = rig.bash(child, command, env={**child.env, "PYTHONSTARTUP": str(rig.outside / "s.py")}, judge=judge)
    assert (decision, row["reason_class"]) == ("none", "undeterminable")
    decision, row, _ = rig.bash(child, command, env={**child.env, "PYTHONUNBUFFERED": "1"}, judge=judge)
    assert decision == "allow"


def test_b4_a_commit_whose_hooks_directory_the_stage_may_write_is_not_covered(rig):
    seg_tests._git(rig.venue, "config", "core.hooksPath", "githooks")
    hooks = rig.venue / "githooks"
    hooks.mkdir()
    child = rig.child()
    decision, row, _ = rig.bash(child, "git commit -q -m x")
    assert (decision, row["reason_class"]) == ("allow", "covered")
    (hooks / "pre-commit").write_text("#!/bin/sh\ntouch /tmp/x\n")
    decision, row, answer = rig.bash(child, "git commit -q -m x")
    assert (decision, answer, row["reason_class"]) == ("none", "", "undeterminable")
    (hooks / "pre-commit").unlink()
    decision, row, _ = rig.bash(child, f"printf x > {hooks}/pre-commit && git commit -q -m x")
    assert (decision, row["reason_class"]) == ("none", "undeterminable")


def test_b4_a_hooks_directory_outside_the_writable_resources_does_not_block(rig):
    elsewhere = rig.outside / "hooks"
    elsewhere.mkdir()
    (elsewhere / "pre-commit").write_text("#!/bin/sh\n")
    seg_tests._git(rig.venue, "config", "core.hooksPath", str(elsewhere))
    decision, row, _ = rig.bash(rig.child(), "git commit -q -m x")
    assert (decision, row["reason_class"]) == ("allow", "covered")


# --- Edit, Write, Read ----------------------------------------------------------------------------

@pytest.mark.parametrize("tool, key", [("Edit", "file_path"), ("Write", "file_path"), ("MultiEdit", "file_path"),
                                       ("NotebookEdit", "notebook_path")])
def test_edit_tools_allow_a_covered_file_and_never_deny(rig, tool, key):
    child = rig.child(mode="acceptEdits")
    decision, row, _ = rig.call(child, tool, **{key: str(rig.venue / "a.py")})
    assert (decision, row["reason_class"], row["matched"]) == ("allow", "covered", ["record-resource"])
    decision, row, answer = rig.call(child, tool, **{key: str(rig.outside / "a.py")})
    assert (decision, answer, row["reason_class"]) == ("none", "", "uncovered-write")


def test_edit_of_a_relative_path_is_resolved_against_the_cwd(rig):
    decision, _, _ = rig.call(rig.child(), "Write", file_path="sub/a.py", content="x")
    assert decision == "allow"
    assert rig.call(rig.child(), "Write", file_path="../outside/a.py", content="x")[0] == "none"


def test_read_is_allowed_only_for_record_resources_and_the_parent_scratchpad(rig):
    child = rig.child()
    assert rig.call(child, "Read", file_path=str(rig.scratch / "n.txt"))[1]["matched"] == ["parent-scratchpad"]
    assert rig.call(child, "Read", file_path=str(rig.venue / "a.py"))[0] == "allow"
    decision, row, answer = rig.call(child, "Read", file_path=str(rig.outside / "a.py"))
    assert (decision, answer, row["reason_class"]) == ("none", "", "undeterminable")
    assert rig.call(child, "Read", file_path=str(rig.venue.parent / "other" / ".." / "outside" / "a.py"))[0] == "none"


def test_other_tools_make_no_decision(rig):
    decision, row, _ = rig.call(rig.child(), "Glob", pattern="*")
    assert (decision, row["reason_class"]) == ("none", "undeterminable")


# --- the decision log and failures ----------------------------------------------------------------

def test_every_call_appends_one_complete_row_per_reason_class(rig):
    child = rig.child(rules=[f"Bash(git -C {rig.venue} commit:*)"])
    rig.bash(child, f"git -C {rig.venue} commit -q -m x")
    rig.bash(child, f"touch {rig.outside}/f")
    rig.bash(child, 'echo "$(date)"')
    rig.bash(child, STDIN_CODE, judge=JudgeStub([{"kind": "file", "op": "write", "path": f"{rig.venue}/o"}]))
    rig.bash(child, "git status", env=child.env, constants={**CONSTANTS, "resource-grant-hook": "off"})
    harness = rig.child(allow=["Bash(git status:*)"])
    rig.bash(harness, "git status")
    rows = child.log_rows() + [r for r in harness.log_rows() if r["reason_class"] == "harness-allowed"]
    assert {r["reason_class"] for r in rows} == {"covered", "uncovered-write", "undeterminable", "opaque-judged",
                                                 "kill-switch", "harness-allowed"}
    assert all(set(r) == ROW_FIELDS for r in rows)
    assert all(len(r["call_sha"]) == 64 for r in rows)


def test_a_hook_error_is_logged_and_never_an_allow(rig, hook, monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "_approved", explode)
    decision, row, answer = rig.bash(rig.child(), f"touch {rig.outside}/f")
    assert (decision, answer, row["reason_class"]) == ("none", "", "error")


def test_malformed_input_is_ignored(rig, hook):
    child = rig.child()
    assert hook.run("not json", child.env) == ""
    assert hook.run("[]", child.env) == ""


# --- the script entry point -----------------------------------------------------------------------

def _run_script(rig, child, payload, extra_env=None):
    env = {**os.environ, **child.env, **(extra_env or {})}
    return subprocess.run([sys.executable, str(support.HOOK_FILE)], input=payload, env=env, text=True,
                          capture_output=True, timeout=60)


def test_script_denies_through_stdout_and_exits_zero(rig):
    child = rig.child()
    done = _run_script(rig, child, child.payload("Bash", command=f"touch {rig.outside}/f"))
    answer = json.loads(done.stdout)["hookSpecificOutput"]
    assert (done.returncode, answer["hookEventName"], answer["permissionDecision"]) == (0, "PreToolUse", "deny")
    assert child.log_rows()[-1]["decision"] == "deny"


def test_script_with_unreadable_stdin_exits_zero_silently(rig):
    done = _run_script(rig, rig.child(), "{")
    assert (done.returncode, done.stdout) == (0, "")


def test_script_ignores_pythonpath_shadowing_agentctl(rig):
    shadow = rig.tmp / "shadow"
    (shadow / "agentctl").mkdir(parents=True)
    (shadow / "agentctl" / "__init__.py").write_text("raise SystemExit(9)\n")
    child = rig.child()
    done = _run_script(rig, child, child.payload("Bash", command=f"touch {rig.outside}/f"),
                       {"PYTHONPATH": str(shadow)})
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- registration in the child's --settings -------------------------------------------------------

def _dry_run_command_line(out: str) -> str:
    return out.split("=== command (not executed) ===", 1)[1].strip().splitlines()[0]


def _printed_argument(command_line: str, flag: str) -> str:
    """The value following `flag` in the `--dry-run` command line (quoted items are Python reprs)."""
    import ast

    head, found, rest = command_line.partition(f"{flag} ")
    assert found, f"{flag} not in the command line"
    if rest[0] not in "'\"":
        return rest.split(" ", 1)[0]
    end = 1
    while rest[end] != rest[0]:
        end += 2 if rest[end] == "\\" else 1
    return ast.literal_eval(rest[: end + 1])


def engine_dry_run(spawn, store, fixtures, tmp_path, monkeypatch, capsys, session):
    """Drive a real engine session to EXECUTING and print `spawn-specialist.py --dry-run` for stage 1."""
    from test_stage_grants import _to_executing, _write_declared_grants_plan

    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(spawn, "plans_dir", lambda: plans)
    plan = _write_declared_grants_plan(fixtures, plans)
    _to_executing(store, session, fixtures, plan_path=plan)
    argv = ["--kind", "developer", "--plan", plan, "--done-criterion", "tests green", "--criterion-type", "measurable",
            "--complexity", "medium", "--effort", "high", "--session", session, "--stage-index", "1",
            "--state-root", str(tmp_path / "state"), "--dry-run"]
    assert spawn.main(argv) == 0
    return capsys.readouterr().out


def test_dry_run_registers_the_hook_with_the_configured_timeout(spawn, store, venued_fixtures_dir, tmp_path,
                                                                monkeypatch, capsys):
    out = engine_dry_run(spawn, store, venued_fixtures_dir, tmp_path, monkeypatch, capsys, "hook-registration")
    command_line = _dry_run_command_line(out)
    settings = json.loads(_printed_argument(command_line, "--settings"))
    [group] = settings["hooks"]["PreToolUse"]
    [entry] = group["hooks"]
    assert group["matcher"] == spawn.RESOURCE_GRANT_HOOK_MATCHER
    assert entry["timeout"] == agentctl_config.resource_grant_hook_timeout_s()
    assert entry["command"].split()[:2] == ["python3", "-I"] and str(support.HOOK_FILE) in entry["command"]
    assert "Bash(git status:*)" in settings["permissions"]["allow"]
    add_dirs = re.findall(r"--add-dir (\S+)", command_line)
    assert add_dirs
    print("child payload:", json.dumps({"hook": group, "allow": settings["permissions"]["allow"],
                                         "add_dirs": add_dirs}, sort_keys=True))


def test_b5_the_record_the_spawn_writes_carries_the_childs_final_allow_list_and_directories(
        spawn, store, venued_fixtures_dir, tmp_path, monkeypatch, capsys):
    out = engine_dry_run(spawn, store, venued_fixtures_dir, tmp_path, monkeypatch, capsys, "hook-record")
    command_line = _dry_run_command_line(out)
    settings = json.loads(_printed_argument(command_line, "--settings"))
    record = json.loads(out.split("=== norm handoff record (not written) ===", 1)[1].strip().splitlines()[0])
    assert record["permissions_allow"] == settings["permissions"]["allow"] and record["permissions_allow"]
    assert record["add_dirs"] == re.findall(r"--add-dir (\S+)", command_line)


def test_dry_run_of_an_unlinked_spawn_registers_no_hook(spawn, venued_fixtures_dir, tmp_path, monkeypatch, capsys):
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    argv = ["--kind", "developer", "--plan", str(plan), "--done-criterion", "tests green",
            "--criterion-type", "measurable", "--complexity", "medium", "--effort", "high", "--dry-run"]
    assert spawn.main(argv) == 0
    settings = json.loads(_printed_argument(_dry_run_command_line(capsys.readouterr().out), "--settings"))
    assert support.HOOK_FILE.name not in json.dumps(settings.get("hooks", {}))


def test_dry_run_registers_nothing_when_the_kill_switch_is_off(spawn, store, venued_fixtures_dir, tmp_path,
                                                               monkeypatch, capsys):
    monkeypatch.setenv("AGENTCTL_RESOURCE_HOOK", "0")
    out = engine_dry_run(spawn, store, venued_fixtures_dir, tmp_path, monkeypatch, capsys, "hook-kill-switch")
    settings = json.loads(_printed_argument(_dry_run_command_line(out), "--settings"))
    assert support.HOOK_FILE.name not in json.dumps(settings.get("hooks", {}))
