"""The difficulty stack: a difficulty record has an identity, a level and a parent, a
nested one is opened with `difficulty-open` and closed with `difficulty-close` (its norm
targets move to the parent), and no record is opened at or past `max-recursion-depth` --
neither inside one session nor across a spawn (`AGENTCTL_PARENT_DIFFICULTY`).

The tests marked "negative control" are the ones the stage's negative control runs against
the base tree: they drive entry points that exist there and read every new symbol through
`getattr` with a default, so on base they fail on their `BEHAVIOUR:` assertion."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from argparse import Namespace
from dataclasses import asdict
from pathlib import Path

import pytest

from agentctl import cli
from agentctl import state as state_module
from agentctl.state import Difficulty, Node, SessionState, StageStatus

PARENT_ENV = "AGENTCTL_PARENT_DIFFICULTY"
ESCALATE = "ESCALATE_TO_USER"
SPAWN_SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load_spawn_module():
    spec = importlib.util.spec_from_file_location("spawn_specialist_difficulty_stack", SPAWN_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SPAWN = _load_spawn_module()


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.delenv(PARENT_ENV, raising=False)
    root = tmp_path / "norms"
    root.mkdir()
    monkeypatch.setenv("AGENTCTL_NORM_ROOTS", str(root))
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    return root


def _cap() -> int:
    return cli.Thresholds().max_recursion_depth


def _executing(store, sid, venued_fixtures_dir, plan="plan_two_stage.toml"):
    """A substantive session at EXECUTING stage 1."""
    cli.cmd_start(ns(session=sid, task="difficulty-stack", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(venued_fixtures_dir / plan)), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _fail(store, sid):
    return cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)


def _diagnosing(store, sid, venued_fixtures_dir):
    _executing(store, sid, venued_fixtures_dir)
    return _fail(store, sid)


def _declare(store, sid):
    return cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)


def _investigate(store, sid):
    return cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                                  hypotheses=["h1", "h2"]), store=store)


def _critique(store, sid, failure_address="ресурсное", targets=None):
    kw = dict(session=sid, functional_ground="fg", replanning_task="rt",
              failure_address=failure_address)
    if targets:
        kw["norm_targets"] = list(targets)
    return cli.cmd_critique(ns(**kw), store=store)


def _normalize(store, sid, factor="reproducible cause"):
    return cli.cmd_normalize(ns(session=sid, factor=factor, level="note"), store=store)


def _waive(store, sid, reason="one-off, not reproducible", factor=None):
    return cli.cmd_normalize(ns(session=sid, factor=factor, level=None, destination=None,
                                normalization_waiver=reason), store=store)


def _work(store, sid, *, failure_address="ресурсное", targets=None, normalize=True):
    """Declare, investigate, critique and (by default) normalize the open record."""
    _declare(store, sid)
    _investigate(store, sid)
    _critique(store, sid, failure_address, targets)
    if normalize:
        _normalize(store, sid)


def _open(store, sid, origin="replan"):
    return cli.cmd_difficulty_open(ns(session=sid, origin=origin), store=store)


def _close(store, sid):
    return cli.cmd_difficulty_close(ns(session=sid), store=store)


def _replan(store, sid, plan, **kw):
    return cli.cmd_replan(ns(session=sid, plan=str(plan), **kw), store=store)


def _norm_file(tmp_path, name="norm.md", text="norm v1\n"):
    root = tmp_path / "norms"
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan_variant(tmp_path, venued_fixtures_dir, name, *edits, base="plan_two_stage.toml"):
    """A copy of a fixture plan with each (old, new) text edit applied once."""
    text = (venued_fixtures_dir / base).read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new, 1)
    path = tmp_path / "work" / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _stage2_only_edit(tmp_path, venued_fixtures_dir, name="stage2-only"):
    """A plan that edits stage 2's norm and leaves stage 1's untouched."""
    return _plan_variant(tmp_path, venued_fixtures_dir, name,
                         ('title = "Add tests"', 'title = "Add more tests"'))


def _renorms_plan(tmp_path, venued_fixtures_dir, path, name="renorms"):
    """The base plan with stage 1 declaring that it edits `path`."""
    return _plan_variant(
        tmp_path, venued_fixtures_dir, name,
        ('output_artifacts = ["mod.py"]',
         f'output_artifacts = ["mod.py"]\nrenorms = {json.dumps([str(path)])}'))


def _blockers(d) -> str:
    return " | ".join((d.data or {}).get("blockers", []))


def _with_child_target(store, tmp_path, venued_fixtures_dir, sid, *, parent_address, edit_after=False):
    """A DIAGNOSING session whose parent record was critiqued `parent_address` and whose
    closed nested child named a norm file; returns (file, child_uid)."""
    path = _norm_file(tmp_path)
    _diagnosing(store, sid, venued_fixtures_dir)
    _declare(store, sid)
    _investigate(store, sid)
    assert _critique(store, sid, parent_address).ok is True
    assert _open(store, sid).ok is True
    child_uid = store.load(sid).difficulty.uid
    _work(store, sid, failure_address="нормативное", targets=[str(path)])
    if edit_after:
        path.write_text("norm v2\n", encoding="utf-8")
    d = _close(store, sid)
    assert d.ok is True, d.detail
    return path, child_uid


# --- identity ----------------------------------------------------------------

def test_difficulty_record_has_identity_level_and_origin(store, venued_fixtures_dir):
    _diagnosing(store, "id1", venued_fixtures_dir)
    d = store.load("id1").difficulty
    assert getattr(d, "uid", None), "BEHAVIOUR: a difficulty record carries a stable uid"
    assert getattr(d, "level", None) == 0, "BEHAVIOUR: a root difficulty is level 0"
    assert getattr(d, "origin", None) == "stage", "BEHAVIOUR: a failed stage is origin `stage`"
    assert getattr(d, "parent_uid", "unset") is None, "BEHAVIOUR: a root has no parent"
    assert getattr(d, "children", None) == [], "BEHAVIOUR: a fresh record has no children"


def test_uid_is_stable_across_recritique(store, venued_fixtures_dir):
    _diagnosing(store, "id2", venued_fixtures_dir)
    _declare(store, "id2")
    _investigate(store, "id2")
    _critique(store, "id2")
    first = store.load("id2").difficulty.uid
    _critique(store, "id2")
    assert first, "BEHAVIOUR: a difficulty record carries a stable uid"
    assert store.load("id2").difficulty.uid == first, \
        "BEHAVIOUR: a re-critique changed the record's uid"


# --- migration ---------------------------------------------------------------

@pytest.mark.parametrize("version", [46, 47])
def test_older_state_files_load_with_stack_defaults(store, venued_fixtures_dir, version):
    _diagnosing(store, "mig", venued_fixtures_dir)
    _declare(store, "mig")
    _investigate(store, "mig")
    _critique(store, "mig", "нормативное", ["stage:1"])
    live = store.load("mig")
    data = asdict(live)
    for key in ("difficulty_stack", "parent_difficulty", "closed_difficulties"):
        data.pop(key, None)
    for key in ("uid", "parent_uid", "level", "origin", "children", "normalization_waiver"):
        data["difficulty"].pop(key, None)
    data["schema_version"] = version
    data["pending_renorms"] = [{"target": "stage:2", "kind": "stage", "baseline": "b"}]
    loaded = SessionState.from_dict(data)
    assert loaded.difficulty.uid
    assert loaded.difficulty.level == 0
    assert loaded.difficulty.origin == "stage"
    assert loaded.difficulty.parent_uid is None
    assert loaded.difficulty.children == []
    assert loaded.difficulty_stack == []
    assert loaded.parent_difficulty is None
    assert loaded.difficulty.norm_targets == live.difficulty.norm_targets
    assert loaded.pending_renorms == data["pending_renorms"]


def test_legacy_record_gets_the_same_uid_on_every_load(store, venued_fixtures_dir):
    """negative control: a record written before schema 48 names itself the same way each
    time it is loaded, so a spawn's exported uid is the one the next load finds."""
    _diagnosing(store, "mig-uid", venued_fixtures_dir)
    _declare(store, "mig-uid")
    legacy = asdict(store.load("mig-uid").difficulty)
    for key in ("uid", "parent_uid", "level", "origin", "children", "normalization_waiver"):
        legacy.pop(key, None)
    first = Difficulty.from_dict(dict(legacy)).uid
    second = Difficulty.from_dict(dict(legacy)).uid
    assert first == second, \
        "BEHAVIOUR: a legacy record was given a different uid on a second load"
    other = dict(legacy)
    other["declaration"] = {**legacy["declaration"], "mismatch": "another mismatch"}
    assert Difficulty.from_dict(other).uid != first, \
        "BEHAVIOUR: two different legacy records share one uid"


