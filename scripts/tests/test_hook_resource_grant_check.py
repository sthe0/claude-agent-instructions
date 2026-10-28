"""The resource-grant PreToolUse hook (`scripts/hook-resource-grant-check.py`): activation, the kill
switch, each decision class, per-segment rule matching, the decision log and the failure modes.

The opaque-code judge is always a stub; no `claude` process is started. The cases of the denial
fixture are replayed through the same entry point in `test_resource_grant_replay.py`.
"""
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
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
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


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
        kw.setdefault("runs_worktree_code", True)
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


def test_default_mode_does_not_run_a_filesystem_program_itself(rig):
    decision, row, _ = rig.bash(rig.child(mode="default"), "touch f")
    assert (decision, row["reason_class"], row["matched"]) == ("none", "uncovered-write", [])


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


class FrozenTime:
    """The hook's `time` module with a clock that never moves, so a deadline test is exact."""

    def __init__(self, now=1000.0):
        self.now = now

    def monotonic(self):
        return self.now

    strftime = staticmethod(time.strftime)


def test_s2_the_judge_gets_the_judge_timeout_and_one_deadline_for_the_whole_call(rig, hook, monkeypatch):
    clock = FrozenTime()
    monkeypatch.setattr(hook, "time", clock)
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
    assert seen[-1] == 3
    assert hook.JudgeBudget(total_s=3, judge_s=15).next_timeout() == 3
    assert hook.JudgeBudget(total_s=0, judge_s=15).next_timeout() == 0


def test_n5_a_sliver_of_the_deadline_is_a_second_not_nothing(hook):
    now = [100.0]
    budget = hook.JudgeBudget(2.0, judge_s=15, clock=lambda: now[0])
    assert (budget.next_timeout(), budget.git_timeout()) == (2, 2)
    now[0] = 101.8
    assert (budget.next_timeout(), budget.git_timeout()) == (1, 1)
    now[0] = 102.0
    assert (budget.next_timeout(), budget.git_timeout()) == (0, 0)


def test_n6_the_git_surface_capture_draws_from_the_call_deadline(rig, hook, monkeypatch):
    seen = []
    real_capture = hook.git_surface.capture

    def spy(directory, timeout):
        seen.append(timeout())
        return real_capture(directory, timeout)

    child = rig.child()
    monkeypatch.setattr(hook.git_surface, "capture", spy)
    monkeypatch.setattr(hook, "time", FrozenTime())
    rig.bash(child, "git commit -q -m x", constants={**CONSTANTS, "resource-grant-hook-timeout-s": "7"})
    assert seen == [2]
    monkeypatch.setattr(hook.JudgeBudget, "git_timeout", lambda self: 0)
    decision, row, _ = rig.bash(child, "git commit -q -m x")
    assert (decision, row["reason_class"]) == ("none", "undeterminable")


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


def test_s13_a_git_option_before_the_subcommand_is_not_a_preserving_call(rig):
    child = rig.child(extra_patterns=["Bash(git:*)"])
    decision, row, answer = rig.bash(child, "git -c core.hooksPath=/tmp/x commit -q -m x")
    assert (decision, answer, row["reason_class"]) == ("none", "", "undeterminable")
    decision, row, _ = rig.bash(child, f"git -C {rig.venue} commit -q -m x")
    assert (decision, row["reason_class"]) == ("allow", "covered")


# --- the trusted snapshot of what git executes -------------------------------------------------------
# The spawn records a digest of every git code-execution surface of the child's repositories; the hook
# allows a line that runs git only while the live digest is the same.

def plant(directory, name, executable=True, body="#!/bin/sh\ntouch /tmp/planted\n"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body)
    path.chmod(0o755 if executable else 0o644)
    return path


def use_hooks_dir(rig, directory):
    seg_tests._git(rig.venue, "config", "core.hooksPath", str(directory))


def commit_all(rig, message="m"):
    seg_tests._git(rig.venue, "add", "-A", "-f")
    seg_tests._git(rig.venue, "commit", "-q", "-m", message)


