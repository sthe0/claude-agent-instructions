"""A нормативное critique carries a consequence: it names the norm that failed
(`--norm-target`), the closing replan must change it in a re-norming stage (`renorms`),
and the edit stays owed -- across replans, through verify-final and resolve -- until the
norm file's bytes have moved.

Tests marked "negative control" are the ones the stage's negative control runs against
the base tree: they drive entry points that exist there and read the new symbols only
inside their bodies, so on base they fail on their `BEHAVIOUR:` assertion."""
import hashlib
import json
import subprocess
from argparse import Namespace
from copy import deepcopy
from dataclasses import fields as dc_fields
from dataclasses import is_dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import cli, gates
from agentctl.dispatch import RunResult
from agentctl.plan import PlanError, load_plan
from agentctl.stage_norm import StageNorm
from agentctl.state import (
    SCHEMA_VERSION, Actor, Criterion, LandedSpec, Means, Node, Principle, SessionState,
    Stage, StageStatus, Subject, Supply,
)
from conftest import STAGE_OBSERVATIONS
from test_re_attest import _dev_stage, _Mem, _reattest_stash, _runner
from test_re_attest import _state as _reattest_state
from test_subplan import _make_frame


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def norms(tmp_path, monkeypatch):
    """The norm-bearing root of every test that does not exercise the real classifier."""
    root = tmp_path / "norms"
    root.mkdir()
    monkeypatch.setenv("AGENTCTL_NORM_ROOTS", str(root))
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    return root


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                    "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def real_roots(tmp_path, monkeypatch):
    """The real classifier: no override roots, scratch narrowed to its own directory,
    a canonical instructions checkout with a linked worktree, and an agent home."""
    monkeypatch.delenv("AGENTCTL_NORM_ROOTS", raising=False)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", str(scratch))
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    canon = tmp_path / "canon"
    (canon / "docs").mkdir(parents=True)
    (canon / "memory-global" / "leaves").mkdir(parents=True)
    (canon / "docs" / "rule.md").write_text("rule\n", encoding="utf-8")
    (canon / "memory-global" / "leaves" / "leaf.md").write_text("leaf\n", encoding="utf-8")
    _git(canon, "init", "-q")
    _git(canon, "add", "-A")
    _git(canon, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    _git(canon, "worktree", "add", "-q", "-b", "wt-branch", str(wt))
    monkeypatch.setattr(cli, "REPO_ROOT", canon)
    return SimpleNamespace(scratch=scratch, home=home, canon=canon, wt=wt)


# --- plans -------------------------------------------------------------------

def _stage(index, title, result, done, deps, outs, **extra):
    return {"index": index, "title": title, "result": result, "done": done,
            "deps": deps, "outs": outs, **extra}


def _stages(*, s1=None, s2=None):
    return [
        _stage(1, "Scaffold module", "module file exists and imports cleanly",
               "python -c 'import mod' exits 0", [], ["mod.py"], **(s1 or {})),
        _stage(2, "Add tests", "pytest green for the new module",
               "pytest tests/test_mod.py green", [1], ["tests/test_mod.py"], **(s2 or {})),
    ]


def _plan(path, stages):
    lines = ["[meta]", 'weight_class = "small_change"', 'task_id = "demo-two-stage"',
             'goal = "Demonstrate a norm-edit consequence"',
             'done_criterion = "both stages PASSED and resolution confirmed"',
             'criterion_type = "measurable"']
    for s in stages:
        lines += ["", "[[stage]]", f"index = {s['index']}", f"title = {json.dumps(s['title'])}",
                  'executor = "spawn:developer"',
                  f"expected_result_image = {json.dumps(s['result'])}",
                  'criterion_type = "measurable"', f"done_criterion = {json.dumps(s['done'])}",
                  f"depends_on = {json.dumps(s['deps'])}",
                  f"output_artifacts = {json.dumps(s['outs'])}"]
        if "method" in s:
            lines.append(f"method = {json.dumps(s['method'])}")
        if "procedure" in s:
            lines.append(f"procedure = {json.dumps(s['procedure'])}")
        if "renorms" in s:
            lines.append(f"renorms = {json.dumps(s['renorms'])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def plans(tmp_path):
    def make(name, stages=None):
        return _plan(tmp_path / "work" / f"{name}.toml", stages or _stages())
    return make


# --- driving the spine -------------------------------------------------------

def _start(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _pass(store, sid, index):
    return cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation=STAGE_OBSERVATIONS[index - 1]), store=store)


def _diagnosing(store, sid, plan, *, fail_stage=1):
    _start(store, sid, plan)
    if fail_stage == 2:
        _pass(store, sid, 1)
        cli.cmd_next_stage(ns(session=sid), store=store)
    _fail_and_investigate(store, sid)


def _fail_and_investigate(store, sid):
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    assert store.load(sid).node == Node.DIAGNOSING.value


def _critique(store, sid, address, targets=None):
    return cli.cmd_critique(
        ns(session=sid, functional_ground="fg", replanning_task="rt",
           failure_address=address, norm_targets=targets), store=store)


def _normalize(store, sid):
    d = cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"),
                          store=store)
    assert d.ok is True


def _replan(store, sid, plan, **kw):
    return cli.cmd_replan(ns(session=sid, plan=plan, **kw), store=store)


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _norm_file(norms, name="leaf.md"):
    p = norms / name
    p.write_text("v1\n", encoding="utf-8")
    return str(p)


def _seed(store, sid, *, address=None, targets):
    """Write `norm_targets` (and optionally the critique's address) into the state file:
    the way a record that inherited targets from a closed nested difficulty looks."""
    path = store.path(sid)
    data = json.loads(path.read_text(encoding="utf-8"))
    if address is not None:
        data["difficulty"]["critique"]["failure_address"] = address
    data["difficulty"]["norm_targets"] = targets
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _file_target(path, *, source="critique"):
    return {"target": path, "kind": "file", "baseline": _digest(path), "source": source}


def _blockers(d):
    return " | ".join(d.data.get("blockers", []))


def _bound_session(store, sid, plans, norm):
    """A VERIFYING session whose replan admitted a re-norming stage 1 for `norm`."""
    _diagnosing(store, sid, plans("base"))
    d = _critique(store, sid, "нормативное", [norm])
    assert d.ok is True
    _normalize(store, sid)
    d = _replan(store, sid, plans("renorm", _stages(s1={"renorms": [norm]})))
    assert d.ok is True
    state = store.load(sid)
    assert state.node == Node.VERIFYING.value
    return state