# --- nesting -----------------------------------------------------------------

def test_difficulty_open_nests_under_the_open_record(store, venued_fixtures_dir):
    _diagnosing(store, "n1", venued_fixtures_dir)
    _declare(store, "n1")
    root_uid = store.load("n1").difficulty.uid
    d = _open(store, "n1", "replan")
    assert d.ok, d.detail
    state = store.load("n1")
    assert state.difficulty.level == 1, "BEHAVIOUR: a nested record is one level below its parent"
    assert state.difficulty.parent_uid == root_uid, "BEHAVIOUR: a nested record names its parent"
    assert state.difficulty.origin == "replan", "BEHAVIOUR: a nested record carries its origin"
    assert state.difficulty.declaration is None
    assert [r.uid for r in state.difficulty_stack] == [root_uid], \
        "BEHAVIOUR: the parent was not suspended on the stack"
    assert state.difficulty_stack[0].declaration is not None, \
        "BEHAVIOUR: the suspended parent lost its declaration"


def test_difficulty_open_refused_outside_diagnosing(store, venued_fixtures_dir):
    _executing(store, "n2", venued_fixtures_dir)
    before = asdict(store.load("n2"))
    d = _open(store, "n2")
    assert d.ok is False, "BEHAVIOUR: difficulty-open was accepted outside DIAGNOSING"
    assert asdict(store.load("n2")) == before, \
        "BEHAVIOUR: a refused difficulty-open changed the state"


def test_replan_refused_while_a_nested_difficulty_is_open(store, venued_fixtures_dir):
    _diagnosing(store, "n3", venued_fixtures_dir)
    _work(store, "n3")
    _open(store, "n3", "normalize")
    nested_uid = store.load("n3").difficulty.uid
    d = _replan(store, "n3", venued_fixtures_dir / "plan_two_stage_refined.toml")
    assert d.ok is False, "BEHAVIOUR: replan ran with a nested difficulty still open"
    assert d.action == "difficulty_close"
    assert nested_uid in _blockers(d)
    assert store.load("n3").node == Node.DIAGNOSING.value
    assert len(store.load("n3").difficulty_stack) == 1