def undeterminable(result):
    decision, row, answer = result
    return (decision, answer, row["reason_class"]) == ("none", "", "undeterminable")


def test_a_line_that_runs_git_is_allowed_while_the_surface_is_the_one_the_spawn_recorded(rig):
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, hooks)
    plant(hooks, "pre-push", body="#!/bin/sh\nexit 0\n")
    commit_all(rig)
    child = rig.child()
    decision, row, _ = rig.bash(child, "git push origin feat")
    assert (decision, row["reason_class"]) == ("allow", "covered")


def test_s14_a_push_of_the_declared_branch_in_a_repository_with_tracked_hooks_is_allowed(rig):
    """This repository's layout: core.hooksPath=githooks, hooks tracked in the tree."""
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, "githooks")
    plant(hooks, "pre-push", body="#!/bin/sh\nexit 0\n")
    plant(hooks, "pre-commit", body="#!/bin/sh\nexit 0\n")
    commit_all(rig)
    child = rig.child()
    assert rig.bash(child, "git push origin feat")[0] == "allow"
    (hooks / "pre-push").write_text("#!/bin/sh\ntouch /tmp/planted\n")
    assert undeterminable(rig.bash(child, "git push origin feat"))


def test_n10_add_commit_push_is_allowed_while_a_harness_neighbour_only_adds_and_commits(rig):
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, "githooks")
    plant(hooks, "pre-push", body="#!/bin/sh\nexit 0\n")
    commit_all(rig)
    (rig.venue / "a.txt").write_text("x")
    child = rig.child(allow=["Bash(git add:*)", "Bash(git commit:*)", "Bash(git restore:*)", "Bash(git checkout:*)"])
    assert rig.bash(child, "git add a.txt && git commit -q -m x && git push origin feat")[0] == "allow"
    assert undeterminable(rig.bash(child, "git restore githooks/pre-push && git push origin feat"))
    assert undeterminable(rig.bash(child, "git checkout -- githooks && git push origin feat"))


def test_b8_a_status_beside_a_stale_index_runs_post_index_change_so_a_planted_hook_blocks(rig):
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, hooks)
    hooks.mkdir()
    child = rig.child(allow=["Bash(git status:*)"])
    assert rig.bash(child, f"git -C {rig.venue} status")[0] == "allow"
    plant(hooks, "post-index-change")
    (rig.venue / "a.txt").write_text("x")
    assert undeterminable(rig.bash(child, f"git -C {rig.venue} status"))
    decision, row, _ = rig.bash(child, "git status")
    assert (decision, row["reason_class"]) == ("none", "harness-allowed")


def test_b8_a_harness_allowed_git_segment_is_checked_beside_a_covered_write(rig):
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, hooks)
    hooks.mkdir()
    child = rig.child(allow=["Bash(git status:*)"])
    command = f"echo x > {rig.venue}/out.txt && git status"
    assert rig.bash(child, command)[0] == "allow"
    plant(hooks, "post-index-change")
    assert undeterminable(rig.bash(child, command))


def test_b8_a_covered_write_into_a_hooks_directory_beside_a_git_run_is_undeterminable(rig):
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, hooks)
    hooks.mkdir()
    (rig.venue / "a.txt").write_text("x")
    child = rig.child()
    assert rig.bash(child, "echo x > a.txt && git add a.txt")[0] == "allow"
    assert undeterminable(rig.bash(child, f"echo x > {hooks}/post-index-change && git add a.txt"))


def test_b9_a_symlinked_hook_entry_whose_target_is_rewritten_after_the_spawn_blocks(rig):
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, hooks)
    target = rig.outside / "real-hook"
    plant(rig.outside, "real-hook", body="#!/bin/sh\nexit 0\n")
    hooks.mkdir()
    (hooks / "pre-push").symlink_to(target)
    child = rig.child()
    assert rig.bash(child, "git push origin feat")[0] == "allow"
    target.write_text("#!/bin/sh\ntouch /tmp/planted\n")
    assert undeterminable(rig.bash(child, "git push origin feat"))


