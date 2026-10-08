"""The resolve-time landing gate: a task resolves only with a landing assertion or a
logged waiver, and a git delivery branch must actually be on trunk.

Covers: the submission rule for a plan setting delivery_worktree (landed check xor
[meta] landing_waiver), the plan-time versus resolve-time waiver split, the
unlanded-branch probe (unlanded / squash-landed / removed worktree), the
AGENTCTL_LANDING_GATE override and its logging, and `close` threading --landing-waiver.
"""
from __future__ import annotations

import json
import os
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates
from agentctl.plan import parse_plan
from agentctl.state import (
    FinalCheck, LandedSpec, Node, Route, WeightClass,
)
from agentctl.submission import submission_violations
from conftest import STAGE_OBSERVATIONS

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
}


@pytest.fixture(autouse=True)
def _landing_gate_on(_landing_gate_off_by_default, monkeypatch):
    """Depends on the suite-wide force-off fixture so it runs after it and can undo it."""
    monkeypatch.delenv("AGENTCTL_LANDING_GATE", raising=False)


def ns(**kw):
    return Namespace(**kw)


def git(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), env={**os.environ, **GIT_ENV},
                          check=check, capture_output=True, text=True)


def make_repo_with_remote(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    git("init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    git("clone", "--quiet", str(origin), str(work), cwd=tmp_path)
    (work / "README.md").write_text("seed\n")
    git("add", "-A", cwd=work)
    git("commit", "--quiet", "-m", "seed", cwd=work)
    git("push", "--quiet", "-u", "origin", "main", cwd=work)
    return work


def commit_file(work: Path, name: str, text: str) -> None:
    (work / name).write_text(text)
    git("add", name, cwd=work)
    git("commit", "--quiet", "-m", f"add {name}", cwd=work)


def quality_rows():
    path = cli.TASK_QUALITY_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def to_resolution(store, sid, fixtures_dir):
    """Drive plan_two_stage.toml to the RESOLUTION node, ready for a resolve call."""
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(
        session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
        tracker_key=None, architectural=False, external_effect=False,
        new_dependency=False, public_api_change=False,
    ), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(fixtures_dir / "plan_two_stage.toml")), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for observation in STAGE_OBSERVATIONS[:2]:
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                 control="reviewed: ok", observation=observation), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched", note=""), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="skipped",
                             note="test fixture, nothing to record"), store=store)
    assert store.load(sid).node == Node.RESOLUTION.value


def amend(store, sid, **fields):
    state = store.load(sid)
    for name, value in fields.items():
        setattr(state, name, value)
    store.save(state)


def landed_final_check():
    return FinalCheck(command="", label="trunk has it", kind="landed",
                      landed=LandedSpec(target="main", delivered_stage=1))


def resolve(store, sid, **kw):
    return cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                              quality_note=None, **kw), store=store)


def landing_event(store, sid):
    return [{k: v for k, v in e.items() if k != "ts"}
            for e in store.load(sid).history if e["event"] == "landing_gate"]


# --- submission rule ---------------------------------------------------------

def _plan_data(**meta):
    return {
        "meta": {"task_id": "land-test", "goal": "g", "done_criterion": "d",
                 "criterion_type": "measurable", "weight_class": "substantive",
                 "external_research": "n/a", "delivery_worktree": "/tmp/wt-land-test", **meta},
        "stage": [{
            "index": 1, "title": "Deliver", "executor": "in_thread",
            "expected_result_image": "tests pass", "criterion_type": "measurable",
            "done_criterion": "the check passes", "verify_command": "true",
            "negative_control_waiver": "nothing to break in a fixture",
            "material": "m", "means": "pytest", "method": "run it", "conditions": "c",
            "invariants": "inv", "capability_required": "cap",
            "principle": {"statement": "s", "source": "src", "derivation": "d follows from src",
                          "confidence": "high", "refutation": "r"},
        }],
    }


def _landing_problems(data):
    problems = submission_violations(parse_plan(data))
    return [p for p in problems if "declares no landing" in p or "'landing_waiver'" in p]


def _with_landed_final_check(data):
    data["final_check"] = [{"kind": "landed", "label": "trunk has it",
                            "landed": {"target": "main", "delivered_stage": 1}}]
    return data


def test_submission_rejects_delivery_worktree_with_neither_landed_check_nor_waiver():
    problems = _landing_problems(_plan_data())
    assert len(problems) == 1
    assert "kind = \"landed\"" in problems[0] and "landing_waiver" in problems[0]


def test_submission_rejects_both_a_landed_check_and_a_waiver():
    problems = _landing_problems(_with_landed_final_check(_plan_data(landing_waiver="nothing lands")))
    assert len(problems) == 1 and "ambiguous" in problems[0]