# --- closing -----------------------------------------------------------------

def test_close_moves_norm_targets_to_the_parent_and_resumes_it(store, venued_fixtures_dir):
    _diagnosing(store, "c1", venued_fixtures_dir)
    _declare(store, "c1")
    _investigate(store, "c1")
    parent_uid = store.load("c1").difficulty.uid
    _open(store, "c1")
    child_uid = store.load("c1").difficulty.uid
    _work(store, "c1", failure_address="нормативное", targets=["stage:1"])
    d = _close(store, "c1")
    assert d.ok, d.detail
    state = store.load("c1")
    assert state.difficulty.uid == parent_uid, "BEHAVIOUR: the parent was not resumed"
    assert state.difficulty_stack == []
    assert [c.uid for c in state.difficulty.children] == [child_uid], \
        "BEHAVIOUR: the closed record did not join its parent's children"
    moved = [t for t in state.difficulty.norm_targets if t["target"] == "stage:1"]
    assert len(moved) == 1, "BEHAVIOUR: the child's norm target did not move to the parent"
    assert moved[0]["source"] == child_uid, \
        "BEHAVIOUR: the moved target is not tagged with the closed record's uid"
    assert moved[0]["baseline"]


def test_unedited_stage_target_from_a_child_blocks_the_closing_replan(store, venued_fixtures_dir, tmp_path):
    """negative control: the target the child named is the parent's to deliver -- a closing
    replan that leaves stage 1's norm unedited is refused, one that edits it is applied."""
    _diagnosing(store, "c1b", venued_fixtures_dir)
    _declare(store, "c1b")
    _investigate(store, "c1b")
    _open(store, "c1b")
    _work(store, "c1b", failure_address="нормативное", targets=["stage:1"])
    assert _close(store, "c1b").ok is True
    _critique(store, "c1b", "ресурсное")
    _normalize(store, "c1b", factor="root factor")
    before = store.path("c1b").read_bytes()
    d = _replan(store, "c1b", _stage2_only_edit(tmp_path, venued_fixtures_dir))
    assert d.ok is False, \
        "BEHAVIOUR: a closing replan left the child's stage:1 target unedited and was applied"
    assert "stage:1" in _blockers(d)
    assert store.path("c1b").read_bytes() == before, \
        "BEHAVIOUR: a refused closing replan changed the state file"
    d = _replan(store, "c1b", venued_fixtures_dir / "plan_two_stage_refined.toml")
    assert d.ok is True, f"BEHAVIOUR: an edit of the targeted stage was refused: {d.detail}"
    assert store.load("c1b").pending_renorms == []


def test_close_moves_a_target_whose_norm_was_already_edited(store, venued_fixtures_dir, tmp_path):
    path = _norm_file(tmp_path)
    original = _digest(path)
    _diagnosing(store, "c2", venued_fixtures_dir)
    _declare(store, "c2")
    _investigate(store, "c2")
    _open(store, "c2")
    child_uid = store.load("c2").difficulty.uid
    _work(store, "c2", failure_address="нормативное", targets=[str(path)])
    path.write_text("norm v2\n", encoding="utf-8")
    d = _close(store, "c2")
    assert d.ok, d.detail
    moved = [t for t in store.load("c2").difficulty.norm_targets if t["target"] == str(path)]
    assert len(moved) == 1, "BEHAVIOUR: an already-edited target was not moved to the parent"
    assert moved[0]["baseline"] == original, \
        "BEHAVIOUR: the moved target's baseline is not the digest the child recorded"
    assert moved[0]["source"] == child_uid


def test_already_edited_file_target_still_needs_a_renorms_reference(store, venued_fixtures_dir, tmp_path):
    path = _norm_file(tmp_path)
    _diagnosing(store, "c2b", venued_fixtures_dir)
    _declare(store, "c2b")
    _investigate(store, "c2b")
    _open(store, "c2b")
    _work(store, "c2b", failure_address="нормативное", targets=[str(path)])
    path.write_text("norm v2\n", encoding="utf-8")
    assert _close(store, "c2b").ok is True
    _critique(store, "c2b", "ресурсное")
    _normalize(store, "c2b", factor="root factor")
    before = store.path("c2b").read_bytes()
    d = _replan(store, "c2b", venued_fixtures_dir / "plan_two_stage_refined.toml")
    assert d.ok is False, \
        "BEHAVIOUR: a closing replan with no renorms reference was applied for the child's file target"
    assert store.path("c2b").read_bytes() == before
    d = _replan(store, "c2b", _renorms_plan(tmp_path, venued_fixtures_dir, path))
    assert d.ok is True, f"BEHAVIOUR: a closing replan naming the file was refused: {d.detail}"
    assert store.load("c2b").node == Node.VERIFYING.value


def test_close_refused_for_an_incomplete_nested_record(store, venued_fixtures_dir):
    _diagnosing(store, "c3", venued_fixtures_dir)
    _work(store, "c3")
    _open(store, "c3")
    _declare(store, "c3")
    before = asdict(store.load("c3"))
    d = _close(store, "c3")
    assert d.ok is False, "BEHAVIOUR: an incomplete nested record was closed"
    assert d.data["blockers"]
    assert asdict(store.load("c3")) == before, "BEHAVIOUR: a refused close changed the state"