def test_a_hook_entry_added_removed_or_made_executable_after_the_spawn_blocks(rig):
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, hooks)
    plant(hooks, "pre-commit", executable=False, body="#!/bin/sh\nexit 0\n")
    child = rig.child()
    assert rig.bash(child, "git commit -q -m x")[0] == "allow"
    (hooks / "pre-commit").chmod(0o755)
    assert undeterminable(rig.bash(child, "git commit -q -m x"))
    (hooks / "pre-commit").chmod(0o644)
    assert rig.bash(child, "git commit -q -m x")[0] == "allow"
    plant(hooks, "post-commit")
    assert undeterminable(rig.bash(child, "git commit -q -m x"))
    (hooks / "post-commit").unlink()
    (hooks / "pre-commit").unlink()
    assert undeterminable(rig.bash(child, "git commit -q -m x"))


def test_a_hook_sample_is_not_part_of_the_surface(rig):
    child = rig.child()
    plant(rig.venue / ".git" / "hooks", "pre-push.sample")
    assert rig.bash(child, "git push origin feat")[0] == "allow"


@pytest.mark.parametrize("key, value", [
    ("core.fsmonitor", "/tmp/evil-monitor"),
    ("filter.lfs.clean", "/tmp/evil-filter"),
    ("diff.external", "/tmp/evil-diff"),
    ("core.sshCommand", "/tmp/evil-ssh"),
    ("gpg.program", "/tmp/evil-gpg"),
    ("alias.st", "!/tmp/evil-alias"),
])
def test_a_config_entry_naming_a_program_added_after_the_spawn_blocks(rig, key, value):
    child = rig.child()
    assert rig.bash(child, "git push origin feat")[0] == "allow"
    seg_tests._git(rig.venue, "config", key, value)
    assert undeterminable(rig.bash(child, "git push origin feat"))


def test_a_program_named_by_the_config_that_is_rewritten_after_the_spawn_blocks(rig):
    program = plant(rig.outside, "monitor", body="#!/bin/sh\nexit 0\n")
    seg_tests._git(rig.venue, "config", "core.fsmonitor", str(program))
    child = rig.child()
    assert rig.bash(child, "git push origin feat")[0] == "allow"
    program.write_text("#!/bin/sh\ntouch /tmp/planted\n")
    assert undeterminable(rig.bash(child, "git push origin feat"))


def test_a_config_entry_that_is_not_a_program_does_not_change_the_surface(rig):
    child = rig.child()
    seg_tests._git(rig.venue, "config", "user.name", "someone else")
    assert rig.bash(child, "git push origin feat")[0] == "allow"


def test_attributes_added_after_the_spawn_block(rig):
    child = rig.child()
    (rig.venue / ".gitattributes").write_text("*.txt filter=evil\n")
    assert undeterminable(rig.bash(child, "git push origin feat"))
    (rig.venue / ".gitattributes").unlink()
    (rig.venue / ".git" / "info").mkdir(exist_ok=True)
    (rig.venue / ".git" / "info" / "attributes").write_text("*.txt filter=evil\n")
    assert undeterminable(rig.bash(child, "git push origin feat"))


def test_a_hook_framework_file_changed_after_the_spawn_blocks(rig):
    (rig.venue / ".pre-commit-config.yaml").write_text("repos: []\n")
    (rig.venue / ".husky").mkdir()
    (rig.venue / ".husky" / "pre-push").write_text("exit 0\n")
    child = rig.child()
    assert rig.bash(child, "git push origin feat")[0] == "allow"
    (rig.venue / ".husky" / "pre-push").write_text("touch /tmp/planted\n")
    assert undeterminable(rig.bash(child, "git push origin feat"))


def test_a_record_without_the_snapshot_makes_every_git_run_undeterminable(rig, tmp_path):
    child = rig.child()
    record = dict(child.record)
    del record["git_surface"]
    path, sha = support.norm_handoff.write_record(tmp_path / "old-state", record)
    env = {**child.env, support.norm_handoff.NORM_HANDOFF_ENV: support.norm_handoff.format_env_value(path, sha)}
    old = dataclasses.replace(child, env=env, state_root=tmp_path / "old-state")
    assert undeterminable(rig.bash(old, "git push origin feat"))
    assert undeterminable(rig.bash(old, f"git -C {rig.venue} status"))
    assert rig.bash(old, f"echo x > {rig.venue}/f")[0] == "allow"