# --- the critique ------------------------------------------------------------

def test_normative_requires_norm_target(store, plans):
    """negative control: a нормативное critique with no norm target is refused."""
    _diagnosing(store, "n1", plans("base"))
    d = _critique(store, "n1", "нормативное")
    assert d.ok is False, "BEHAVIOUR: a нормативное critique naming no norm target was accepted"
    assert "--norm-target" in d.detail, "BEHAVIOUR: the refusal does not name the flag to pass"
    assert store.load("n1").difficulty.critique is None, \
        "BEHAVIOUR: a refused critique was persisted"


def test_norm_target_refused_with_other_failure_address(store, plans, norms):
    _diagnosing(store, "n2", plans("base"))
    f = _norm_file(norms)
    for address in ("ресурсное", "not_applicable", None):
        d = _critique(store, "n2", address, [f])
        assert d.ok is False, f"BEHAVIOUR: --norm-target accepted with failure_address {address!r}"
        assert "--norm-target" in d.detail
    assert store.load("n2").difficulty.norm_targets == []


@pytest.mark.parametrize("address", ["ресурсное", "not_applicable"])
def test_other_failure_addresses_critique_unchanged(store, plans, address):
    _diagnosing(store, "n3", plans("base"))
    d = _critique(store, "n3", address)
    assert d.ok is True, "BEHAVIOUR: a critique without norm targets must behave as before"
    state = store.load("n3")
    assert state.difficulty.norm_targets == []
    assert state.difficulty.critique.failure_address == address


def test_scratch_file_refused(store, plans, real_roots):
    _diagnosing(store, "c1", plans("base"))
    f = real_roots.scratch / "note.md"
    f.write_text("x\n", encoding="utf-8")
    d = _critique(store, "c1", "нормативное", [str(f)])
    assert d.ok is False, "BEHAVIOUR: a scratch file was accepted as a norm target"
    assert "scratch" in d.detail


def test_canonical_checkout_file_refused_naming_the_worktree(store, plans, real_roots):
    _diagnosing(store, "c2", plans("base"))
    d = _critique(store, "c2", "нормативное", [str(real_roots.canon / "docs" / "rule.md")])
    assert d.ok is False, "BEHAVIOUR: a file in the canonical checkout was accepted"
    assert "linked worktree" in d.detail


def test_session_plan_file_refused_naming_stage_form(store, plans, real_roots):
    _diagnosing(store, "c3", plans("base"))
    plan_path = store.load("c3").plan_path
    d = _critique(store, "c3", "нормативное", [plan_path])
    assert d.ok is False, "BEHAVIOUR: the session's own plan file was accepted"
    assert "stage:<n>" in d.detail


def test_nonexistent_and_unrooted_paths_refused(store, plans, real_roots, tmp_path):
    _diagnosing(store, "c4", plans("base"))
    missing = _critique(store, "c4", "нормативное", [str(real_roots.wt / "docs" / "nope.md")])
    assert missing.ok is False, "BEHAVIOUR: a nonexistent file was accepted"
    assert "not an existing file" in missing.detail
    stray = tmp_path / "elsewhere.md"
    stray.write_text("x\n", encoding="utf-8")
    unrooted = _critique(store, "c4", "нормативное", [str(stray)])
    assert unrooted.ok is False, "BEHAVIOUR: a file under no norm-bearing root was accepted"
    assert "norm-bearing root" in unrooted.detail


def test_linked_worktree_file_accepted(store, plans, real_roots):
    _diagnosing(store, "c5", plans("base"))
    f = real_roots.wt / "docs" / "rule.md"
    d = _critique(store, "c5", "нормативное", [str(f)])
    assert d.ok is True, f"BEHAVIOUR: a linked-worktree file was refused: {d.detail}"
    (entry,) = store.load("c5").difficulty.norm_targets
    assert entry["target"] == str(f) and entry["kind"] == "file"


def test_memory_leaf_and_skill_file_accepted(store, plans, real_roots):
    _diagnosing(store, "c6", plans("base"))
    leaf = real_roots.home / "projects" / "-some-project" / "memory" / "leaf.md"
    leaf.parent.mkdir(parents=True)
    leaf.write_text("leaf\n", encoding="utf-8")
    skill = real_roots.home / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill\n", encoding="utf-8")
    global_leaf = real_roots.canon / "memory-global" / "leaves" / "leaf.md"
    d = _critique(store, "c6", "нормативное", [str(leaf), str(skill), str(global_leaf)])
    assert d.ok is True, f"BEHAVIOUR: a norm-bearing file was refused: {d.detail}"
    assert len(store.load("c6").difficulty.norm_targets) == 3


def test_norm_roots_override_replaces_the_whole_root_set(store, plans, real_roots,
                                                         monkeypatch):
    _diagnosing(store, "c7", plans("base"))
    inside = real_roots.scratch / "inside.md"
    inside.write_text("x\n", encoding="utf-8")
    outside = real_roots.wt / "docs" / "rule.md"
    monkeypatch.setenv("AGENTCTL_NORM_ROOTS", str(real_roots.scratch))
    ok = _critique(store, "c7", "нормативное", [str(inside)])
    assert ok.ok is True, "BEHAVIOUR: the override did not replace the scratch exclusion"
    refused = _critique(store, "c7", "нормативное", [str(outside)])
    assert refused.ok is False, "BEHAVIOUR: a file outside the override roots was accepted"


def test_snapshot_records_baseline_kind_and_source(store, plans, norms):
    base = plans("base")
    _diagnosing(store, "s1", base)
    f = _norm_file(norms)
    d = _critique(store, "s1", "нормативное", [f, "stage:2"])
    assert d.ok is True
    by_target = {t["target"]: t for t in store.load("s1").difficulty.norm_targets}
    assert by_target[f]["kind"] == "file" and by_target[f]["source"] == "critique"
    assert by_target[f]["baseline"] == _digest(f), \
        "BEHAVIOUR: the file baseline is not the digest of its bytes at critique time"
    stage_two = next(s for s in load_plan(base, strict=False).stages if s.index == 2)
    assert by_target["stage:2"]["kind"] == "stage"
    assert by_target["stage:2"]["baseline"] == gates.stage_norm_digest(stage_two)
    assert _critique(store, "s1", "нормативное", ["stage:9"]).ok is False


