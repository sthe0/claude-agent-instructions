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
from argparse import Namespace
from dataclasses import asdict
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.state import Node, SessionState, StageStatus

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


def _executing(store, sid, fixtures_dir, plan="plan_two_stage.toml"):
    """A substantive session at EXECUTING stage 1."""
    cli.cmd_start(ns(session=sid, task="difficulty-stack", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(fixtures_dir / plan)), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _fail(store, sid):
    return cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)


def _diagnosing(store, sid, fixtures_dir):
    _executing(store, sid, fixtures_dir)
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


def _work(store, sid, *, failure_address="ресурсное", targets=None, normalize=True):
    """Declare, investigate, critique and (by default) normalize the open record."""
    _declare(store, sid)
    _investigate(store, sid)
    _critique(store, sid, failure_address, targets)
    if normalize:
        _normalize(store, sid)


def _open(store, sid, origin="replan"):
    return cli.cmd_difficulty_open(ns(session=sid, origin=origin), store=store)


def _close(store, sid, waiver=None):
    return cli.cmd_difficulty_close(ns(session=sid, normalization_waiver=waiver), store=store)


def _norm_file(tmp_path, name="norm.md", text="norm v1\n"):
    root = tmp_path / "norms"
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- identity ----------------------------------------------------------------

def test_difficulty_record_has_identity_level_and_origin(store, fixtures_dir):
    _diagnosing(store, "id1", fixtures_dir)
    d = store.load("id1").difficulty
    assert getattr(d, "uid", None), "BEHAVIOUR: a difficulty record carries a stable uid"
    assert getattr(d, "level", None) == 0, "BEHAVIOUR: a root difficulty is level 0"
    assert getattr(d, "origin", None) == "stage", "BEHAVIOUR: a failed stage is origin `stage`"
    assert getattr(d, "parent_uid", "unset") is None, "BEHAVIOUR: a root has no parent"
    assert getattr(d, "children", None) == [], "BEHAVIOUR: a fresh record has no children"


def test_uid_is_stable_across_recritique(store, fixtures_dir):
    _diagnosing(store, "id2", fixtures_dir)
    _declare(store, "id2")
    _investigate(store, "id2")
    _critique(store, "id2")
    first = store.load("id2").difficulty.uid
    _critique(store, "id2")
    assert store.load("id2").difficulty.uid == first
    assert first


# --- migration ---------------------------------------------------------------

@pytest.mark.parametrize("version", [46, 47])
def test_older_state_files_load_with_stack_defaults(store, fixtures_dir, version):
    _diagnosing(store, "mig", fixtures_dir)
    _declare(store, "mig")
    _investigate(store, "mig")
    _critique(store, "mig", "нормативное", ["stage:1"])
    live = store.load("mig")
    data = asdict(live)
    for key in ("difficulty_stack", "parent_difficulty", "closed_difficulties"):
        data.pop(key, None)
    for key in ("uid", "parent_uid", "level", "origin", "children"):
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


# --- nesting -----------------------------------------------------------------

def test_difficulty_open_nests_under_the_open_record(store, fixtures_dir):
    _diagnosing(store, "n1", fixtures_dir)
    _declare(store, "n1")
    root_uid = store.load("n1").difficulty.uid
    d = _open(store, "n1", "replan")
    assert d.ok, d.detail
    state = store.load("n1")
    assert state.difficulty.level == 1
    assert state.difficulty.parent_uid == root_uid
    assert state.difficulty.origin == "replan"
    assert state.difficulty.declaration is None
    assert [r.uid for r in state.difficulty_stack] == [root_uid]
    assert state.difficulty_stack[0].declaration is not None  # suspended, not overwritten


def test_difficulty_open_refused_outside_diagnosing(store, fixtures_dir):
    _executing(store, "n2", fixtures_dir)
    before = asdict(store.load("n2"))
    d = _open(store, "n2")
    assert d.ok is False
    assert asdict(store.load("n2")) == before