def test_a_repository_the_snapshot_does_not_know_is_undeterminable(rig, hook, tmp_path):
    linked = tmp_path / "linked"
    child = rig.child(extra_resources=[support.R.FileResource(str(linked), "write")])
    seg_tests._git(rig.venue, "worktree", "add", "-q", "-b", "side", str(linked))
    assert hook.git_surface.capture(str(linked), lambda: 20).digest
    assert rig.bash(child, f"git -C {rig.venue} status")[0] == "allow"
    assert undeterminable(rig.bash(child, f"git -C {linked} status"))
    verdict = hook.SegmentVerdict("git status", hook.COVERED, git_dir=str(linked))
    why = hook.git_surface_blocker(child_approved(hook, child), [verdict], hook.JudgeBudget(20, 15))
    assert "is not in the git-surface snapshot" in why


def child_approved(hook, child):
    record = child.record
    return hook.Approved([], [], [], "p", 8, record["launch_dir"], "default", git_surface=record["git_surface"])


def test_a_segment_outside_any_repository_that_runs_git_is_no_hook(rig, hook):
    verdict = hook.SegmentVerdict("git status", hook.COVERED, venue=str(rig.outside), git_dir=str(rig.outside))
    approved = hook.Approved([], [], [], "p", 8, str(rig.outside), "default", git_surface={"schema": 1, "repos": {}})
    assert hook.git_surface_blocker(approved, [verdict], hook.JudgeBudget(20, 15)) is None


def test_a_surface_git_cannot_be_read_makes_the_line_undeterminable(rig, hook, monkeypatch):
    child = rig.child()

    def broken(directory, timeout):
        raise hook.git_surface.Unreadable("boom")

    monkeypatch.setattr(hook.git_surface, "capture", broken)
    assert undeterminable(rig.bash(child, "git push origin feat"))
    assert rig.bash(child, f"echo x > {rig.venue}/f")[0] == "allow"


def test_a_neighbour_that_could_change_the_surface_first_blocks_the_git_run(rig):
    judge = JudgeStub([{"kind": "file", "op": "write", "path": f"{rig.venue}/out.txt"}])
    decision, row, _ = rig.bash(rig.child(), f"{STDIN_CODE}\ngit commit -q -m x", judge=judge)
    assert (decision, row["reason_class"]) == ("none", "undeterminable")
    decision, row, _ = rig.bash(rig.child(), "git commit -q -m x")
    assert (decision, row["reason_class"]) == ("allow", "covered")


def test_s13_a_rule_pattern_neighbour_counts_like_an_opaque_one(rig):
    child = rig.child(rules=["Bash(make test:*)"])
    assert rig.bash(child, "make test")[0] == "allow"
    assert rig.bash(child, "git push origin feat")[0] == "allow"
    assert undeterminable(rig.bash(child, "git push origin feat && make test"))


def test_s13_an_opaque_neighbour_after_the_git_run_blocks_it(rig):
    judge = JudgeStub([{"kind": "file", "op": "write", "path": f"{rig.venue}/out.txt"}])
    child = rig.child()
    assert rig.bash(child, "git push origin feat && echo y")[0] == "allow"
    assert rig.bash(child, STDIN_CODE, judge=judge)[0] == "allow"
    assert undeterminable(rig.bash(child, f"git push origin feat && {STDIN_CODE}", judge=judge))


def test_s13_a_harness_allowed_neighbour_blocks_the_git_run_unless_it_is_read_only(rig):
    child = rig.child(allow=["Bash(make:*)", "Bash(ls:*)"])
    assert rig.bash(child, "git push origin feat && ls")[0] == "allow"
    assert undeterminable(rig.bash(child, "git push origin feat && make test"))