def test_close_without_failure_address_or_normalization_is_refused(store, venued_fixtures_dir):
    _diagnosing(store, "c4", venued_fixtures_dir)
    _work(store, "c4")
    _open(store, "c4")
    _declare(store, "c4")
    _investigate(store, "c4")
    _critique(store, "c4", failure_address=None)
    _normalize(store, "c4")
    unrouted = _close(store, "c4")
    assert unrouted.ok is False, "BEHAVIOUR: an unrouted nested record was closed"
    assert unrouted.action == "critique"
    assert len(store.load("c4").difficulty_stack) == 1
    _critique(store, "c4", failure_address="ресурсное")
    assert _close(store, "c4").ok is True


def test_close_without_normalization_is_refused(store, venued_fixtures_dir):
    _diagnosing(store, "c4b", venued_fixtures_dir)
    _work(store, "c4b")
    _open(store, "c4b")
    _work(store, "c4b", normalize=False)
    unnormalized = _close(store, "c4b")
    assert unnormalized.ok is False, "BEHAVIOUR: a nested record without a normalization was closed"
    assert unnormalized.action == "normalize"
    assert len(store.load("c4b").difficulty_stack) == 1


def test_close_with_no_nested_record_points_to_replan(store, venued_fixtures_dir):
    _diagnosing(store, "c5", venued_fixtures_dir)
    _work(store, "c5")
    d = _close(store, "c5")
    assert d.ok is False, "BEHAVIOUR: difficulty-close accepted a root record"
    assert d.action == "replan"
    assert store.load("c5").difficulty is not None


def test_close_outside_diagnosing_is_refused_and_changes_nothing(store, venued_fixtures_dir):
    _executing(store, "c5b", venued_fixtures_dir)
    before = asdict(store.load("c5b"))
    d = _close(store, "c5b")
    assert d.ok is False, "BEHAVIOUR: difficulty-close was accepted outside DIAGNOSING"
    assert d.action == "noop"
    assert "DIAGNOSING" in " ".join(d.data["blockers"])
    after = asdict(store.load("c5b"))
    after["history"] = before["history"]  # the refusal's own gate-log row is the only change
    assert after == before


def test_closing_replan_files_the_closed_root_with_its_children(store, venued_fixtures_dir):
    _diagnosing(store, "c8", venued_fixtures_dir)
    _declare(store, "c8")
    _investigate(store, "c8")
    root_uid = store.load("c8").difficulty.uid
    _open(store, "c8")
    child_uid = store.load("c8").difficulty.uid
    _work(store, "c8", failure_address="нормативное", targets=["stage:1"])
    assert _close(store, "c8").ok
    _critique(store, "c8")
    _normalize(store, "c8", factor="root factor")
    d = _replan(store, "c8", venued_fixtures_dir / "plan_two_stage_refined.toml")
    assert d.ok, d.detail
    state = store.load("c8")
    assert state.difficulty is None
    filed = [r for r in state.closed_difficulties if r["uid"] == root_uid]
    assert len(filed) == 1, "BEHAVIOUR: the closed root was not filed in closed_difficulties"
    assert [c["uid"] for c in filed[0]["children"]] == [child_uid], \
        "BEHAVIOUR: the filed root lost its closed child"
    assert state.stage(1).outcome.status == StageStatus.PENDING.value


# --- a child's target on a parent critiqued without a norm --------------------

@pytest.mark.parametrize("parent_address", ["ресурсное", "not_applicable"])
def test_child_file_target_binds_a_parent_that_was_not_critiqued_normative(
        store, venued_fixtures_dir, tmp_path, parent_address):
    """negative control: the gate keys on the record carrying a norm target, not on its
    `failure_address` label. A parent critiqued `parent_address` that inherited a file
    target from a closed child keeps it through a re-critique, and its closing replan
    must name the file in `renorms` -- no `--normalization-waiver` gets around that."""
    sid = f"f1-{parent_address}"
    path, child_uid = _with_child_target(
        store, tmp_path, venued_fixtures_dir, sid, parent_address=parent_address)
    assert _critique(store, sid, parent_address).ok is True
    kept = [t for t in store.load(sid).difficulty.norm_targets if t["target"] == str(path)]
    assert len(kept) == 1, "BEHAVIOUR: a re-critique dropped the inherited file target"
    assert kept[0]["source"] == child_uid, \
        "BEHAVIOUR: a re-critique re-sourced the inherited target as its own"
    _normalize(store, sid, factor="root factor")

    before = store.path(sid).read_bytes()
    refined = venued_fixtures_dir / "plan_two_stage_refined.toml"
    d = _replan(store, sid, refined)
    assert d.ok is False, \
        "BEHAVIOUR: a closing replan with no renorms reference was applied for an inherited file target"
    assert store.path(sid).read_bytes() == before, \
        "BEHAVIOUR: a refused closing replan changed the state file"

    d = _replan(store, sid, refined, normalization_waiver="genuinely one-off")
    assert d.ok is False, "BEHAVIOUR: a normalization waiver closed a target-bearing record"
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    assert not any(h.get("event") == "normalization_waived" for h in state.history), \
        "BEHAVIOUR: the refused waiver was recorded as taken"

    d = _replan(store, sid, _renorms_plan(tmp_path, venued_fixtures_dir, path))
    assert d.ok is True, f"BEHAVIOUR: a closing replan naming the file was refused: {d.detail}"
    bound = store.load(sid).pending_renorms
    assert [e["target"] for e in bound] == [str(path)], \
        "BEHAVIOUR: the inherited file target was not bound as an owed edit"