def test_recritique_replaces_own_targets_and_keeps_inherited(store, plans, norms):
    _diagnosing(store, "s2", plans("base"))
    own = _norm_file(norms, "own.md")
    inherited = _norm_file(norms, "inherited.md")
    assert _critique(store, "s2", "нормативное", [own]).ok is True
    _seed(store, "s2", targets=[_file_target(own),
                                _file_target(inherited, source="difficulty-close")])
    d = _critique(store, "s2", "нормативное", ["stage:1", inherited])
    assert d.ok is True
    entries = {t["target"]: t for t in store.load("s2").difficulty.norm_targets}
    assert set(entries) == {"stage:1", inherited}, \
        "BEHAVIOUR: a re-critique kept its replaced target or dropped an inherited one"
    assert entries[inherited]["source"] == "difficulty-close", \
        "BEHAVIOUR: a re-critique re-sourced an inherited target as its own"


def _edit_state_stage(store, sid, index, **fields):
    path = store.path(sid)
    data = json.loads(path.read_text(encoding="utf-8"))
    for stage in data["stages"]:
        if stage["index"] == index:
            stage.update(fields)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_recritique_keeps_the_first_baseline_of_a_stage_target_it_names_again(store, plans):
    """A stage edited between two critiques must not become the bar the second critique
    sets: the edit would then read as the unchanged stage the closing plan must move."""
    base = plans("base")
    _diagnosing(store, "s3", base)
    assert _critique(store, "s3", "нормативное", ["stage:1"]).ok is True
    (first,) = store.load("s3").difficulty.norm_targets
    _edit_state_stage(store, "s3", 1, title="Scaffold module, edited")
    assert gates.stage_norm_digest(store.load("s3").stage(1)) != first["baseline"], \
        "setup: the edit did not move the live stage's digest"
    assert _critique(store, "s3", "нормативное", ["stage:1"]).ok is True
    (again,) = store.load("s3").difficulty.norm_targets
    assert again["baseline"] == first["baseline"], \
        "BEHAVIOUR: a re-critique re-based a stage target on the stage as already edited"


def test_stage_the_current_plan_lacks_is_refused_not_read_from_the_snapshot(store, plans):
    """The approved snapshot still holds stage 2, but the state's current plan does not:
    `--renormalize` leaves the snapshot on the approved bytes, so a baseline read from it
    is the digest of a stage the session no longer runs."""
    base = plans("base")
    _diagnosing(store, "s4", base)
    state = store.load("s4")
    snapshot = load_plan(state.plan_snapshot_path, strict=False)
    assert any(s.index == 2 for s in snapshot.stages), "setup: the snapshot lacks stage 2"
    path = store.path("s4")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["stages"] = [s for s in data["stages"] if s["index"] != 2]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    d = _critique(store, "s4", "нормативное", ["stage:2"])
    assert d.ok is False, "BEHAVIOUR: a stage target the current plan lacks was accepted"
    assert "the current plan has no stage 2" in d.detail
    assert store.load("s4").difficulty.norm_targets == []


# --- the norm digest and `renorms` -------------------------------------------

def test_norm_digest_is_index_free_and_moves_on_edit(plans):
    base = load_plan(plans("a"), strict=False)
    shifted = load_plan(plans("b", [
        _stage(1, "Prepare", "env ready", "make env exits 0", [], ["env.txt"]),
        _stage(2, "Scaffold module", "module file exists and imports cleanly",
               "python -c 'import mod' exits 0", [], ["mod.py"]),
        _stage(3, "Add tests", "pytest green for the new module",
               "pytest tests/test_mod.py green", [2], ["tests/test_mod.py"]),
    ]), strict=False)
    edited = load_plan(plans("c", _stages(s1={"method": "a different method"})), strict=False)

    def norm(doc, index):
        return next(s for s in doc.stages if s.index == index)

    assert gates.stage_norm_digest(norm(base, 1)) == gates.stage_norm_digest(norm(shifted, 2)), \
        "BEHAVIOUR: renumbering a stage moved its norm digest"
    assert gates.stage_norm_digest(norm(base, 1)) != gates.stage_norm_digest(norm(edited, 1)), \
        "BEHAVIOUR: a method edit left the norm digest unchanged"


def test_review_digest_is_untouched_for_stages_without_renorms(plans, norms):
    f = _norm_file(norms)
    plain = load_plan(plans("a"), strict=False).stages[0]
    named = load_plan(plans("b", _stages(s1={"renorms": [f]})), strict=False).stages[0]
    assert plain.renorms == [] and named.renorms == [f]
    assert "renorms" not in StageNorm.from_stage(plain)._review_payload(indexed=True), \
        "BEHAVIOUR: a stage without renorms carries a renorms element in its review payload"
    assert StageNorm.from_stage(named).review_digest() != \
        StageNorm.from_stage(plain).review_digest()


def test_renorms_validation(plans, tmp_path):
    good = load_plan(plans("g", _stages(s1={"renorms": ["/abs/norm.md"]})), strict=False)
    assert good.stages[0].renorms == ["/abs/norm.md"]
    for i, bad in enumerate((["relative/norm.md"], [""], ["stage:1"])):
        with pytest.raises(PlanError):
            load_plan(plans(f"b{i}", _stages(s1={"renorms": bad})), strict=False)
    not_a_list = tmp_path / "work" / "not-a-list.toml"
    not_a_list.write_text(
        Path(plans("t")).read_text(encoding="utf-8").replace(
            'output_artifacts = ["mod.py"]', 'output_artifacts = ["mod.py"]\nrenorms = "x"'),
        encoding="utf-8")
    with pytest.raises(PlanError):
        load_plan(str(not_a_list), strict=False)


# --- the closing replan ------------------------------------------------------