def test_s13_a_neighbour_writing_onto_a_pinned_path_blocks_the_git_run(rig):
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, hooks)
    hooks.mkdir()
    (rig.venue / ".git" / "info").mkdir(exist_ok=True)
    child = rig.child()
    assert undeterminable(rig.bash(child, f"echo x > {hooks}/pre-push && git push origin feat"))
    for target in (f"{rig.venue}/.gitattributes", f"{rig.venue}/.pre-commit-config.yaml", f"{rig.venue}/.git/config"):
        assert rig.bash(child, f"echo x > {target} && git push origin feat")[0] != "allow", target
    assert rig.bash(child, f"echo x > {rig.venue}/ok.txt && git push origin feat")[0] == "allow"


def test_s16_a_read_only_git_call_is_held_to_the_snapshot_too(rig):
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, hooks)
    hooks.mkdir()
    child = rig.child()
    assert rig.bash(child, "git status")[0] == "allow"
    plant(hooks, "post-index-change")
    assert undeterminable(rig.bash(child, "git status"))
    assert undeterminable(rig.bash(child, "git log -1"))


def child_in(rig, venue, **kw):
    kw.setdefault("runs_worktree_code", True)
    return support.link_child(rig.spawn, rig.tmp / "state", venue, **kw)


def test_b11_a_rule_pattern_git_call_into_another_repository_is_checked_there(rig, tmp_path):
    other = seg_tests._init_repo(tmp_path / "other", "feat")
    plant(other / ".git" / "hooks", "pre-push")
    child = rig.child(extra_patterns=["Bash(git:*)"])
    assert rig.bash(child, "git status")[0] == "allow"
    assert undeterminable(rig.bash(child, f"git -C {other} status"))
    assert undeterminable(rig.bash(child, f"git -C {other} -C . status"))
    assert undeterminable(rig.bash(child, f"git -C {rig.venue} -C {other} status"))


def test_b11_a_cd_into_another_repository_moves_the_repository_git_runs_in(rig, tmp_path):
    other = seg_tests._init_repo(tmp_path / "other", "feat")
    plant(other / ".git" / "hooks", "pre-push")
    child = rig.child(extra_patterns=["Bash(git:*)"])
    assert rig.bash(child, f"cd {rig.venue} && git push origin feat")[0] == "allow"
    assert undeterminable(rig.bash(child, f"cd {other} && git push origin feat"))


def test_b11_a_repository_git_dir_that_cannot_be_resolved_is_undeterminable(rig):
    child = rig.child(extra_patterns=["Bash(git:*)"])
    for target in ("~/x", "$HOME", "sub/*"):
        assert undeterminable(rig.bash(child, f"git -C {target} status")), target


def test_b12_a_checkout_that_rewrites_a_pinned_worktree_file_is_undeterminable(rig):
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, "githooks")
    plant(hooks, "pre-push", body="#!/bin/sh\nexit 0\n")
    commit_all(rig)
    seg_tests._git(rig.venue, "checkout", "-q", "-b", "other")
    plant(hooks, "post-checkout")
    commit_all(rig)
    seg_tests._git(rig.venue, "checkout", "-q", "feat")
    child = rig.child(extra_patterns=["Bash(git checkout:*)"])
    assert undeterminable(rig.bash(child, "git checkout other"))
    assert rig.bash(child, "git push origin feat")[0] == "allow"


def test_b13_init_then_a_covered_hook_write_then_a_commit_is_undeterminable_without_any_repository(rig, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "hook-body").write_text("#!/bin/sh\ntouch /tmp/planted\n")
    child = child_in(rig, plain, branch="feat", extra_patterns=["Bash(git init:*)"])
    command = "git init -q && cp hook-body .git/hooks/post-commit && git commit --allow-empty -q -m x"
    assert undeterminable(rig.bash(child, command))
    assert rig.bash(child, "git init -q")[0] == "allow"
    assert rig.bash(child, "cp hook-body copy && git init -q")[0] == "allow"