def test_replan_waiver_cannot_stand_in_for_the_renorm_of_a_target_bearing_record(
        store, venued_fixtures_dir, tmp_path):
    """negative control: with the renorms reference already satisfied, the
    --normalization-waiver is the only thing that could let the closing replan past the
    missing normalization -- and a record carrying an inherited norm target refuses it."""
    sid = "f1-waiver-only"
    path, _ = _with_child_target(store, tmp_path, venued_fixtures_dir, sid, parent_address="ресурсное")
    assert _critique(store, sid, "ресурсное").ok is True
    plan = _renorms_plan(tmp_path, venued_fixtures_dir, path)

    plain = _replan(store, sid, plan)
    assert plain.ok is False and plain.action == "normalize", \
        "BEHAVIOUR: the closing replan needs a recorded normalization before the waiver is in play"

    waived = _replan(store, sid, plan, normalization_waiver="genuinely one-off")
    assert waived.ok is False, \
        "BEHAVIOUR: a normalization waiver let a target-bearing record close without its normalization"
    assert "norm targets" in " ".join(waived.data.get("blockers", [])), \
        "BEHAVIOUR: the waiver refusal does not name the norm targets as its ground"
    assert store.load(sid).node == Node.DIAGNOSING.value


# --- target collision ---------------------------------------------------------

def test_collision_takes_the_childs_baseline(store, venued_fixtures_dir, tmp_path):
    """negative control: both records named the same file; the file moved between their
    snapshots. The parent ends up holding the closing child's entry, baseline included."""
    path = _norm_file(tmp_path)
    first = _digest(path)
    _diagnosing(store, "col", venued_fixtures_dir)
    _declare(store, "col")
    _investigate(store, "col")
    assert _critique(store, "col", "нормативное", [str(path)]).ok is True
    held = {t["target"]: t for t in store.load("col").difficulty.norm_targets}
    assert held[str(path)]["baseline"] == first
    path.write_text("norm v2\n", encoding="utf-8")
    second = _digest(path)
    assert _open(store, "col").ok is True
    child_uid = store.load("col").difficulty.uid
    _work(store, "col", failure_address="нормативное", targets=[str(path)])
    assert _close(store, "col").ok is True
    entries = [t for t in store.load("col").difficulty.norm_targets if t["target"] == str(path)]
    assert len(entries) == 1, "BEHAVIOUR: a colliding target was entered twice"
    assert entries[0]["source"] == child_uid
    assert entries[0]["baseline"] == second, \
        "BEHAVIOUR: a colliding target kept the parent's stale baseline instead of the child's"


# --- the normalization waiver --------------------------------------------------

def test_waiver_is_refused_for_a_target_bearing_nested_record(store, venued_fixtures_dir):
    _diagnosing(store, "c6", venued_fixtures_dir)
    _declare(store, "c6")
    _investigate(store, "c6")
    _open(store, "c6")
    _work(store, "c6", failure_address="нормативное", targets=["stage:1"], normalize=False)
    refused = _waive(store, "c6")
    assert refused.ok is False, "BEHAVIOUR: a waiver was recorded on a target-bearing record"
    after = store.load("c6")
    assert after.difficulty.normalization_waiver is None
    assert _close(store, "c6").ok is False, \
        "BEHAVIOUR: a target-bearing nested record closed without a normalization"
    assert len(store.load("c6").difficulty_stack) == 1


def test_waiver_recorded_before_a_target_appears_no_longer_closes(store, venued_fixtures_dir):
    _diagnosing(store, "c6b", venued_fixtures_dir)
    _declare(store, "c6b")
    _investigate(store, "c6b")
    _open(store, "c6b")
    _work(store, "c6b", normalize=False)
    assert _waive(store, "c6b").ok is True
    assert _critique(store, "c6b", "нормативное", ["stage:1"]).ok is True
    refused = _close(store, "c6b")
    assert refused.ok is False, \
        "BEHAVIOUR: a waiver taken earlier closed a record that then named a norm target"
    assert "stage:1" in _blockers(refused) or "norm target" in _blockers(refused)
    assert len(store.load("c6b").difficulty_stack) == 1


def test_waiver_closes_a_target_less_nested_record_and_is_archived(store, venued_fixtures_dir):
    _diagnosing(store, "c7", venued_fixtures_dir)
    _declare(store, "c7")
    _investigate(store, "c7")
    root_uid = store.load("c7").difficulty.uid
    _open(store, "c7")
    child_uid = store.load("c7").difficulty.uid
    _work(store, "c7", normalize=False)
    assert _waive(store, "c7", "one-off, not reproducible").ok is True
    assert any(h.get("event") == "normalization_waived" for h in store.load("c7").history)
    d = _close(store, "c7")
    assert d.ok is True, d.detail
    state = store.load("c7")
    assert state.difficulty_stack == []
    child = next(c for c in state.difficulty.children if c.uid == child_uid)
    assert child.normalization_waiver == "one-off, not reproducible", \
        "BEHAVIOUR: the closed record does not carry the waiver it closed on"
    _critique(store, "c7")
    _normalize(store, "c7", factor="root factor")
    d = _replan(store, "c7", venued_fixtures_dir / "plan_two_stage_refined.toml")
    assert d.ok is True, d.detail
    filed = next(r for r in store.load("c7").closed_difficulties if r["uid"] == root_uid)
    assert [c["normalization_waiver"] for c in filed["children"]] == ["one-off, not reproducible"], \
        "BEHAVIOUR: the archived record does not show the waiver"