def test_replan_refused_while_a_nested_difficulty_is_open(store, fixtures_dir):
    _diagnosing(store, "n3", fixtures_dir)
    _work(store, "n3")
    _open(store, "n3", "normalize")
    nested_uid = store.load("n3").difficulty.uid
    d = cli.cmd_replan(ns(session="n3", plan=str(fixtures_dir / "plan_two_stage_refined.toml")),
                       store=store)
    assert d.ok is False
    assert d.action == "difficulty_close"
    assert nested_uid in " ".join(d.data["blockers"])
    assert store.load("n3").node == Node.DIAGNOSING.value
    assert len(store.load("n3").difficulty_stack) == 1


# --- closing -----------------------------------------------------------------

def test_close_moves_norm_targets_to_the_parent_and_resumes_it(store, fixtures_dir):
    _diagnosing(store, "c1", fixtures_dir)
    _declare(store, "c1")
    _investigate(store, "c1")
    parent_uid = store.load("c1").difficulty.uid
    _open(store, "c1")
    child_uid = store.load("c1").difficulty.uid
    _work(store, "c1", failure_address="нормативное", targets=["stage:1"])
    d = _close(store, "c1")
    assert d.ok, d.detail
    state = store.load("c1")
    assert state.difficulty.uid == parent_uid
    assert state.difficulty_stack == []
    assert [c.uid for c in state.difficulty.children] == [child_uid]
    moved = [t for t in state.difficulty.norm_targets if t["target"] == "stage:1"]
    assert len(moved) == 1
    assert moved[0]["source"] == child_uid
    assert moved[0]["baseline"]


def test_close_moves_a_target_whose_norm_was_already_edited(store, fixtures_dir, tmp_path):
    path = _norm_file(tmp_path)
    original = _digest(path)
    _diagnosing(store, "c2", fixtures_dir)
    _declare(store, "c2")
    _investigate(store, "c2")
    _open(store, "c2")
    child_uid = store.load("c2").difficulty.uid
    _work(store, "c2", failure_address="нормативное", targets=[str(path)])
    path.write_text("norm v2\n", encoding="utf-8")
    d = _close(store, "c2")
    assert d.ok, d.detail
    moved = [t for t in store.load("c2").difficulty.norm_targets if t["target"] == str(path)]
    assert len(moved) == 1
    assert moved[0]["baseline"] == original
    assert moved[0]["source"] == child_uid


def test_close_refused_for_an_incomplete_nested_record(store, fixtures_dir):
    _diagnosing(store, "c3", fixtures_dir)
    _work(store, "c3")
    _open(store, "c3")
    _declare(store, "c3")
    before = asdict(store.load("c3"))
    d = _close(store, "c3")
    assert d.ok is False
    assert d.data["blockers"]
    assert asdict(store.load("c3")) == before


def test_close_without_failure_address_or_normalization_is_refused(store, fixtures_dir):
    _diagnosing(store, "c4", fixtures_dir)
    _work(store, "c4")
    _open(store, "c4")
    _declare(store, "c4")
    _investigate(store, "c4")
    _critique(store, "c4", failure_address=None)
    _normalize(store, "c4")
    unrouted = _close(store, "c4")
    assert unrouted.ok is False
    assert unrouted.action == "critique"
    assert len(store.load("c4").difficulty_stack) == 1
    _critique(store, "c4", failure_address="ресурсное")
    assert _close(store, "c4").ok is True


def test_close_without_normalization_is_refused(store, fixtures_dir):
    _diagnosing(store, "c4b", fixtures_dir)
    _work(store, "c4b")
    _open(store, "c4b")
    _work(store, "c4b", normalize=False)
    unnormalized = _close(store, "c4b")
    assert unnormalized.ok is False
    assert unnormalized.action == "normalize"
    assert len(store.load("c4b").difficulty_stack) == 1


def test_close_with_no_nested_record_points_to_replan(store, fixtures_dir):
    _diagnosing(store, "c5", fixtures_dir)
    _work(store, "c5")
    d = _close(store, "c5")
    assert d.ok is False
    assert d.action == "replan"
    assert store.load("c5").difficulty is not None