def test_submission_accepts_a_landed_check_alone():
    assert _landing_problems(_with_landed_final_check(_plan_data())) == []


def test_submission_accepts_a_waiver_alone():
    assert _landing_problems(_plan_data(landing_waiver="docs-only, lands nothing")) == []


def test_submission_ignores_a_plan_without_delivery_worktree():
    data = _plan_data()
    del data["meta"]["delivery_worktree"]
    assert _landing_problems(data) == []


# --- gate activation ---------------------------------------------------------

def test_landing_gate_activation_follows_weight_class_delivery_worktree_and_env(store, fixtures_dir, monkeypatch):
    to_resolution(store, "act", fixtures_dir)
    state = store.load("act")
    assert gates.landing_gate_active(state) is True
    state.weight_class = WeightClass.SMALL_CHANGE.value
    assert gates.landing_gate_active(state) is False
    state.delivery_worktree = "/somewhere"
    assert gates.landing_gate_active(state) is True
    monkeypatch.setenv("AGENTCTL_LANDING_GATE", "0")
    assert gates.landing_gate_active(state) is False


# --- resolve: plan-time versus resolve-time waiver ---------------------------

def test_delivery_worktree_plan_without_landed_check_or_waiver_is_refused(store, fixtures_dir, tmp_path):
    to_resolution(store, "dw-refused", fixtures_dir)
    amend(store, "dw-refused", delivery_worktree=str(tmp_path / "gone"))

    d = resolve(store, "dw-refused")
    assert d.ok is False
    assert any("neither a kind=landed check nor [meta] landing_waiver" in b for b in d.data["blockers"])
    assert quality_rows() == []
    assert store.load("dw-refused").node == Node.RESOLUTION.value

    d = resolve(store, "dw-refused", landing_waiver="resolve-time excuse")
    assert d.ok is False
    assert any("refused for a plan with a delivery_worktree" in b for b in d.data["blockers"])
    assert quality_rows() == []


def test_plan_time_waiver_resolves_and_is_logged_with_source_plan(store, fixtures_dir, tmp_path):
    to_resolution(store, "plan-waiver", fixtures_dir)
    amend(store, "plan-waiver", delivery_worktree=str(tmp_path / "gone"),
          landing_waiver="docs-only, lands nothing")

    d = resolve(store, "plan-waiver")
    assert d.ok is True
    assert landing_event(store, "plan-waiver") == [
        {"event": "landing_gate", "waiver": "docs-only, lands nothing",
         "waiver_source": "plan", "override": None}]
    row = quality_rows()[-1]
    assert row["landing_waiver"] == "docs-only, lands nothing"
    assert row["landing_waiver_source"] == "plan"
    assert row["landing_gate_override"] is None


def test_plan_with_no_git_venue_is_refused_without_a_resolve_waiver(store, fixtures_dir):
    to_resolution(store, "nogit", fixtures_dir)

    d = resolve(store, "nogit")
    assert d.ok is False
    assert any("--landing-waiver" in b for b in d.data["blockers"])
    assert quality_rows() == []


def test_resolve_waiver_unblocks_a_plan_with_no_git_venue_and_is_logged(store, fixtures_dir):
    to_resolution(store, "nogit-ok", fixtures_dir)

    d = resolve(store, "nogit-ok", landing_waiver="  investigation only  ")
    assert d.ok is True
    assert landing_event(store, "nogit-ok")[0]["waiver_source"] == "resolve"
    row = quality_rows()[-1]
    assert (row["landing_waiver"], row["landing_waiver_source"]) == ("investigation only", "resolve")


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_waiver_is_refused(store, fixtures_dir, empty):
    to_resolution(store, "empty", fixtures_dir)

    d = resolve(store, "empty", landing_waiver=empty)
    assert d.ok is False
    assert any("must be non-empty" in b for b in d.data["blockers"])


def test_plan_with_a_landed_final_check_resolves_without_a_waiver(store, fixtures_dir):
    to_resolution(store, "landed-fc", fixtures_dir)
    amend(store, "landed-fc", final_check=[landed_final_check()])

    d = resolve(store, "landed-fc")
    assert d.ok is True
    assert landing_event(store, "landed-fc")[0]["waiver"] is None
    assert quality_rows()[-1]["landing_waiver"] is None


# --- resolve: the unlanded-branch probe --------------------------------------

def _delivery_session(store, sid, fixtures_dir, work):
    to_resolution(store, sid, fixtures_dir)
    amend(store, sid, delivery_worktree=str(work), final_check=[landed_final_check()])