def test_waiver_with_a_factor_or_empty_reason_is_refused(store, venued_fixtures_dir):
    _diagnosing(store, "c7b", venued_fixtures_dir)
    _declare(store, "c7b")
    _investigate(store, "c7b")
    _open(store, "c7b")
    _work(store, "c7b", normalize=False)
    assert _waive(store, "c7b", "reason", factor="a factor").ok is False, \
        "BEHAVIOUR: normalize accepted a --factor together with a --normalization-waiver"
    assert _waive(store, "c7b", "   ").ok is False, \
        "BEHAVIOUR: an empty waiver reason was recorded"
    assert store.load("c7b").difficulty.normalization_waiver is None


def test_waiver_is_refused_for_the_root_record_and_points_to_replan(store, venued_fixtures_dir):
    """The root record closes through `replan`, which never reads a stored waiver: taking
    one on `normalize` would log a passed waiver that the closing replan then refuses."""
    _diagnosing(store, "c7r", venued_fixtures_dir)
    _work(store, "c7r", normalize=False)
    before = store.path("c7r").read_bytes()
    d = _waive(store, "c7r")
    assert d.ok is False, "BEHAVIOUR: normalize --normalization-waiver was accepted on the root record"
    assert "replan" in _blockers(d), "BEHAVIOUR: the root-record refusal does not point to replan"
    assert store.path("c7r").read_bytes() == before, \
        "BEHAVIOUR: a refused root waiver changed the state file"


def test_closing_replan_files_its_own_waiver_on_the_archived_root(store, venued_fixtures_dir):
    """The root's waiver is taken on `replan`; the filed record must show it the way a
    nested record's waiver is shown."""
    _diagnosing(store, "c7w", venued_fixtures_dir)
    _work(store, "c7w", normalize=False)
    root_uid = store.load("c7w").difficulty.uid
    d = _replan(store, "c7w", venued_fixtures_dir / "plan_two_stage_refined.toml",
                normalization_waiver="one-off root factor")
    assert d.ok is True, d.detail
    state = store.load("c7w")
    filed = next(r for r in state.closed_difficulties if r["uid"] == root_uid)
    assert filed["normalization_waiver"] == "one-off root factor", \
        "BEHAVIOUR: the archived root does not show the waiver its closing replan took"
    assert filed["normalization"] is None


def test_closing_replan_with_a_factor_files_no_waiver_on_the_root(store, venued_fixtures_dir):
    _diagnosing(store, "c7x", venued_fixtures_dir)
    _work(store, "c7x")
    root_uid = store.load("c7x").difficulty.uid
    d = _replan(store, "c7x", venued_fixtures_dir / "plan_two_stage_refined.toml",
                normalization_waiver="not needed")
    assert d.ok is True, d.detail
    filed = next(r for r in store.load("c7x").closed_difficulties if r["uid"] == root_uid)
    assert filed["normalization_waiver"] is None, \
        "BEHAVIOUR: a waiver the replan did not need was filed on the root"


def test_refused_waiver_gate_row_carries_the_blocker_text(store, venued_fixtures_dir, tmp_path, monkeypatch):
    log = tmp_path / "gate-log.jsonl"
    monkeypatch.setattr(cli, "GATE_LOG", log)
    sid = "c7g"
    _with_child_target(store, tmp_path, venued_fixtures_dir, sid, parent_address="ресурсное")
    d = _replan(store, sid, venued_fixtures_dir / "plan_two_stage_refined.toml",
                normalization_waiver="genuinely one-off")
    assert d.ok is False
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    waiver_rows = [r for r in rows if r["gate"] == "normalization_waiver" and r["passed"] is False]
    assert waiver_rows, "BEHAVIOUR: the refused waiver left no gate row"
    assert waiver_rows[-1]["blockers"] == d.data["blockers"], \
        "BEHAVIOUR: the waiver gate row's blockers differ from the refusal's own text"


def test_waiver_outside_a_difficulty_cycle_is_refused(store, venued_fixtures_dir):
    _executing(store, "c7c", venued_fixtures_dir)
    before = asdict(store.load("c7c"))
    d = _waive(store, "c7c")
    assert d.ok is False, "BEHAVIOUR: a normalization waiver was accepted outside DIAGNOSING"
    assert asdict(store.load("c7c")) == before


def test_difficulty_close_takes_no_waiver_argument():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["difficulty-close", "--session", "s", "--normalization-waiver", "x"])
    args = parser.parse_args(["normalize", "--session", "s", "--normalization-waiver", "x"])
    assert args.normalization_waiver == "x"


# --- the depth cap -----------------------------------------------------------

def test_difficulty_open_at_the_cap_leaves_state_unchanged(store, venued_fixtures_dir):
    _diagnosing(store, "cap1", venued_fixtures_dir)
    for _ in range(_cap() - 1):
        assert _open(store, "cap1").ok
    assert store.load("cap1").difficulty.level == _cap() - 1
    before = asdict(store.load("cap1"))
    d = _open(store, "cap1")
    assert d.ok is False, "BEHAVIOUR: a difficulty was opened at the depth cap"
    assert d.marker == ESCALATE
    assert asdict(store.load("cap1")) == before, \
        "BEHAVIOUR: a refused difficulty-open at the cap changed the state"