def test_waiver_is_refused_for_a_target_bearing_nested_record(store, fixtures_dir):
    _diagnosing(store, "c6", fixtures_dir)
    _declare(store, "c6")
    _investigate(store, "c6")
    _open(store, "c6")
    _work(store, "c6", failure_address="нормативное", targets=["stage:1"], normalize=False)
    refused = _close(store, "c6", waiver="one-off")
    assert refused.ok is False
    after = store.load("c6")
    assert len(after.difficulty_stack) == 1
    assert after.difficulty.norm_targets


def test_waiver_is_accepted_for_a_target_less_nested_record(store, fixtures_dir):
    _diagnosing(store, "c7", fixtures_dir)
    _declare(store, "c7")
    _investigate(store, "c7")
    _open(store, "c7")
    _work(store, "c7", normalize=False)
    assert _close(store, "c7", waiver="one-off, not reproducible").ok is True
    assert store.load("c7").difficulty_stack == []


def test_closing_replan_files_the_closed_root_with_its_children(store, fixtures_dir):
    _diagnosing(store, "c8", fixtures_dir)
    _declare(store, "c8")
    _investigate(store, "c8")
    root_uid = store.load("c8").difficulty.uid
    _open(store, "c8")
    child_uid = store.load("c8").difficulty.uid
    _work(store, "c8", failure_address="нормативное", targets=["stage:1"])
    assert _close(store, "c8").ok
    _critique(store, "c8")
    _normalize(store, "c8", factor="root factor")
    d = cli.cmd_replan(ns(session="c8", plan=str(fixtures_dir / "plan_two_stage_refined.toml")),
                       store=store)
    assert d.ok, d.detail
    state = store.load("c8")
    assert state.difficulty is None
    filed = [r for r in state.closed_difficulties if r["uid"] == root_uid]
    assert len(filed) == 1
    assert [c["uid"] for c in filed[0]["children"]] == [child_uid]
    assert state.stage(1).outcome.status == StageStatus.PENDING.value


# --- the depth cap -----------------------------------------------------------

def test_difficulty_open_at_the_cap_leaves_state_unchanged(store, fixtures_dir):
    _diagnosing(store, "cap1", fixtures_dir)
    for _ in range(_cap() - 1):
        assert _open(store, "cap1").ok
    assert store.load("cap1").difficulty.level == _cap() - 1
    before = asdict(store.load("cap1"))
    d = _open(store, "cap1")
    assert d.ok is False
    assert d.marker == ESCALATE
    assert asdict(store.load("cap1")) == before


def test_child_failed_result_at_cap_escalates(store, fixtures_dir, monkeypatch):
    """negative control: a child whose inherited level is at the cap records its stage
    result and the DIAGNOSING transition, but no difficulty record, and escalates."""
    monkeypatch.setenv(PARENT_ENV, f"parent-session:aaaa00000001:{_cap() - 1}")
    _executing(store, "cap2", fixtures_dir)
    d = _fail(store, "cap2")
    state = store.load("cap2")
    assert state.node == Node.DIAGNOSING.value
    assert state.stage(1).outcome.status == StageStatus.FAILED.value
    assert d.marker == ESCALATE, \
        f"BEHAVIOUR: a result failed at the depth cap escalates to the user; marker={d.marker!r}"
    assert state.difficulty is None, \
        "BEHAVIOUR: no difficulty record is opened at the depth cap"


def test_declare_behind_a_withheld_record_is_refused_at_the_cap(store, fixtures_dir, monkeypatch):
    monkeypatch.setenv(PARENT_ENV, f"parent-session:aaaa00000001:{_cap() - 1}")
    _executing(store, "cap3", fixtures_dir)
    _fail(store, "cap3")
    d = _declare(store, "cap3")
    assert d.ok is False
    assert d.marker == ESCALATE
    assert store.load("cap3").difficulty is None


def test_child_below_the_cap_still_records_its_difficulty(store, fixtures_dir, monkeypatch):
    monkeypatch.setenv(PARENT_ENV, f"parent-session:aaaa00000001:{_cap() - 2}")
    _executing(store, "cap4", fixtures_dir)
    d = _fail(store, "cap4")
    assert d.marker == "OVERCOME-DIFFICULTY"
    assert store.load("cap4").difficulty.level == _cap() - 1