def test_closing_replan_refused_without_renorms_reference(store, fixtures_dir, norms):
    """negative control: a closing replan whose record names a norm file must name it in a
    stage's `renorms`; the refined plan below changes stage 1 but never mentions the file."""
    f = _norm_file(norms)
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _diagnosing(store, "r1", plan)
    assert _critique(store, "r1", "ресурсное").ok is True
    _seed(store, "r1", address="нормативное", targets=[_file_target(f)])
    _normalize(store, "r1")
    before = store.path("r1").read_bytes()
    d = _replan(store, "r1", refined)
    assert d.ok is False, "BEHAVIOUR: a closing replan with no renorms reference was applied"
    assert store.load("r1").node == Node.DIAGNOSING.value
    assert store.path("r1").read_bytes() == before, \
        "BEHAVIOUR: a refused closing replan changed the state file"


def test_closing_replan_names_renorms_and_binds_the_obligation(store, plans, norms):
    f = _norm_file(norms)
    state = _bound_session(store, "r2", plans, f)
    assert [(e["target"], e["stage"], e["depth"]) for e in state.pending_renorms] == [(f, 1, 0)]
    assert state.pending_renorms[0]["baseline"] == _digest(f)
    assert state.difficulty is None


def test_stage_target_refused_while_norm_unchanged_admitted_once_edited(store, plans):
    _diagnosing(store, "r3", plans("base"))
    assert _critique(store, "r3", "нормативное", ["stage:1"]).ok is True
    _normalize(store, "r3")
    other_stage_only = plans("other", _stages(s2={"method": "write a regression test"}))
    d = _replan(store, "r3", other_stage_only)
    assert d.ok is False, "BEHAVIOUR: editing a different stage satisfied a stage:1 target"
    assert "stage:1" in _blockers(d)
    edited = plans("edited", _stages(s1={"method": "mirror the working caller"}))
    d = _replan(store, "r3", edited)
    assert d.ok is True, f"BEHAVIOUR: an edit of the targeted stage was refused: {d.detail}"
    assert store.load("r3").pending_renorms == []


def test_stage_target_shifted_by_an_inserted_stage_is_still_refused(store, plans):
    _diagnosing(store, "r4", plans("base"))
    assert _critique(store, "r4", "нормативное", ["stage:2"]).ok is True
    _normalize(store, "r4")
    shifted = plans("shifted", [
        _stage(1, "Scaffold module", "module file exists and imports cleanly",
               "python -c 'import mod' exits 0", [], ["mod.py"]),
        _stage(2, "Lint", "lint is clean", "ruff exits 0", [1], ["lint.txt"]),
        _stage(3, "Add tests", "pytest green for the new module",
               "pytest tests/test_mod.py green", [1], ["tests/test_mod.py"]),
    ])
    d = _replan(store, "r4", shifted)
    assert d.ok is False, "BEHAVIOUR: shifting the targeted stage to a new index passed the gate"
    assert "stage:2" in _blockers(d)


def test_reference_only_on_a_carried_passed_stage_is_refused(store, plans, norms):
    f = _norm_file(norms)
    _diagnosing(store, "r5", plans("base"), fail_stage=2)
    assert store.load("r5").stage(1).outcome.status == StageStatus.PASSED.value
    assert _critique(store, "r5", "нормативное", [f]).ok is True
    _normalize(store, "r5")
    on_passed = plans("on-passed", _stages(s1={"renorms": [f]}))
    d = _replan(store, "r5", on_passed)
    assert d.ok is False, "BEHAVIOUR: a reference on a carried-forward PASSED stage was admitted"
    assert "PASSED" in _blockers(d)
    on_pending = plans("on-pending", _stages(s2={"renorms": [f]}))
    d = _replan(store, "r5", on_pending)
    assert d.ok is True, f"BEHAVIOUR: a reference on the stage that will run was refused: {d.detail}"
    assert store.load("r5").pending_renorms[0]["stage"] == 2


def test_waiver_refused_for_target_bearing_record(store, fixtures_dir, norms):
    """negative control: --normalization-waiver is not an escape for a record that carries
    norm targets, whatever its failure_address label says."""
    f = _norm_file(norms)
    _diagnosing(store, "w1", str(fixtures_dir / "plan_two_stage.toml"))
    assert _critique(store, "w1", "ресурсное").ok is True
    _seed(store, "w1", targets=[_file_target(f, source="difficulty-close")])
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    d = _replan(store, "w1", refined, normalization_waiver="genuinely one-off")
    assert d.ok is False, "BEHAVIOUR: a normalization waiver closed a target-bearing record"
    state = store.load("w1")
    assert state.node == Node.DIAGNOSING.value
    assert not any(h.get("event") == "normalization_waived" for h in state.history), \
        "BEHAVIOUR: the refused waiver was recorded as taken"


def test_waiver_still_admitted_for_a_record_without_targets(store, fixtures_dir):
    _diagnosing(store, "w2", str(fixtures_dir / "plan_two_stage.toml"))
    assert _critique(store, "w2", "ресурсное").ok is True
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    d = _replan(store, "w2", refined, normalization_waiver="genuinely one-off")
    assert d.ok is True, "BEHAVIOUR: the waiver regressed for a record carrying no targets"
    assert any(h.get("event") == "normalization_waived" for h in store.load("w2").history)


def test_gate_keys_on_the_record_carrying_targets_not_its_label(store, plans, norms):
    f = _norm_file(norms)
    _diagnosing(store, "g1", plans("base"))
    assert _critique(store, "g1", "ресурсное").ok is True
    _seed(store, "g1", targets=[_file_target(f, source="difficulty-close")])
    _normalize(store, "g1")
    d = _replan(store, "g1", plans("without", _stages(s1={"method": "m2"})))
    assert d.ok is False, "BEHAVIOUR: a ресурсное record with inherited targets skipped the gate"
    d = _replan(store, "g1", plans("with", _stages(s1={"renorms": [f]})))
    assert d.ok is True
    assert store.load("g1").pending_renorms[0]["target"] == f


# --- the obligation ----------------------------------------------------------

def test_pass_refused_while_the_file_is_unedited_then_discharged(store, plans, norms):
    f = _norm_file(norms)
    state = _bound_session(store, "o1", plans, f)
    cli.cmd_next_stage(ns(session="o1"), store=store)
    before = store.path("o1").read_bytes()
    d = _pass(store, "o1", 1)
    assert d.ok is False, "BEHAVIOUR: the re-norming stage passed with its norm file unedited"
    assert f in _blockers(d)
    assert store.path("o1").read_bytes() == before, \
        "BEHAVIOUR: a refused PASS changed the state file"
    Path(f).write_text("v2\n", encoding="utf-8")
    d = _pass(store, "o1", 1)
    assert d.ok is True, f"BEHAVIOUR: the stage was refused after the file moved: {d.detail}"
    state = store.load("o1")
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert gates.open_renorms(state) == []