def test_child_failed_result_at_cap_escalates(store, venued_fixtures_dir, monkeypatch):
    """negative control: a child whose inherited level is at the cap records its stage
    result and the DIAGNOSING transition, but no difficulty record, and escalates."""
    monkeypatch.setenv(PARENT_ENV, f"parent-session:aaaa00000001:{_cap() - 1}")
    _executing(store, "cap2", venued_fixtures_dir)
    d = _fail(store, "cap2")
    state = store.load("cap2")
    assert state.node == Node.DIAGNOSING.value
    assert state.stage(1).outcome.status == StageStatus.FAILED.value
    assert d.marker == ESCALATE, \
        f"BEHAVIOUR: a result failed at the depth cap escalates to the user; marker={d.marker!r}"
    assert state.difficulty is None, \
        "BEHAVIOUR: no difficulty record is opened at the depth cap"


def test_declare_behind_a_withheld_record_is_refused_at_the_cap(store, venued_fixtures_dir, monkeypatch):
    monkeypatch.setenv(PARENT_ENV, f"parent-session:aaaa00000001:{_cap() - 1}")
    _executing(store, "cap3", venued_fixtures_dir)
    _fail(store, "cap3")
    d = _declare(store, "cap3")
    assert d.ok is False, "BEHAVIOUR: declare opened a record at the depth cap"
    assert d.marker == ESCALATE
    assert store.load("cap3").difficulty is None


def test_child_below_the_cap_still_records_its_difficulty(store, venued_fixtures_dir, monkeypatch):
    monkeypatch.setenv(PARENT_ENV, f"parent-session:aaaa00000001:{_cap() - 2}")
    _executing(store, "cap4", venued_fixtures_dir)
    d = _fail(store, "cap4")
    assert d.marker == "OVERCOME-DIFFICULTY"
    assert store.load("cap4").difficulty.level == _cap() - 1, \
        "BEHAVIOUR: a child below the cap did not record its difficulty at the inherited level"


# --- across a spawn ----------------------------------------------------------

def test_child_difficulty_inherits_parent_level(store, venued_fixtures_dir, monkeypatch):
    """negative control: a root difficulty of a spawned session is numbered from the
    spawning session's open difficulty (`child` origin, its uid as parent)."""
    monkeypatch.setenv(PARENT_ENV, "parent-session:abc123def456:1")
    _diagnosing(store, "ch1", venued_fixtures_dir)
    d = store.load("ch1").difficulty
    assert getattr(d, "level", None) == 2, \
        f"BEHAVIOUR: a child's root difficulty is one level below its parent's; level={getattr(d, 'level', None)!r}"
    assert getattr(d, "origin", None) == "child", "BEHAVIOUR: a child's root has origin `child`"
    assert getattr(d, "parent_uid", None) == "abc123def456", \
        "BEHAVIOUR: a child's root names the spawning difficulty as its parent"
    inherited = getattr(store.load("ch1"), "parent_difficulty", None)
    assert inherited == {"session": "parent-session", "uid": "abc123def456", "level": 1}, \
        f"BEHAVIOUR: the spawning difficulty was not recorded on the child session: {inherited!r}"


@pytest.mark.parametrize("raw", ["garbage", "a:b", "s:uid:notanumber", ":uid:2", "s::2"])
def test_malformed_parent_difficulty_is_ignored(store, venued_fixtures_dir, monkeypatch, capsys, raw):
    monkeypatch.setenv(PARENT_ENV, raw)
    _diagnosing(store, "ch2", venued_fixtures_dir)
    state = store.load("ch2")
    assert state.difficulty.level == 0, "BEHAVIOUR: a malformed parent value shifted the level"
    assert state.difficulty.origin == "stage"
    assert getattr(state, "parent_difficulty", None) is None
    assert PARENT_ENV in capsys.readouterr().err, \
        "BEHAVIOUR: a malformed parent value was ignored silently"


def test_reset_without_the_spawn_env_keeps_the_parent_link(store, venued_fixtures_dir, monkeypatch):
    """negative control: a session spawned under a parent difficulty stays under it across
    a `reset`, which runs without the spawn's environment."""
    monkeypatch.setenv(PARENT_ENV, "parent-session:abc123def456:1")
    _diagnosing(store, "ch3", venued_fixtures_dir)
    monkeypatch.delenv(PARENT_ENV)
    d = cli.cmd_reset(ns(session="ch3", task="difficulty-stack", goal="", done_criterion="",
                         criterion_type="measurable", recursion_depth=0, force=True),
                      store=store)
    assert d.ok is True, d.detail
    inherited = getattr(store.load("ch3"), "parent_difficulty", None)
    assert inherited == {"session": "parent-session", "uid": "abc123def456", "level": 1}, \
        f"BEHAVIOUR: a reset dropped the session's parent difficulty link: {inherited!r}"