def test_s22_a_git_run_in_a_repository_holding_code_git_starts_needs_the_declared_flag(rig):
    hooks = rig.venue / "githooks"
    use_hooks_dir(rig, "githooks")
    plant(hooks, "pre-push", body="#!/bin/sh\nexit 0\n")
    commit_all(rig)
    assert undeterminable(rig.bash(rig.child(runs_worktree_code=False), "git push origin feat"))
    assert undeterminable(rig.bash(rig.child(runs_worktree_code=False), "git status"))
    assert rig.bash(rig.child(runs_worktree_code=True), "git push origin feat")[0] == "allow"


def test_s22_without_code_git_starts_the_flag_is_not_needed(rig):
    child = rig.child(runs_worktree_code=False)
    assert rig.bash(child, "git push origin feat")[0] == "allow"
    plant(rig.venue / ".git" / "hooks", "pre-push.sample")
    assert rig.bash(child, "git push origin feat")[0] == "allow"


def test_s22_without_a_write_grant_inside_the_worktree_the_flag_is_not_needed(rig, hook):
    use_hooks_dir(rig, "githooks")
    plant(rig.venue / "githooks", "pre-push", body="#!/bin/sh\nexit 0\n")
    commit_all(rig)
    live = {str(rig.venue): hook.git_surface.capture(str(rig.venue), lambda: 20)}
    approved = hook.Approved([support.R.FileResource(str(rig.outside), "write")], [], [], "p", 8, str(rig.venue), "default")
    assert hook.worktree_code_blocker(approved, live) is None
    approved.resources.append(support.R.FileResource(str(rig.venue), "write"))
    assert "runs_worktree_code" in hook.worktree_code_blocker(approved, live)
    approved.runs_worktree_code = True
    assert hook.worktree_code_blocker(approved, live) is None


# --- B7: the settings the harness merges with the record ----------------------------------------------

def write_settings(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body))


def test_b7_a_project_scope_allow_entry_keeps_the_hook_from_denying(rig):
    child = rig.child()
    command = f"touch {rig.outside}/f"
    assert rig.bash(child, command)[0] == "deny"
    write_settings(rig.venue / ".claude" / "settings.local.json", {"permissions": {"allow": ["Bash(touch:*)"]}})
    decision, row, answer = rig.bash(child, command)
    assert (decision, answer, row["reason_class"]) == ("none", "", "uncovered-write")


@pytest.mark.parametrize("where", ["user", "project", "config-root"])
def test_b7_an_allow_entry_in_any_ambient_file_keeps_the_hook_from_denying(rig, where, tmp_path):
    paths = {
        "user": tmp_path / "home" / ".claude" / "settings.json",
        "project": rig.venue / ".claude" / "settings.json",
        "config-root": tmp_path / "agent-home" / "settings.json",
    }
    write_settings(paths[where], {"permissions": {"allow": ["Bash(touch:*)"]}})
    child = rig.child()
    decision, _, _ = rig.bash(child, f"touch {rig.outside}/f", env={**child.env, "CLAUDE_CONFIG_DIR": str(tmp_path / "agent-home")})
    assert decision == "none"


def test_b7_an_ambient_additional_directory_is_a_working_directory(rig):
    child = rig.child()
    command = f"touch {rig.outside}/f"
    write_settings(rig.venue / ".claude" / "settings.json", {"permissions": {"additionalDirectories": [str(rig.outside)]}})
    decision, row, _ = rig.bash(child, command)
    assert (decision, row["reason_class"]) == ("none", "uncovered-write")


@pytest.mark.parametrize("body", ["{not json", "[]", '{"permissions": []}', '{"permissions": {"allow": "x"}}', ""])
def test_b7_a_settings_file_that_cannot_be_read_as_settings_means_no_deny(rig, body):
    write_settings(rig.venue / ".claude" / "settings.json", body)
    decision, row, _ = rig.bash(rig.child(), f"touch {rig.outside}/f")
    assert (decision, row["reason_class"]) == ("none", "uncovered-write")