def test_unlanded_commit_in_an_existing_delivery_worktree_blocks_resolve(store, fixtures_dir, tmp_path):
    work = make_repo_with_remote(tmp_path)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    commit_file(work, "f.txt", "feature\n")
    _delivery_session(store, "unlanded", fixtures_dir, work)

    d = resolve(store, "unlanded")
    assert d.ok is False
    [blocker] = [b for b in d.data["blockers"] if "feature" in b]
    assert "scripts/land-branch.py" in blocker
    assert quality_rows() == []


def test_squash_landed_delivery_branch_does_not_block_resolve(store, fixtures_dir, tmp_path):
    work = make_repo_with_remote(tmp_path)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    commit_file(work, "a.txt", "a\n")
    commit_file(work, "b.txt", "b\n")
    git("checkout", "--quiet", "main", cwd=work)
    git("merge", "--quiet", "--squash", "feature", cwd=work)
    git("commit", "--quiet", "-m", "squashed", cwd=work)
    git("push", "--quiet", "origin", "main", cwd=work)
    git("checkout", "--quiet", "feature", cwd=work)
    _delivery_session(store, "squashed", fixtures_dir, work)

    assert resolve(store, "squashed").ok is True


def test_removed_delivery_worktree_does_not_block_resolve(store, fixtures_dir, tmp_path):
    _delivery_session(store, "removed", fixtures_dir, tmp_path / "land-branch-removed-it")

    assert resolve(store, "removed").ok is True


def test_plan_waiver_skips_the_unlanded_branch_probe(store, fixtures_dir, tmp_path):
    work = make_repo_with_remote(tmp_path)
    git("checkout", "--quiet", "-b", "feature", cwd=work)
    commit_file(work, "f.txt", "feature\n")
    to_resolution(store, "waived-unlanded", fixtures_dir)
    amend(store, "waived-unlanded", delivery_worktree=str(work), landing_waiver="throwaway branch")

    assert resolve(store, "waived-unlanded").ok is True


def test_probe_error_fails_closed(store, fixtures_dir, tmp_path):
    work = make_repo_with_remote(tmp_path)
    git("remote", "rename", "origin", "elsewhere", cwd=work)
    _delivery_session(store, "git-error", fixtures_dir, work)

    d = resolve(store, "git-error")
    assert d.ok is False
    assert any("cannot tell whether" in b for b in d.data["blockers"])


# --- the AGENTCTL_LANDING_GATE override --------------------------------------

def test_force_off_resolves_an_unwaived_plan_and_logs_the_override(store, fixtures_dir, monkeypatch):
    to_resolution(store, "off", fixtures_dir)
    monkeypatch.setenv("AGENTCTL_LANDING_GATE", "0")

    d = resolve(store, "off")
    assert d.ok is True
    assert d.data["landing_gate_override"] == "0"
    assert "AGENTCTL_LANDING_GATE=0" in d.detail
    assert landing_event(store, "off")[0]["override"] == "0"
    assert quality_rows()[-1]["landing_gate_override"] == "0"


def test_force_on_gates_a_non_substantive_session(store, fixtures_dir, monkeypatch):
    to_resolution(store, "on", fixtures_dir)
    amend(store, "on", weight_class=WeightClass.SMALL_CHANGE.value, route=Route.IN_THREAD.value)
    assert resolve(store, "on").ok is True

    to_resolution(store, "on2", fixtures_dir)
    amend(store, "on2", weight_class=WeightClass.SMALL_CHANGE.value, route=Route.IN_THREAD.value)
    monkeypatch.setenv("AGENTCTL_LANDING_GATE", "1")

    d = resolve(store, "on2")
    assert d.ok is False
    assert any("--landing-waiver" in b for b in d.data["blockers"])


# --- close threads the waiver ------------------------------------------------

def test_close_probe_reports_the_landing_blocker(store, fixtures_dir):
    to_resolution(store, "close-probe", fixtures_dir)

    d = cli.cmd_close(ns(session="close-probe"), store=store)
    assert d.action == "fix_stages"
    assert any("--landing-waiver" in b for b in d.data["blockers"])


def test_close_passes_the_waiver_to_its_probe_and_to_the_confirmed_resolve(store, fixtures_dir):
    to_resolution(store, "close-waiver", fixtures_dir)

    probe = cli.cmd_close(ns(session="close-waiver", landing_waiver="investigation only"), store=store)
    assert probe.action == "await_user_confirmation"

    d = cli.cmd_close(ns(session="close-waiver", landing_waiver="investigation only",
                         confirmed_by="user", quality=5, quality_by="user-confirmed"), store=store)
    assert d.ok is True and d.node == Node.RESOLVED.value
    assert quality_rows()[-1]["landing_waiver_source"] == "resolve"