def test_reset_to_another_task_drops_the_parent_link(store, venued_fixtures_dir, monkeypatch):
    """A resumed child session that starts unrelated work is not nested under the parent
    difficulty it was spawned for."""
    monkeypatch.setenv(PARENT_ENV, "parent-session:abc123def456:1")
    _diagnosing(store, "ch3b", venued_fixtures_dir)
    monkeypatch.delenv(PARENT_ENV)
    d = cli.cmd_reset(ns(session="ch3b", task="an-unrelated-task", goal="", done_criterion="",
                         criterion_type="measurable", recursion_depth=0, force=True),
                      store=store)
    assert d.ok is True, d.detail
    assert store.load("ch3b").parent_difficulty is None, \
        "BEHAVIOUR: a reset to a different task kept the old parent difficulty link"


def test_env_format_and_parse_live_in_one_module():
    assert cli.PARENT_DIFFICULTY_ENV == state_module.PARENT_DIFFICULTY_ENV == PARENT_ENV
    assert SPAWN.PARENT_DIFFICULTY_ENV == state_module.PARENT_DIFFICULTY_ENV
    raw = state_module.format_parent_difficulty("a:b", "uid1", 3)
    assert state_module.parse_parent_difficulty(raw) == {"session": "a:b", "uid": "uid1", "level": 3}
    assert state_module.parse_parent_difficulty("nonsense") is None


class _Launched(Exception):
    def __init__(self, env):
        super().__init__("launched")
        self.env = env


def _spawned_env(monkeypatch, tmp_path, venued_fixtures_dir, session, inherited=None):
    """The environment `spawn-specialist.py main` hands the child `claude -p` process."""
    monkeypatch.setattr(SPAWN.shutil, "which", lambda *a, **k: "/usr/bin/claude")

    def launch(cmd, env=None, **kw):
        raise _Launched(dict(env or {}))

    monkeypatch.setattr(SPAWN.proc_tree, "launch_supervised", launch)
    if inherited is None:
        monkeypatch.delenv(PARENT_ENV, raising=False)
    else:
        monkeypatch.setenv(PARENT_ENV, inherited)
    with pytest.raises(_Launched) as info:
        SPAWN.main([
            "--kind", "thinker", "--plan", str(venued_fixtures_dir / "plan_two_stage.toml"),
            "--done-criterion", "a verdict", "--criterion-type", "measurable",
            "--complexity", "medium", "--effort", "medium",
            "--session", session, "--state-root", str(tmp_path / "state"),
            "--workdir", str(tmp_path),
        ])
    return info.value.env


def test_spawn_exports_parent_difficulty(store, venued_fixtures_dir, tmp_path, monkeypatch):
    """negative control: a spawn for a session with an open difficulty tells the child
    engine which one (`<session>:<uid>:<level>`)."""
    _diagnosing(store, "sp1", venued_fixtures_dir)
    open_record = store.load("sp1").difficulty
    uid = getattr(open_record, "uid", "")
    level = getattr(open_record, "level", 0)
    env = _spawned_env(monkeypatch, tmp_path, venued_fixtures_dir, "sp1")
    exported = env.get(PARENT_ENV)
    assert exported is not None, \
        "BEHAVIOUR: a spawn for a session with an open difficulty exports it to the child"
    assert exported == f"sp1:{uid}:{level}", f"BEHAVIOUR: exported {exported!r}"


def test_spawn_without_an_open_difficulty_exports_nothing(store, venued_fixtures_dir, tmp_path, monkeypatch):
    _executing(store, "sp2", venued_fixtures_dir)
    env = _spawned_env(monkeypatch, tmp_path, venued_fixtures_dir, "sp2",
                       inherited="grandparent:ffff00000000:0")
    assert PARENT_ENV not in env, \
        "BEHAVIOUR: a spawn with no open difficulty passed a stale ancestor value on"


def test_spawn_for_an_unknown_session_still_launches(store, venued_fixtures_dir, tmp_path, monkeypatch):
    env = _spawned_env(monkeypatch, tmp_path, venued_fixtures_dir, "no-such-session")
    assert PARENT_ENV not in env


def test_spawn_for_an_unloadable_session_still_launches(store, venued_fixtures_dir, tmp_path, monkeypatch):
    """negative control: a state file the engine cannot build a SessionState from (here a
    section with a field this engine does not know) must not fail the spawn."""
    _diagnosing(store, "sp4", venued_fixtures_dir)
    _declare(store, "sp4")
    path = store.path("sp4")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["difficulty"]["declaration"]["unknown_field"] = 1
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(Exception):
        store.load("sp4")
    env = _spawned_env(monkeypatch, tmp_path, venued_fixtures_dir, "sp4")
    assert PARENT_ENV not in env


def test_exported_value_round_trips_into_the_child_engine(store, venued_fixtures_dir, tmp_path, monkeypatch):
    _diagnosing(store, "sp3", venued_fixtures_dir)
    env = _spawned_env(monkeypatch, tmp_path, venued_fixtures_dir, "sp3")
    parent = store.load("sp3").difficulty
    monkeypatch.setenv(PARENT_ENV, env[PARENT_ENV])
    _diagnosing(store, "sp3-child", venued_fixtures_dir)
    child = store.load("sp3-child").difficulty
    assert child.level == parent.level + 1
    assert child.parent_uid == parent.uid
    assert child.origin == "child"


# --- surface -----------------------------------------------------------------

def test_new_verbs_are_user_authority_session_commands():
    from lib.widening_targets import AGENTCTL_USER_AUTHORITY_VERBS
    for verb in ("difficulty-open", "difficulty-close"):
        assert verb in AGENTCTL_USER_AUTHORITY_VERBS
        assert verb in cli._SESSION_COMMANDS