def test_file_edited_before_the_replan_leaves_no_obligation(store, plans, norms):
    f = _norm_file(norms)
    _diagnosing(store, "o2", plans("base"))
    assert _critique(store, "o2", "нормативное", [f]).ok is True
    _normalize(store, "o2")
    Path(f).write_text("v2\n", encoding="utf-8")
    d = _replan(store, "o2", plans("renorm", _stages(s1={"renorms": [f]})))
    assert d.ok is True
    assert store.load("o2").pending_renorms == [], \
        "BEHAVIOUR: an already-moved file was bound as an owed edit"
    cli.cmd_next_stage(ns(session="o2"), store=store)
    assert _pass(store, "o2", 1).ok is True


def test_refinement_dropping_the_pending_stage_is_refused(store, plans, norms):
    f = _norm_file(norms)
    _bound_session(store, "o3", plans, f)
    before = store.path("o3").read_bytes()
    for name, stage in (("dropped", {"method": "m2"}), ("emptied", {"renorms": []})):
        d = _replan(store, "o3", plans(name, _stages(s1=stage)))
        assert d.ok is False, f"BEHAVIOUR: a refinement that {name} the renorms reference applied"
        assert f in _blockers(d)
        assert store.path("o3").read_bytes() == before


def test_refinement_moving_the_reference_rebinds_the_obligation(store, plans, norms):
    f = _norm_file(norms)
    _bound_session(store, "o4", plans, f)
    d = _replan(store, "o4", plans("moved", _stages(s2={"renorms": [f]})))
    assert d.ok is True, f"BEHAVIOUR: moving the reference to another pending stage failed: {d.detail}"
    (entry,) = store.load("o4").pending_renorms
    assert entry["target"] == f and entry["stage"] == 2, \
        "BEHAVIOUR: the obligation stayed bound to the stage that no longer names it"


def test_obligation_survives_a_substantive_replan(store, plans, norms):
    f = _norm_file(norms)
    _bound_session(store, "o5", plans, f)
    bigger = plans("bigger", _stages(s1={"renorms": [f]}) + [
        _stage(3, "Wire CI", "CI config references test_mod", "grep test_mod ci.yml",
               [2], [".github/workflows/ci.yml"])])
    d = _replan(store, "o5", bigger)
    assert d.ok is True
    state = store.load("o5")
    assert state.node == Node.PLAN_READY.value
    assert [e["target"] for e in state.pending_renorms] == [f], \
        "BEHAVIOUR: a substantive replan discharged an obligation whose file is unedited"
    without = plans("bigger-without", _stages() + [
        _stage(3, "Wire CI", "CI config references test_mod", "grep test_mod ci.yml",
               [2], [".github/workflows/ci.yml"])])
    cli.cmd_approve(ns(session="o5", by="user"), store=store)
    refused = _replan(store, "o5", without)
    assert refused.ok is False, "BEHAVIOUR: a substantive replan dropped the open obligation"


def test_verify_final_and_resolve_refused_while_an_edit_is_owed(store, plans, norms):
    f = _norm_file(norms)
    _start(store, "v1", plans("base"))
    _pass(store, "v1", 1)
    cli.cmd_next_stage(ns(session="v1"), store=store)
    _pass(store, "v1", 2)
    state = store.load("v1")
    assert state.node == Node.VERIFYING.value
    owed = {"target": f, "baseline": _digest(f), "stage": 2, "depth": 0}
    state.pending_renorms = [owed]
    store.save(state)

    d = cli.cmd_verify_final(ns(session="v1"), store=store)
    assert d.ok is False, "BEHAVIOUR: verify-final passed with a norm edit still owed"
    assert f in _blockers(d) and "stage 2" in _blockers(d), \
        "BEHAVIOUR: the refusal does not name the target and its bound stage"
    assert store.load("v1").node == Node.VERIFYING.value

    Path(f).write_text("v2\n", encoding="utf-8")
    d = cli.cmd_verify_final(ns(session="v1"), store=store)
    assert d.ok is True and store.load("v1").pending_renorms == []

    state = store.load("v1")
    state.pending_renorms = [{**owed, "baseline": _digest(f)}]
    store.save(state)
    d = cli.cmd_resolve(ns(session="v1", by="user", quality=5, quality_by="user-confirmed",
                           quality_note=None), store=store)
    assert d.ok is False and f in _blockers(d), \
        "BEHAVIOUR: resolve closed the task with a norm edit still owed"


def test_previous_schema_state_loads_without_targets_or_obligations(store, plans):
    _diagnosing(store, "m1", plans("base"))
    data = store.load("m1").to_dict()
    data["schema_version"] = SCHEMA_VERSION - 1
    data.pop("pending_renorms", None)
    data["difficulty"].pop("norm_targets", None)
    for stage in data.get("stages") or []:
        stage.pop("renorms", None)
    restored = SessionState.from_dict(data)
    assert restored.schema_version == SCHEMA_VERSION - 1
    assert restored.pending_renorms == []
    assert restored.difficulty.norm_targets == []
    assert all(s.renorms == [] for s in restored.stages)
    assert restored.difficulty.complete() is False


# --- the stage baseline: the approved snapshot, not the live plan file --------

def test_stage_baseline_is_the_approved_snapshot_not_the_live_plan_file(store, plans):
    """The plan file is editable while DIAGNOSING; a baseline read from it lets an edit
    made before the critique become the bar, and a restore then pass as the "change"."""
    base = plans("base")
    approved = next(s for s in load_plan(base, strict=False).stages if s.index == 1)
    _diagnosing(store, "b1", base)
    assert store.load("b1").plan_snapshot_path, "setup: the session has no approved snapshot"
    _plan(Path(base), _stages(s1={"method": "edited in place while DIAGNOSING"}))
    assert _critique(store, "b1", "нормативное", ["stage:1"]).ok is True
    (entry,) = store.load("b1").difficulty.norm_targets
    assert entry["baseline"] == gates.stage_norm_digest(approved), \
        "BEHAVIOUR: the stage baseline was read from the live (edited) plan file"
    _plan(Path(base), _stages())
    _normalize(store, "b1")
    d = _replan(store, "b1", plans("restored"))
    assert d.ok is False, "BEHAVIOUR: edit-critique-restore passed a stage:1 target unchanged"
    assert "stage:1" in _blockers(d)