def test_s17_project_settings_are_read_from_the_launch_directory_not_the_cwd(rig):
    child = rig.child()
    command = f"touch {rig.outside}/f"
    other = rig.tmp / "elsewhere"
    other.mkdir()

    def after_cd():
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(other),
                              "permission_mode": child.mode})
        return rig.hook.run(payload, child.env, runner=JudgeStub([]), cache_dir=rig.tmp / "judge-cache", constants=CONSTANTS)

    assert json.loads(after_cd())["hookSpecificOutput"]["permissionDecision"] == "deny"
    write_settings(rig.venue / ".claude" / "settings.json", {"permissions": {"allow": ["Bash(touch:*)"]}})
    assert after_cd() == ""


def test_n8_a_relative_additional_directory_is_anchored_on_the_settings_root(rig):
    write_settings(rig.venue / ".claude" / "settings.json", {"permissions": {"additionalDirectories": ["../outside"]}})
    decision, row, _ = rig.bash(rig.child(), f"touch {rig.outside}/f")
    assert (decision, row["reason_class"]) == ("none", "uncovered-write")


# --- S9, S10: opaque confinement and the carrier set --------------------------------------------------

def test_s9_opaque_code_whose_only_write_is_a_redirect_is_not_allowed_on_its_reads(rig):
    judge = JudgeStub([])
    code = f"python3 - <<'E' > {rig.venue}/o.txt\nimport json\nprint(json.dumps({{}}))\nE"
    decision, row, _ = rig.bash(rig.child(), code, judge=judge)
    assert (decision, row["reason_class"]) == ("none", "undeterminable") and judge.calls == 1


def test_s10_a_git_variable_makes_any_carrier_segment_undeterminable_but_not_a_plain_file_program(rig):
    child = rig.child(rules=["Bash(make test:*)"])
    env = {**child.env, "GIT_DIR": str(rig.outside)}
    assert rig.bash(child, "make test")[0] == "allow"
    decision, row, _ = rig.bash(child, "make test", env=env)
    assert (decision, row["reason_class"]) == ("none", "undeterminable")
    assert rig.bash(child, f"cat {rig.venue}/f", env=env)[0] == "allow"


def test_s10_a_python_variable_makes_a_carrier_segment_undeterminable(rig):
    child = rig.child(rules=["Bash(make test:*)"])
    decision, _, _ = rig.bash(child, "make test", env={**child.env, "PYTHONSTARTUP": str(rig.outside)})
    assert decision == "none"
    assert rig.bash(child, "make test", env={**child.env, "PYTHONUNBUFFERED": "1"})[0] == "allow"


def test_b10_a_git_variable_stops_python_and_a_python_variable_stops_git(rig):
    child = rig.child()
    (rig.venue / "tool.py").write_text("print(1)\n")
    judge = JudgeStub([{"kind": "file", "op": "write", "path": f"{rig.venue}/o"}])
    python = f"python3 {rig.venue}/tool.py"
    assert rig.bash(child, python, judge=judge)[0] == "allow"
    assert undeterminable(rig.bash(child, python, judge=judge, env={**child.env, "GIT_DIR": str(rig.outside)}))
    assert rig.bash(child, "git push origin feat")[0] == "allow"
    assert undeterminable(rig.bash(child, "git push origin feat", env={**child.env, "PYTHONPATH": str(rig.outside)}))
    assert rig.bash(child, "git push origin feat", env={**child.env, "PYTHONUNBUFFERED": "1"})[0] == "allow"
    assert rig.bash(child, f"echo x > {rig.venue}/f", env={**child.env, "GIT_DIR": str(rig.outside)})[0] == "allow"


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


def test_script_inside_the_opaque_code_judge_child_decides_nothing_and_logs_nothing(rig):
    child = rig.child()
    rows = len(child.log_rows())
    done = _run_script(rig, child, child.payload("Bash", command=f"touch {rig.outside}/f"),
                       {"AGENTCTL_JUDGE_CHILD": "1"})
    assert (done.returncode, done.stdout) == (0, "")
    assert len(child.log_rows()) == rows


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
    no_arc = tmp_path / "no-arc-bin"
    no_arc.mkdir()
    (no_arc / "arc").write_text("#!/bin/sh\nexit 1\n")
    (no_arc / "arc").chmod(0o755)
    monkeypatch.setenv("PATH", f"{no_arc}{os.pathsep}{os.environ['PATH']}")
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