# --- across a spawn ----------------------------------------------------------

def test_child_difficulty_inherits_parent_level(store, fixtures_dir, monkeypatch):
    """negative control: a root difficulty of a spawned session is numbered from the
    spawning session's open difficulty (`child` origin, its uid as parent)."""
    monkeypatch.setenv(PARENT_ENV, "parent-session:abc123def456:1")
    _diagnosing(store, "ch1", fixtures_dir)
    d = store.load("ch1").difficulty
    assert getattr(d, "level", None) == 2, \
        f"BEHAVIOUR: a child's root difficulty is one level below its parent's; level={getattr(d, 'level', None)!r}"
    assert getattr(d, "origin", None) == "child", "BEHAVIOUR: a child's root has origin `child`"
    assert getattr(d, "parent_uid", None) == "abc123def456", \
        "BEHAVIOUR: a child's root names the spawning difficulty as its parent"
    inherited = getattr(store.load("ch1"), "parent_difficulty", None)
    assert inherited == {"session": "parent-session", "uid": "abc123def456", "level": 1}


@pytest.mark.parametrize("raw", ["garbage", "a:b", "s:uid:notanumber", ":uid:2", "s::2"])
def test_malformed_parent_difficulty_is_ignored(store, fixtures_dir, monkeypatch, capsys, raw):
    monkeypatch.setenv(PARENT_ENV, raw)
    _diagnosing(store, "ch2", fixtures_dir)
    state = store.load("ch2")
    assert state.difficulty.level == 0
    assert state.difficulty.origin == "stage"
    assert getattr(state, "parent_difficulty", None) is None
    assert PARENT_ENV in capsys.readouterr().err


class _Launched(Exception):
    def __init__(self, env):
        super().__init__("launched")
        self.env = env


def _spawned_env(monkeypatch, tmp_path, fixtures_dir, session, inherited=None):
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
            "--kind", "thinker", "--plan", str(fixtures_dir / "plan_two_stage.toml"),
            "--done-criterion", "a verdict", "--criterion-type", "measurable",
            "--complexity", "medium", "--effort", "medium",
            "--session", session, "--state-root", str(tmp_path / "state"),
            "--workdir", str(tmp_path),
        ])
    return info.value.env


def test_spawn_exports_parent_difficulty(store, fixtures_dir, tmp_path, monkeypatch):
    """negative control: a spawn for a session with an open difficulty tells the child
    engine which one (`<session>:<uid>:<level>`)."""
    _diagnosing(store, "sp1", fixtures_dir)
    open_record = store.load("sp1").difficulty
    uid = getattr(open_record, "uid", "")
    level = getattr(open_record, "level", 0)
    env = _spawned_env(monkeypatch, tmp_path, fixtures_dir, "sp1")
    exported = env.get(PARENT_ENV)
    assert exported is not None, \
        "BEHAVIOUR: a spawn for a session with an open difficulty exports it to the child"
    assert exported == f"sp1:{uid}:{level}", f"BEHAVIOUR: exported {exported!r}"


def test_spawn_without_an_open_difficulty_exports_nothing(store, fixtures_dir, tmp_path, monkeypatch):
    _executing(store, "sp2", fixtures_dir)
    env = _spawned_env(monkeypatch, tmp_path, fixtures_dir, "sp2",
                       inherited="grandparent:ffff00000000:0")
    assert PARENT_ENV not in env  # a stale ancestor value is not passed on either


def test_spawn_for_an_unknown_session_still_launches(store, fixtures_dir, tmp_path, monkeypatch):
    env = _spawned_env(monkeypatch, tmp_path, fixtures_dir, "no-such-session")
    assert PARENT_ENV not in env


def test_exported_value_round_trips_into_the_child_engine(store, fixtures_dir, tmp_path, monkeypatch):
    _diagnosing(store, "sp3", fixtures_dir)
    env = _spawned_env(monkeypatch, tmp_path, fixtures_dir, "sp3")
    parent = store.load("sp3").difficulty
    monkeypatch.setenv(PARENT_ENV, env[PARENT_ENV])
    _diagnosing(store, "sp3-child", fixtures_dir)
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