def test_plan_drafted_in_place_before_the_critique_is_not_a_false_block(store, plans):
    base = plans("base")
    _diagnosing(store, "b2", base)
    _plan(Path(base), _stages(s1={"method": "mirror the working caller"}))
    assert _critique(store, "b2", "нормативное", ["stage:1"]).ok is True
    _normalize(store, "b2")
    d = _replan(store, "b2", base)
    assert d.ok is True, \
        f"BEHAVIOUR: a corrected plan drafted in place was blocked as unchanged: {d.detail}"


def _populated_stage():
    """A stage with every norm-bearing leaf set to a distinct non-empty value."""
    return Stage(
        index=1,
        title="title",
        subject=Subject(material="material", result="result", invariants="invariants",
                        material_refs=["material-ref"], knowledge_refs=["knowledge-ref"]),
        means=Means(means="means", method="method", procedure="procedure"),
        actor=Actor(executor="spawn:developer", capability_required="capability",
                    cost_tier="small", guard_exempt_paths=["exempt/path"]),
        criterion=Criterion(criterion_type="measurable", done_criterion="done",
                            verify_command="verify", expected_exit=3, observation="seen",
                            verify_venue="delivery", verify_kind="shell",
                            landed=LandedSpec(target="main", delivered_stage=1),
                            verify_venue_at_final="repo_root",
                            negative_control="negative", negative_control_waiver="waiver"),
        principle=Principle(statement="statement", source="source", derivation="derivation",
                            confidence="high", refutation="refutation"),
        conditions="conditions",
        preconditions="preconditions",
        knowledge="knowledge",
        supplies=[Supply(on=2, element="element", artifact="artifact", delivery="report")],
        output_artifacts=["out/artifact"],
        ephemeral_artifacts_waiver="ephemeral",
        renorms=["/norm/file"],
        control="control",
    )


def _leaf_paths(obj, prefix=()):
    """Every settable leaf under a dataclass tree; an empty list is itself a leaf."""
    if is_dataclass(obj):
        for f in dc_fields(obj):
            yield from _leaf_paths(getattr(obj, f.name), prefix + (f.name,))
    elif isinstance(obj, list) and obj:
        for i, item in enumerate(obj):
            yield from _leaf_paths(item, prefix + (i,))
    else:
        yield prefix


def _step(parent, key):
    return parent[key] if isinstance(key, int) else getattr(parent, key)


def _get_leaf(root, path):
    for key in path:
        root = _step(root, key)
    return root


def _set_leaf(root, path, value):
    parent = root
    for key in path[:-1]:
        parent = _step(parent, key)
    if isinstance(path[-1], int):
        parent[path[-1]] = value
    else:
        setattr(parent, path[-1], value)


def _edited(value, path):
    if isinstance(value, str):
        return value + "-edited"
    if isinstance(value, int):
        return value + 1
    if value == []:
        return ["edited"]
    raise AssertionError(f"no edit is defined for the leaf {path} ({value!r}): populate it "
                         f"in _populated_stage or teach _edited its type")


def test_apply_refined_stage_fields_covers_every_norm_field():
    """The relation, not one instance of it: for every stage leaf `StageNorm.from_stage`
    reads, a replan that edits only that leaf must leave the live stage's norm equal to
    the norm of the stage loaded from the plan file. A `stage:<n>` norm target is
    snapshotted from the live stage and later compared with file-loaded stages, so a leaf
    `_apply_refined_stage_fields` fails to copy makes an unedited stage read as moved.

    The leaf set is not listed: each leaf of a fully populated stage is edited in turn and
    kept when the edit moves the `StageNorm`, so a field a later change makes the norm
    read is checked here without being named."""
    base = _populated_stage()
    base_norm = StageNorm.from_stage(base)
    norm_read, left_stale = [], []
    for path in _leaf_paths(base):
        if path == ("index",) or path[0] == "outcome":
            continue  # the key a live stage is matched by; the execution record
        value = _get_leaf(base, path)
        if value is None:
            assert path == ("grants",), f"populate {path} in _populated_stage"
            continue
        edited = deepcopy(base)
        _set_leaf(edited, path, _edited(value, path))
        if StageNorm.from_stage(edited) == base_norm:
            continue
        norm_read.append(path)
        live = deepcopy(base)
        cli._apply_refined_stage_fields(live, edited)
        if StageNorm.from_stage(live) != StageNorm.from_stage(edited):
            left_stale.append(path)

    for path in (("title",), ("actor", "capability_required"), ("principle", "statement"),
                 ("output_artifacts", 0)):
        assert path in norm_read, f"the walk no longer finds {path} norm-bearing"
    assert not left_stale, (
        f"BEHAVIOUR: _apply_refined_stage_fields leaves norm-read leaves stale: {left_stale}")


def _renormalized_then_failed(store, sid, plans, monkeypatch):
    """Approved with procedure P, `--renormalize`d to P', then stage 1 fails: the state's
    stage runs P' while the approved snapshot still holds P."""
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    approved = plans("p", _stages(s1={"procedure": "1. read 2. edit"}))
    _start(store, sid, approved)
    renormalized = plans("p-prime", _stages(s1={"procedure": "1. edit 2. read"}))
    d = _renormalize(store, sid, renormalized)
    assert d.ok is True, f"setup: the renormalization was refused: {d.detail}"
    _fail_and_investigate(store, sid)
    return approved, renormalized


def test_renormalized_stage_left_unchanged_is_not_a_false_discharge(store, plans, monkeypatch):
    """`--renormalize` leaves the snapshot on P while the live stage runs P'. A baseline
    taken from the snapshot made a closing plan that keeps P' read as an edit."""
    _, renormalized = _renormalized_then_failed(store, "n1", plans, monkeypatch)
    assert _critique(store, "n1", "нормативное", ["stage:1"]).ok is True
    (entry,) = store.load("n1").difficulty.norm_targets
    ran = next(s for s in load_plan(renormalized, strict=False).stages if s.index == 1)
    assert entry["baseline"] == gates.stage_norm_digest(ran), \
        "BEHAVIOUR: the baseline is not the digest of the stage that ran (P')"
    _normalize(store, "n1")
    d = _replan(store, "n1", renormalized)
    assert d.ok is False, \
        "BEHAVIOUR: a closing plan that leaves the failed renormalized stage unchanged passed"
    assert "stage:1" in _blockers(d)


def test_reverting_a_renormalized_stage_is_a_real_edit(store, plans, monkeypatch):
    """The reverse of the false discharge: P is an edit relative to the P' that ran, even
    though it equals what was approved."""
    approved, _ = _renormalized_then_failed(store, "n2", plans, monkeypatch)
    assert _critique(store, "n2", "нормативное", ["stage:1"]).ok is True
    _normalize(store, "n2")
    d = _replan(store, "n2", approved)
    assert d.ok is True, \
        f"BEHAVIOUR: reverting the renormalized stage was refused as unchanged: {d.detail}"


# --- the plan file the replan installs is no norm -----------------------------

def test_target_that_is_the_plan_the_replan_installs_is_refused(store, norms):
    first = _plan(norms / "x.toml", _stages())
    v2_path = norms / "x-v2.toml"
    v2 = _plan(v2_path, _stages(s1={"renorms": [str(v2_path)]}))
    _diagnosing(store, "i1", first)
    assert _critique(store, "i1", "нормативное", [v2]).ok is True
    _normalize(store, "i1")
    before = store.path("i1").read_bytes()
    d = _replan(store, "i1", v2)
    assert d.ok is False, "BEHAVIOUR: a replan installing its own norm target as the plan applied"
    assert "installs" in _blockers(d)
    assert store.path("i1").read_bytes() == before


def test_open_obligation_on_the_installed_plan_is_refused_even_after_its_bytes_moved(
        store, plans, norms):
    f = _plan(norms / "y.toml", _stages())
    _bound_session(store, "i2", plans, f)
    _plan(Path(f), _stages(s1={"renorms": [f]}))
    before = store.path("i2").read_bytes()
    d = _replan(store, "i2", f)
    assert d.ok is False, \
        "BEHAVIOUR: installing the obligation's file as the plan discharged it by moving it"
    assert "installs" in _blockers(d)
    assert store.path("i2").read_bytes() == before


def test_every_plan_file_the_session_installed_is_refused_as_a_target(store, plans, norms):
    _diagnosing(store, "i3", plans("v1"))
    assert _critique(store, "i3", "ресурсное").ok is True
    _normalize(store, "i3")
    v2 = plans("v2", _stages(s1={"method": "second method"}))
    assert _replan(store, "i3", v2).ok is True
    v3 = plans("v3", _stages(s1={"method": "third method"}))
    assert _replan(store, "i3", v3).ok is True
    state = store.load("i3")
    assert state.plan_path == v3
    for earlier in (plans("v1"), v2):
        assert Path(earlier).resolve() in {Path(p) for p in cli._session_plan_paths(state)}, \
            "BEHAVIOUR: a plan file the session once installed is not recognised as a plan"


# --- the sub-plan depth -------------------------------------------------------

def _owed(path, *, stage=1, depth=0):
    return {"target": path, "baseline": _digest(path), "stage": stage, "depth": depth}


def test_depth_scopes_each_obligation_gate(norms):
    f = _norm_file(norms)
    state = SessionState(session_id="dp", task_id="dp")
    state.pending_renorms = [_owed(f)]
    no_reference = SimpleNamespace(stages=[SimpleNamespace(index=1, renorms=[])])
    assert gates.pending_renorm_stage_blockers(state, 1)
    assert gates.pending_renorm_final_blockers(state)
    assert gates.renorm_retention_blockers(state, no_reference, [1])

    state.plan_stack = [_make_frame(0)]
    assert gates.pending_renorm_stage_blockers(state, 1) == [], \
        "BEHAVIOUR: a parent plan's obligation bound a stage index of the child plan"
    assert gates.pending_renorm_final_blockers(state) == [], \
        "BEHAVIOUR: a parent plan's obligation blocked the child's verify-final"
    assert gates.renorm_retention_blockers(state, no_reference, [1]) == [], \
        "BEHAVIOUR: a child plan was held to retaining the parent's obligation"

    rebinding = SimpleNamespace(stages=[SimpleNamespace(index=2, renorms=[f])])
    (kept,) = gates.bound_renorms(state, rebinding, [2])
    assert kept["stage"] == 1, "BEHAVIOUR: a child replan rebound the parent's obligation"
    state.plan_stack = []
    (rebound,) = gates.bound_renorms(state, rebinding, [2])
    assert rebound["stage"] == 2


def test_child_subplan_verifies_and_resolves_with_a_parent_obligation_open(
        store, plans, norms):
    f = _norm_file(norms)
    _start(store, "d1", plans("base"))
    _pass(store, "d1", 1)
    cli.cmd_next_stage(ns(session="d1"), store=store)
    _pass(store, "d1", 2)
    state = store.load("d1")
    owed = _owed(f, stage=2)
    state.pending_renorms = [owed]
    state.plan_stack = [_make_frame(0)]
    store.save(state)

    d = cli.cmd_verify_final(ns(session="d1"), store=store)
    assert d.ok is True, f"BEHAVIOUR: the child's verify-final was held by the parent: {d.detail}"
    assert store.load("d1").pending_renorms == [owed], \
        "BEHAVIOUR: the child's verify-final dropped the parent's open obligation"
    cli.cmd_plugin_record(ns(session="d1", plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session="d1", plugin="experience", phase="recorded"), store=store)
    d = cli.cmd_resolve(ns(session="d1", by="user", quality=5, quality_by="user-confirmed",
                           quality_note=None), store=store)
    assert d.ok is True, f"BEHAVIOUR: the child's resolve was held by the parent: {d.detail}"
    assert store.load("d1").pending_renorms == [owed], \
        "BEHAVIOUR: the child's resolve dropped the parent's open obligation"


def test_verify_final_refusal_is_logged_as_a_gate(store, plans, norms, tmp_path, monkeypatch):
    gate_log = tmp_path / "gates.jsonl"
    monkeypatch.setattr(cli, "GATE_LOG", gate_log)
    f = _norm_file(norms)
    _start(store, "d2", plans("base"))
    _pass(store, "d2", 1)
    cli.cmd_next_stage(ns(session="d2"), store=store)
    _pass(store, "d2", 2)
    state = store.load("d2")
    state.pending_renorms = [_owed(f, stage=2)]
    store.save(state)
    assert cli.cmd_verify_final(ns(session="d2"), store=store).ok is False
    rows = [json.loads(line) for line in gate_log.read_text(encoding="utf-8").splitlines()]
    (row,) = [r for r in rows if r["gate"] == "renorm_final"]
    assert row["passed"] is False and f in " ".join(row["blockers"]), \
        "BEHAVIOUR: the verify-final norm-edit refusal left no gate record"


# --- --renormalize and re-attest keep the obligation --------------------------

def _no_judge(argv, timeout=None):
    return RunResult(1, "", "no such model")


def _renormalize(store, sid, plan):
    return cli.cmd_replan(
        ns(session=sid, plan=plan, renormalize=True, coverage_waiver=None,
           normalization_waiver=None, cost_log=None),
        store=store, runner=_no_judge)


def _with_obligation(store, sid, entry):
    state = store.load(sid)
    state.pending_renorms = [entry]
    store.save(state)


def test_renormalize_keeps_a_named_obligation_and_refuses_an_orphaned_one(
        store, plans, norms, monkeypatch):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    f = _norm_file(norms)
    approved = plans("approved", _stages(s1={"renorms": [f], "procedure": "1. read 2. edit"}))
    _start(store, "rn1", approved)
    _with_obligation(store, "rn1", _owed(f))
    d = _renormalize(store, "rn1", plans(
        "resequenced", _stages(s1={"renorms": [f], "procedure": "1. edit 2. read"})))
    assert d.ok is True, f"BEHAVIOUR: a procedure-only edit was refused: {d.detail}"
    assert [e["target"] for e in store.load("rn1").pending_renorms] == [f], \
        "BEHAVIOUR: --renormalize dropped an open obligation"

    orphan = _norm_file(norms, "orphan.md")
    _with_obligation(store, "rn1", _owed(orphan))
    before = store.path("rn1").read_bytes()
    d = _renormalize(store, "rn1", plans(
        "resequenced-again", _stages(s1={"renorms": [f], "procedure": "1. edit 2. test"})))
    assert d.ok is False, "BEHAVIOUR: --renormalize applied with an obligation no stage names"
    assert orphan in _blockers(d)
    assert store.path("rn1").read_bytes() == before


def test_reattest_declines_a_stage_bound_to_an_unedited_norm(norms):
    f = _norm_file(norms)
    stage = _dev_stage()
    store = _Mem(_reattest_state(stage, reattest_stash=[_reattest_stash(stage)]))
    store.s.pending_renorms = [_owed(f, stage=stage.index)]
    runner = _runner(control_returncode=0)

    cli.cmd_dispatch(ns(session="ra", re_attest=True), store=store, runner=runner)

    declined = [e for e in store.s.history if e["event"] == "reattest_declined"]
    assert declined and "norm-edit obligation" in declined[-1]["reason"], \
        "BEHAVIOUR: a re-attest was not declined for an unedited norm file"
    assert store.s.stages[0].outcome.status == StageStatus.ACTIVE.value
    assert [c[0] for c in runner.calls] == ["python3"], \
        "BEHAVIOUR: the control was re-run for a stage whose norm edit is still owed"


# --- paths and missing files ---------------------------------------------------

def test_renorms_and_norm_target_compare_normalized(store, plans, norms):
    f = _norm_file(norms)
    messy_target = f"{norms}/./leaf.md"
    messy_renorm = f"{norms}//leaf.md"
    _diagnosing(store, "p1", plans("base"))
    assert _critique(store, "p1", "нормативное", [messy_target]).ok is True
    _normalize(store, "p1")
    d = _replan(store, "p1", plans("renorm", _stages(s1={"renorms": [messy_renorm]})))
    assert d.ok is True, f"BEHAVIOUR: equal paths written differently did not match: {d.detail}"
    assert [e["target"] for e in store.load("p1").pending_renorms] == [f]


def test_a_deleted_norm_file_counts_as_moved(store, plans, norms):
    f = _norm_file(norms)
    state = _bound_session(store, "p2", plans, f)
    assert [e["target"] for e in gates.open_renorms(state)] == [f]
    Path(f).unlink()
    assert gates.open_renorms(store.load("p2")) == [], \
        "BEHAVIOUR: a deleted norm file stayed an open obligation"
    cli.cmd_next_stage(ns(session="p2"), store=store)
    assert _pass(store, "p2", 1).ok is True


# --- compatibility with a pre-change state file --------------------------------

def _as_previous_schema(store, sid):
    path = store.path(sid)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = SCHEMA_VERSION - 1
    data.pop("pending_renorms", None)
    (data.get("difficulty") or {}).pop("norm_targets", None)
    for stage in data.get("stages") or []:
        stage.pop("renorms", None)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_previous_schema_state_file_loads_through_the_store(store, plans):
    _diagnosing(store, "m2", plans("base"))
    _as_previous_schema(store, "m2")
    state = store.load("m2")
    assert state.pending_renorms == []
    assert state.difficulty.norm_targets == []
    assert all(s.renorms == [] for s in state.stages)
    assert state.node == Node.DIAGNOSING.value


def test_inflight_normative_record_without_targets_still_closes(store, fixtures_dir):
    """A нормативное critique recorded before this change carries no targets: the closing
    replan, and the waiver, behave as they did."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _diagnosing(store, "m3", plan)
    assert _critique(store, "m3", "ресурсное").ok is True
    _seed(store, "m3", address="нормативное", targets=[])
    _as_previous_schema(store, "m3")
    _normalize(store, "m3")
    d = _replan(store, "m3", refined)
    assert d.ok is True, f"BEHAVIOUR: an in-flight нормативное record no longer closes: {d.detail}"
    assert store.load("m3").pending_renorms == []

    _diagnosing(store, "m4", plan)
    assert _critique(store, "m4", "ресурсное").ok is True
    _seed(store, "m4", address="нормативное", targets=[])
    _as_previous_schema(store, "m4")
    d = _replan(store, "m4", refined, normalization_waiver="genuinely one-off")
    assert d.ok is True, f"BEHAVIOUR: the waiver regressed for an in-flight record: {d.detail}"
