"""Pair-keyed plan-review recording (`plan-review --scope topo:<pair>`): the record,
its digest binding and currency (`gates.pair_status`), the condition-4 gap ledger,
the pair override checks and the same-pair prior-pass branching.

Fixtures are plan dicts serialized to a real TOML file the session points at, so
every record is bound against a fresh load of bytes on disk — the way the engine
binds it — and an edit is a rewrite of that file."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates
from agentctl.dispatch import RunResult
from agentctl.plan import (
    CONDITION_MARKERS,
    changed_parts,
    load_plan,
    pair_binding,
    review_pairs,
)
from agentctl.render import render_stage_brief, render_stage_interface
from agentctl.state import (
    GateRecord,
    PlanPairReview,
    PlanReview,
    SessionState,
    plan_review_pair_scope,
)
from agentctl.store import FileStateStore

SID = "pair-s"
C4 = CONDITION_MARKERS[3]


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    return "[" + ", ".join(_value(i) for i in v) + "]"


def _dump(table: dict, prefix: str = "") -> list[str]:
    lines: list[str] = []
    subs: list[tuple[str, object]] = []
    for key, v in table.items():
        if isinstance(v, dict) or (isinstance(v, list) and v and isinstance(v[0], dict)):
            subs.append((key, v))
        else:
            lines.append(f"{key} = {_value(v)}")
    for key, v in subs:
        name = prefix + key
        if isinstance(v, dict):
            lines += ["", f"[{name}]", *_dump(v, name + ".")]
        else:
            for item in v:
                lines += ["", f"[[{name}]]", *_dump(item, name + ".")]
    return lines


def _write(path: Path, data: dict) -> Path:
    path.write_text("\n".join(_dump(data)) + "\n", encoding="utf-8")
    return path


def _stage(index, **overrides):
    stage = {
        "index": index, "title": f"Stage {index}", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "dc",
        "means": "Edit", "method": f"method-{index}", "verify_command": f"true-{index}",
    }
    stage.update(overrides)
    return stage


def _order(**extra):
    return {
        "requirements": [{"id": "R1", "text": "R1"}],
        "coverage": {"R1": ["stage 3 verify_command"]},
        **extra,
    }


def _data(**order_extra) -> dict:
    """tb16's shape: stage 3 relies on stage 1 (supplies) and on stage 2 (raw
    depends_on only); stage 3 is the plan's sink. Pairs: base-plan, plan-3, 3-1, 3-2."""
    return {
        "meta": {"task_id": "t", "goal": "G", "done_criterion": "DC", "order": _order(**order_extra)},
        "final_check": [{"command": "true", "expected_exit": 0, "label": "fc1"}],
        "stage": [
            _stage(1),
            _stage(2),
            _stage(3, depends_on=[1, 2], supplies=[{"on": 1}]),
        ],
    }


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _exit(code: int):
    return lambda argv: RunResult(returncode=code)


class Env:
    def __init__(self, tmp_path: Path, store: FileStateStore, data: dict):
        self.tmp_path = tmp_path
        self.store = store
        self.plan = _write(tmp_path / "plan.toml", data)
        self.ledger = tmp_path / "ledger.jsonl"
        self.ledger.unlink(missing_ok=True)
        self.store.save(SessionState(
            session_id=SID, task_id="t", weight_class="SUBSTANTIVE", plan_verified=True,
            plan_path=str(self.plan),
            approval=GateRecord("plan_approval", armed=True, passed=True),
        ))

    def state(self) -> SessionState:
        return self.store.load(SID)

    def doc(self, path: Path | None = None):
        return load_plan(path or self.plan)

    def status(self, pair: str, path: Path | None = None) -> str:
        return gates.pair_status(self.state(), self.doc(path), str(path or self.plan), pair)

    def edit(self, mutate) -> None:
        data = copy.deepcopy(self._data)
        mutate(data)
        self._data = data
        _write(self.plan, data)

    def mutate_state(self, fn) -> None:
        state = self.state()
        fn(state)
        self.store.save(state)

    def record(self, scope, verdict, *, reviewer="thinker", concerns=None, note="",
               target: Path | None = None, digest="auto", regression_command=None, runner=None):
        if digest == "auto":
            digest = _sha(target or self.plan)
        return cli.cmd_plan_review(
            Namespace(session=SID, target=str(target) if target else None, scope=scope,
                      verdict=verdict, reviewer=reviewer, concerns=concerns, note=note,
                      plan_digest=digest, regression_command=regression_command),
            store=self.store, runner=runner,
        )

    def record_all(self) -> None:
        for pair in review_pairs(self.doc()):
            assert self.record(f"topo:{pair}", "pass").ok, pair

    def statuses(self) -> dict[str, str]:
        return {pair: self.status(pair) for pair in review_pairs(self.doc())}

    def ledger_lines(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def make_env(tmp_path, store, monkeypatch):
    monkeypatch.setenv("AGENTCTL_ESCALATION_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")

    def make(data: dict | None = None) -> Env:
        data = data if data is not None else _data()
        env = Env(tmp_path, store, data)
        env._data = copy.deepcopy(data)
        return env

    return make


@pytest.fixture
def env(make_env) -> Env:
    return make_env()


def _review_dicts(state: SessionState) -> tuple:
    return (
        dataclasses.asdict(state.plan_review) if state.plan_review else None,
        {k: dataclasses.asdict(v) for k, v in state.plan_review_passes.items()},
        {k: dataclasses.asdict(v) for k, v in state.plan_stage_reviews.items()},
        state.plan_review_rounds,
    )


def _seed_whole_plan_pass(env: Env, **overrides) -> None:
    sha = _sha(env.plan)

    def seed(state):
        review = PlanReview(plan_path=str(env.plan), verdict="pass", reviewer="thinker",
                            plan_sha256=sha, **overrides)
        state.plan_review = review
        state.plan_review_passes[""] = review
        state.plan_stage_reviews["stage:1"] = dataclasses.replace(review, scope="stage:1")

    env.mutate_state(seed)


def test_tr1_pair_record_is_keyed_by_pair_id_with_the_seven_digests(env):
    for pair, base, service in (("3-1", 3, 1), ("base-plan", "base", "plan"), ("plan-3", "plan", 3)):
        d = env.record(f"topo:{pair}", "pass")
        assert d.ok, d.detail
        record = env.state().plan_pair_reviews[pair]
        assert isinstance(record, PlanPairReview)
        assert (record.base, record.service) == (base, service)
        assert record.binding() == pair_binding(env.doc(), pair)
        assert len(record.binding()) == 7 and all(record.binding().values())
        assert env.status(pair) == "current"
    state = env.state()
    assert set(state.plan_pair_reviews) == {"3-1", "base-plan", "plan-3"}
    assert state.plan_review is None
    assert state.plan_review_passes == {} and state.plan_stage_reviews == {}
    assert state.plan_review_rounds == 0


def test_tr2_pass_needs_a_digest_and_records_without_prerequisite_pairs(env):
    d = env.record("topo:3-1", "pass", digest=None)
    assert not d.ok
    assert "--plan-digest" in d.detail
    assert env.state().plan_pair_reviews == {}

    d = env.record("topo:3-2", "pass")
    assert d.ok, d.detail
    assert env.status("3-2") == "current"


def test_tr3_condition4_ledger_lines_and_whole_plan_records_untouched(env):
    concern = f"{C4} stage 3 relies on a product stage 1 never delivers"
    d = env.record("topo:3-1", "revise", concerns=[concern, f"{CONDITION_MARKERS[0]} wording only"])
    assert d.ok, d.detail
    (line,) = env.ledger_lines()
    assert line["unit"] == "3-1"
    assert line["dependency_stage"] == 1
    assert line["condition"] == C4.rstrip(":")
    assert line["concern"] == concern
    assert line["concern_sha256"] == _text_sha(concern)
    assert line["outcome"] == "confirmed-gap"
    assert (line["session"], line["plan_path"], line["task_id"]) == (SID, str(env.plan), "t")
    assert env.status("3-1") == "revise"

    d = env.record("topo:3-1", "override", reviewer="fedor", note="accepted as is")
    assert d.ok, d.detail
    confirmed, netted = env.ledger_lines()
    assert confirmed == line
    assert netted["outcome"] == "false-alarm"
    assert (netted["unit"], netted["concern_sha256"]) == ("3-1", line["concern_sha256"])
    assert netted["concern"] == concern

    lines_before = env.ledger_lines()
    assert env.record("topo:3-2", "revise", concerns=[f"{CONDITION_MARKERS[0]} wording only"]).ok
    assert env.record("topo:plan-3", "pass", concerns=[f"{C4} stray marker on a pass"]).ok
    assert env.record("topo:base-plan", "revise").ok
    assert env.ledger_lines() == lines_before

    _seed_whole_plan_pass(env)
    before = _review_dicts(env.state())
    gap = f"{C4} gap behind a whole-plan pass"
    d = env.record("topo:3-1", "revise", concerns=[gap])
    assert d.ok, d.detail
    assert [row["outcome"] for row in env.ledger_lines()[len(lines_before):]] == ["confirmed-gap"]
    state = env.state()
    assert state.plan_pair_reviews["3-1"].verdict == "revise"
    assert _review_dicts(state) == before


def test_tr6_staleness_by_edge_kind(make_env):
    env = make_env()
    env.record_all()
    env.edit(lambda d: d["stage"][0].update(method="edited"))
    assert env.status("3-1") == "stale:service"
    assert env.status("3-2") == "current"

    env.record_all()
    env.edit(lambda d: d["stage"][2].update(method="edited"))
    assert env.status("3-1") == "stale:base"
    assert env.status("3-2") == "stale:base"

    env.record_all()
    env.edit(lambda d: d["stage"][1].update(method="edited"))
    assert env.status("3-1") == "current"
    assert env.status("3-2") == "stale:service"

    env = make_env()
    env.record_all()
    env.edit(lambda d: d["meta"]["order"]["requirements"][0].update(text="changed"))
    assert env.statuses() == {pair: "stale:context" for pair in review_pairs(env.doc())}

    env = make_env()
    env.record_all()
    env.edit(lambda d: d["final_check"][0].update(command="false"))
    statuses = env.statuses()
    assert statuses["base-plan"].startswith("stale:")
    assert statuses["plan-3"].startswith("stale:")
    assert statuses["3-1"] == statuses["3-2"] == "current"

    for mutate in (lambda d: d["meta"].update(goal="another goal"),
                   lambda d: d["final_check"][0].update(label="fc-renamed")):
        env = make_env()
        env.record_all()
        env.edit(mutate)
        statuses = env.statuses()
        assert statuses["3-1"] == statuses["3-2"] == "stale:base_file"
        assert statuses["plan-3"] == "stale:base"

    env = make_env()
    env.record_all()
    env.edit(lambda d: d["stage"][1].update(output_artifacts=["new-artifact.py"]))
    assert env.status("base-plan") == "stale:service_interface"

    def effects(resolver):
        return [{"path": "scripts/x.sh", "sha256": "a" * 64, "resolver": resolver}]

    data = _tb8_data()
    data["stage"][2]["effects"] = effects("r1")
    env = make_env(data)
    env.record_all()
    env.edit(lambda d: d["stage"][2].update(effects=effects("r2")))
    assert env.status("4-3") == "stale:service_file"
    assert env.status("4-2") == "current"

    env = make_env()
    env.record_all()
    before = pair_binding(env.doc(), "3-1")
    env.edit(lambda d: d["stage"][2].update(depends_on=[1]))
    after = pair_binding(env.doc(), "3-1")
    assert {k for k in before if before[k] != after[k]} == {"edge_digest"}
    assert env.status("3-1") == "stale:edge"

    data = _data()
    data["stage"][0].update(depends_on=[2], supplies=[{"on": 2}])
    env = make_env(data)
    env.record_all()
    before = pair_binding(env.doc(), "3-2")
    env.edit(lambda d: d["stage"][0].update(supplies=[{"on": 2, "element": "e2"}], title="Stage 1"))
    assert pair_binding(env.doc(), "3-2") == before

    env.edit(lambda d: d["stage"][0].update(depends_on=[], supplies=[]))
    after = pair_binding(env.doc(), "3-2")
    assert {k for k in before if before[k] != after[k]} == {"edge_digest"}
    assert env.status("3-2") == "stale:edge"


def test_tr10_state_files_load_across_schema_variants_and_effects_move_the_service_file(make_env):
    env = make_env()
    doc = env.doc()
    record = PlanPairReview(pair="3-1", base=3, service=1, verdict="pass", reviewer="thinker",
                            plan_path=str(env.plan), **pair_binding(doc, "3-1"))
    state = SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                         stages=list(doc.stages), plan_pair_reviews={"3-1": record})
    base = json.loads(state.to_json())
    base["stages"][0]["effects"] = [{"path": "scripts/x.sh", "sha256": "a" * 64, "resolver": "r"}]

    without_pairs = {k: v for k, v in base.items() if k != "plan_pair_reviews"}
    assert SessionState.from_json(json.dumps(without_pairs)).plan_pair_reviews == {}

    legacy = {**without_pairs, "plan_topo_reviews": {"3": {"unit": "3", "verdict": "pass"}}}
    loaded = SessionState.from_json(json.dumps(legacy))
    assert loaded.plan_pair_reviews == {}
    assert not hasattr(loaded, "plan_topo_reviews")

    only_pairs = copy.deepcopy(base)
    for stage in only_pairs["stages"]:
        stage.pop("effects", None)
    assert SessionState.from_json(json.dumps(only_pairs)).plan_pair_reviews["3-1"] == record

    only_effects = {k: v for k, v in base.items() if k != "plan_pair_reviews"}
    assert SessionState.from_json(json.dumps(only_effects)).stages[0].effects[0].resolver == "r"

    both = SessionState.from_json(json.dumps(base))
    assert both.plan_pair_reviews["3-1"] == record
    assert both.stages[0].effects[0].resolver == "r"

    def effects(resolver):
        return [{"path": "scripts/x.sh", "sha256": "a" * 64, "resolver": resolver}]

    data = _tb8_data()
    data["stage"][2]["effects"] = effects("r1")
    env = make_env(data)
    before = pair_binding(env.doc(), "4-3")
    env.edit(lambda d: d["stage"][2].update(effects=effects("r2")))
    after = pair_binding(env.doc(), "4-3")
    assert {k for k in before if before[k] != after[k]} == {"service_file_digest"}


def _tb8_data() -> dict:
    return {
        "meta": {"task_id": "t"},
        "stage": [
            _stage(1),
            _stage(2),
            _stage(3, depends_on=[1]),
            _stage(4, depends_on=[3], supplies=[{"on": 2}]),
        ],
    }


def test_tr11_non_direct_edges_pair_ids_and_staleness(make_env):
    env = make_env(_tb8_data())
    pairs = review_pairs(env.doc())
    assert "4-2" in pairs and "4-3" in pairs and "4-1" not in pairs

    d = env.record("topo:4-1", "pass")
    assert not d.ok
    assert "4-1" in d.detail
    assert env.state().plan_pair_reviews == {}

    env.record_all()
    env.edit(lambda d: d["stage"][0].update(method="edited"))
    assert env.status("4-2") == env.status("4-3") == "current"

    env.edit(lambda d: d["stage"][1].update(method="edited"))
    assert env.status("4-2") == "stale:service"
    assert env.status("4-3") == "current"


def test_tr15_freshness_and_target_copy_recording(env):
    spawn_time_digest = _sha(env.plan)
    env.edit(lambda d: d["stage"][0].update(method="edited after the node files were cut"))
    d = env.record("topo:3-1", "pass", digest=spawn_time_digest)
    assert not d.ok
    assert "fresh load" in d.detail
    assert env.state().plan_pair_reviews == {}

    copy_path = env.tmp_path / "copy.toml"
    copy_path.write_bytes(env.plan.read_bytes())
    env.edit(lambda d: d["stage"][0].update(method="session plan moved on"))
    d = env.record("topo:3-1", "pass", target=copy_path)
    assert d.ok, d.detail
    record = env.state().plan_pair_reviews["3-1"]
    assert record.plan_path == str(copy_path)
    assert env.status("3-1", copy_path) == "current"
    assert env.status("3-1") == "missing"


def test_tr16_prior_pass_branching_overturn_and_scope_parsing(make_env):
    env = make_env()
    _seed_whole_plan_pass(env, reviewed_meta_digest="superseded", reviewed_stage_keys={"1": "old"})
    d = env.record("topo:3-1", "revise", concerns=["needs work"])
    assert d.ok, d.detail
    assert env.state().plan_pair_reviews["3-1"].verdict == "revise"

    env = make_env()
    assert env.record("topo:3-1", "pass").ok

    d = env.record("topo:3-1", "revise", concerns=["regressed"])
    assert not d.ok
    assert "--regression-command" in d.detail
    assert env.status("3-1") == "current"

    d = env.record("topo:3-1", "revise", concerns=["regressed"],
                   regression_command="check", runner=_exit(0))
    assert not d.ok
    assert env.status("3-1") == "current"

    d = env.record("topo:3-1", "revise", concerns=["regressed"],
                   regression_command="check", runner=_exit(1))
    assert d.ok, d.detail
    assert env.status("3-1") == "revise"

    env = make_env()
    assert env.record("topo:3-1", "pass").ok
    assert env.record("topo:3-1", "override", reviewer="fedor", note="accepted").ok
    assert env.status("3-1") == "override"
    assert env.record("topo:3-1", "revise", concerns=["reopened"]).ok

    env = make_env()
    for scope in ("", "stage:1"):
        assert env.record(scope, "pass").ok
        d = env.record(scope, "revise", concerns=["late objection"])
        assert d.data["plan_review_post_pass_unevidenced"] is True
        assert env.state().plan_review_passes[scope].verdict == "pass"

    env = make_env()
    for scope in ("topo:9-1", "topo:3", "topo:order", "topo:foo", "topo:", "foo"):
        assert not env.record(scope, "pass").ok, scope
    assert env.state().plan_pair_reviews == {}
    for pair in ("3-1", "plan-3", "base-plan"):
        assert plan_review_pair_scope(f"topo:{pair}") == pair
        assert env.record(f"topo:{pair}", "pass").ok
        assert pair in env.state().plan_pair_reviews


def _interface_empty_data() -> dict:
    return {
        "meta": {"task_id": "t"},
        "stage": [
            _stage(1, depends_on=[2, 3], supplies=[{"on": 2, "element": "e2"}, {"on": 3, "element": "e3"}]),
            _stage(2, depends_on=[4], expected_result_image=" "),
            _stage(3, depends_on=[4], output_artifacts=[]),
            _stage(4),
        ],
    }


@pytest.mark.parametrize("pair, service, expected_render", [
    ("1-2", 2, lambda doc: render_stage_interface(doc, 2, contract=True)),
    ("2-4", 4, lambda doc: render_stage_brief(doc, 4)),
], ids=["interface_empty_service", "source_service"])
def test_tr18_interface_empty_or_source_service_binds_the_full_brief(make_env, pair, service, expected_render):
    env = make_env(_interface_empty_data())
    doc = env.doc()
    assert pair_binding(doc, pair)["service_interface_digest"] == _text_sha(expected_render(doc))
    env.record_all()
    before = pair_binding(doc, pair)
    env.edit(lambda d: d["stage"][service - 1].update(method="edited"))
    after = pair_binding(env.doc(), pair)
    assert after["service_interface_digest"] != before["service_interface_digest"]
    assert env.status(pair) == "stale:service"


def test_tr21_pair_override_checks_and_storage(make_env):
    env = make_env(_data(customer_id="fedor"))
    concern = f"{C4} stage 3 relies on a product stage 1 never delivers"
    assert env.record("topo:3-1", "revise", reviewer="thinker", concerns=[concern]).ok
    _seed_whole_plan_pass(env)
    before = _review_dicts(env.state())

    d = env.record("topo:3-1", "override", reviewer="thinker", note="self-override")
    assert not d.ok and "distinct reviewer" in d.detail

    d = env.record("topo:3-1", "override", reviewer="mallory", note="not the customer")
    assert not d.ok and "customer_id" in d.detail

    d = env.record("topo:3-1", "override", reviewer="fedor", note="")
    assert not d.ok and "--note" in d.detail
    assert env.state().plan_pair_reviews["3-1"].verdict == "revise"

    d = env.record("topo:3-1", "override", reviewer="fedor", note="user accepts the risk")
    assert d.ok, d.detail
    state = env.state()
    assert state.plan_pair_reviews["3-1"].verdict == "override"
    assert state.plan_pair_reviews["3-1"].reviewer == "fedor"
    assert state.plan_pair_reviews["3-1"].note == "user accepts the risk"
    assert _review_dicts(state) == before
    assert env.status("3-1") == "override"
    confirmed, netted = env.ledger_lines()
    assert (confirmed["outcome"], netted["outcome"]) == ("confirmed-gap", "false-alarm")
    assert netted["concern_sha256"] == confirmed["concern_sha256"] == _text_sha(concern)

    # No customer_id and no active release: the coordinator cannot self-override a
    # pair, exactly as it cannot self-override the whole plan; once it records one
    # under a release, the user surface names it.
    env = make_env(_data())
    assert env.record("topo:3-1", "revise", reviewer="thinker", concerns=[concern]).ok
    d = env.record("topo:3-1", "override", reviewer="agent", note="self-waiver")
    assert not d.ok and "agent-authored override is refused" in d.detail
    whole = cli.cmd_plan_review(
        Namespace(session=SID, target=None, scope=None, verdict="override", reviewer="agent",
                  concerns=None, note="self-waiver", plan_digest=None, regression_command=None),
        store=env.store)
    assert not whole.ok and whole.detail == d.detail
    assert env.state().plan_pair_reviews["3-1"].verdict == "revise"
    state = env.state()
    state.plan_pair_reviews["3-1"].verdict = "override"
    state.plan_pair_reviews["3-1"].reviewer = "agent"
    assert cli._agent_review_override(state) == {
        "reviewer": "agent", "scope": "topo:3-1", "note": state.plan_pair_reviews["3-1"].note,
        "plan_path": str(env.plan)}

    # Flags a pair record has no field for are refused, never silently dropped.
    for flag, dest, value in (("--concern-id", "concern_ids", ["k1"]),
                              ("--findings-blocking", "findings_blocking", 1),
                              ("--findings-nonblocking", "findings_nonblocking", 0)):
        d = cli.cmd_plan_review(
            Namespace(session=SID, target=None, scope="topo:3-1", verdict="revise",
                      reviewer="thinker", concerns=[concern], note="", plan_digest=None,
                      regression_command=None, **{dest: value}),
            store=env.store)
        assert not d.ok and flag in d.detail


def _walk(env: Env, target: Path | None = None, fmt: str = "json"):
    return cli.cmd_plan_review_walk(
        Namespace(session=SID, target=str(target) if target else None, format=fmt), store=env.store)


def _compose(env: Env, target: Path | None = None):
    return cli.cmd_plan_review_compose(
        Namespace(session=SID, target=str(target) if target else None), store=env.store)


def _delta(env: Env):
    return cli.cmd_plan_review_delta(Namespace(session=SID, plan=None), store=env.store)


def _blockers(env: Env, path: Path | None = None) -> list[str]:
    return gates.plan_review_blockers(env.state(), str(path or env.plan))


def _rows(env: Env, target: Path | None = None) -> dict[str, dict]:
    d = _walk(env, target)
    assert d.ok, d.detail
    return {row["pair"]: row for level in d.data["levels"] for row in level}


def _w(env: Env) -> list[str]:
    state = env.state()
    return gates.walk_stale_pairs(state, env.doc(), str(env.plan), state.plan_review)


def _effects(resolver: str) -> list[dict]:
    return [{"path": "scripts/x.sh", "sha256": "a" * 64, "resolver": resolver}]


def _diamond_data() -> dict:
    """s=1; a=2 and b=3 rely on s; c=4 relies on a and b and, raw-only, on s. The plan's
    only reliance stage is c. Pairs: base-plan, plan-4, 4-1, 4-2, 4-3, 2-1, 3-1."""
    return {
        "meta": {"task_id": "t", "goal": "G", "done_criterion": "DC",
                 "order": _order(coverage={"R1": ["stage 4 verify_command"]})},
        "final_check": [{"command": "true", "expected_exit": 0, "label": "fc1"}],
        "stage": [
            _stage(1),
            _stage(2, depends_on=[1], supplies=[{"on": 1}]),
            _stage(3, depends_on=[1], supplies=[{"on": 1}]),
            _stage(4, depends_on=[1, 2, 3], supplies=[{"on": 2}, {"on": 3}]),
        ],
    }


def _bounded_data() -> dict:
    """n=1, a=2, b=3, c=4: a relies on n, b on a (not on n), c on nothing and nothing
    relies on c. Pairs: base-plan, plan-3, plan-4, 2-1, 3-2."""
    return {
        "meta": {"task_id": "t", "goal": "G", "done_criterion": "DC", "order": _order()},
        "final_check": [{"command": "true", "expected_exit": 0, "label": "fc1"}],
        "stage": [
            _stage(1),
            _stage(2, depends_on=[1], supplies=[{"on": 1}]),
            _stage(3, depends_on=[2], supplies=[{"on": 2}]),
            _stage(4),
        ],
    }


def _edit_n_interface(d):
    d["stage"][0].update(expected_result_image="img edited")


def _edit_n_method(d):
    d["stage"][0].update(method="method-1 edited")


def _edit_b_drops_a(d):
    d["stage"][2].update(depends_on=[], supplies=[])


def _hand_baseline(env: Env, *, record_seq: int = 1, **overrides) -> PlanReview:
    """A whole-plan pass bound to the plan as it stands, with the pair bindings both
    real writers record, seeded directly into the state."""
    doc = env.doc()
    review = PlanReview(
        plan_path=str(env.plan), verdict="pass", reviewer="thinker", plan_sha256=_sha(env.plan),
        reviewed_meta_digest=cli.plan_meta_digest(doc),
        reviewed_stage_keys={str(k): v for k, v in cli.plan_stage_digests(doc).items()},
        reviewed_pair_bindings={pid: gates.pair_binding_hash(doc, pid) for pid in review_pairs(doc)},
        record_seq=record_seq, **overrides,
    )

    def seed(state):
        state.plan_review = review
        state.plan_review_passes[""] = review

    env.mutate_state(seed)
    return review


def test_tr4_compose_over_all_current_pair_passes_writes_an_ordinary_whole_plan_pass(env, monkeypatch):
    env.record_all()
    monkeypatch.setattr(cli, "_record_first_thinker_verdict",
                        lambda *a, **k: pytest.fail("compose must not record a first-thinker verdict"))
    d = _compose(env)
    assert d.ok, d.detail
    state = env.state()
    review = state.plan_review
    assert review.reviewer == cli.TOPOLOGICAL_COMPOSITION_REVIEWER == "topological-composition"
    assert review.verdict == "pass" and review.scope == ""
    assert review.plan_path == str(env.plan)
    assert review.plan_sha256 == _sha(env.plan)
    assert state.plan_review_passes[""] == review
    assert set(review.reviewed_pair_bindings) == set(review_pairs(env.doc()))
    assert _blockers(env) == []


def test_tr5_compose_refuses_and_names_the_failing_pairs(make_env):
    env = make_env()
    for pair in ("base-plan", "plan-3", "3-1"):
        assert env.record(f"topo:{pair}", "pass").ok
    d = _compose(env)
    assert not d.ok
    assert d.data["failing"] == {"3-2": "missing"}
    assert "3-2 (missing)" in d.detail
    assert env.state().plan_review is None

    # moved stage key names every incident pair
    env = make_env()
    env.record_all()
    env.edit(lambda d: d["stage"][2].update(method="edited"))
    d = _compose(env)
    assert not d.ok
    assert set(d.data["failing"]) == {"plan-3", "3-1", "3-2"}
    assert all(pair in d.detail for pair in d.data["failing"])
    assert env.state().plan_review is None

    # moved service-file digest
    data = _tb8_data()
    data["stage"][2]["effects"] = _effects("r1")
    env = make_env(data)
    env.record_all()
    env.edit(lambda d: d["stage"][2].update(effects=_effects("r2")))
    d = _compose(env)
    assert not d.ok
    assert d.data["failing"] == {"4-3": "stale:service_file", "3-1": "stale:base_file"}

    # moved service-interface digest
    env = make_env()
    env.record_all()
    env.edit(lambda d: d["stage"][1].update(output_artifacts=["new-artifact.py"]))
    d = _compose(env)
    assert not d.ok
    assert d.data["failing"]["base-plan"] == "stale:service_interface"
    assert all(s.startswith("stale:") for s in d.data["failing"].values())

    # moved meta digest
    env = make_env()
    env.record_all()
    env.edit(lambda d: d["meta"].update(goal="another goal"))
    d = _compose(env)
    assert not d.ok
    assert set(d.data["failing"]) == set(review_pairs(env.doc()))

    # revise record
    env = make_env()
    for pair in review_pairs(env.doc()):
        verdict = "revise" if pair == "3-1" else "pass"
        assert env.record(f"topo:{pair}", verdict, concerns=["gap"] if verdict == "revise" else None).ok
    d = _compose(env)
    assert not d.ok
    assert d.data["failing"] == {"3-1": "revise"}

    # plan-node edits fail base-plan and the plan-s pairs, no stage-stage pair outright
    plan_node_edits = {
        "final_check": lambda d: d["final_check"][0].update(command="false"),
        "external_research": lambda d: d["meta"].update(external_research=["https://example.invalid/doc"]),
        "task_id": lambda d: d["meta"].update(task_id="renamed"),
        "delivery_worktree": lambda d: d["meta"].update(delivery_worktree="/tmp/other-worktree"),
    }
    for name, mutate in plan_node_edits.items():
        env = make_env()
        env.record_all()
        env.edit(mutate)
        d = _compose(env)
        assert not d.ok, name
        failing = d.data["failing"]
        assert {"base-plan", "plan-3"} <= set(failing), name
        stage_stage = {pid: status for pid, status in failing.items() if pid not in ("base-plan", "plan-3")}
        assert set(stage_stage.values()) <= {"stale:base_file"}, name
        assert env.state().plan_review is None, name
        if name == "final_check":
            assert set(failing) == {"base-plan", "plan-3"}


def test_tr7_walk_orders_base_before_service_with_advisory_readiness(make_env):
    env = make_env(_diamond_data())
    doc = env.doc()
    assert set(review_pairs(doc)) == {"base-plan", "plan-4", "4-1", "4-2", "4-3", "2-1", "3-1"}
    d = _walk(env)
    assert d.ok, d.detail
    levels = [[row["pair"] for row in level] for level in d.data["levels"]]
    assert levels[0] == ["base-plan"]
    assert levels[1] == ["plan-4"]
    assert sorted(levels[2]) == ["4-1", "4-2", "4-3"]
    assert sorted(levels[3]) == ["2-1", "3-1"]
    rows = _rows(env)
    depths = gates.pair_depths(doc)
    from agentctl.plan import split_pair_id

    for pid, row in rows.items():
        assert row["level"] == depths[split_pair_id(pid)[0]]
        for prereq in gates.pair_prereqs(doc, pid):
            assert rows[prereq]["level"] < row["level"], (pid, prereq)
    assert rows["2-1"]["level"] > rows["4-2"]["level"]

    # ready progression (advisory) and the waiting prerequisites
    env = make_env(_diamond_data())

    def ready() -> set[str]:
        return {pid for pid, row in _rows(env).items()
                if row["ready"] and row["status"] not in gates.PAIR_SATISFIED}

    assert ready() == {"base-plan"}
    assert env.record("topo:base-plan", "pass").ok
    assert ready() == {"plan-4"}
    assert env.record("topo:plan-4", "pass").ok
    assert ready() == {"4-1", "4-2", "4-3"}
    rows = _rows(env)
    assert sorted(rows["2-1"]["waiting"]) == ["4-1", "4-2"]
    assert sorted(rows["3-1"]["waiting"]) == ["4-1", "4-3"]
    assert not rows["2-1"]["ready"] and not rows["3-1"]["ready"]

    d = env.record("topo:2-1", "pass")
    assert d.ok, d.detail
    assert _rows(env)["2-1"]["status"] == "current"

    # stale and missing marks, the documented JSON keys
    env = make_env(_diamond_data())
    for pair in review_pairs(env.doc()):
        if pair != "3-1":
            assert env.record(f"topo:{pair}", "pass").ok
    env.edit(lambda d: d["stage"][1].update(method="edited"))
    before = env.store.path(SID).read_bytes()
    d = _walk(env)
    assert env.store.path(SID).read_bytes() == before
    assert set(d.data) == {"plan_path", "discharges", "levels"}
    assert d.data["plan_path"] == str(env.plan)
    assert json.loads(d.detail) == d.data
    rows = _rows(env)
    assert rows["3-1"]["status"] == "missing"
    assert rows["4-2"]["status"] == "stale:service"
    assert rows["2-1"]["status"] == "stale:base"
    assert rows["base-plan"]["status"] == "current"
    for row in rows.values():
        assert set(row) == {"pair", "base", "service", "level", "status", "ready", "waiting",
                            "spawn", "record"}
    assert rows["base-plan"]["spawn"] is None and rows["base-plan"]["record"] is None
    assert "--review-topo 3-1" in rows["3-1"]["spawn"]
    assert str(env.plan) in rows["3-1"]["spawn"]
    assert "--scope topo:3-1" in rows["3-1"]["record"]
    assert f"--target {env.plan}" in rows["3-1"]["record"]

    # text format lists every pair by level
    env = make_env()
    d = _walk(env, fmt="text")
    assert d.ok
    assert "level 0:" in d.detail
    assert all(f"  {pair}: missing" in d.detail for pair in review_pairs(env.doc()))


def test_tr8_round_release_and_delta_messages_name_the_topological_route(env):
    message = gates._PLAN_REVIEW_ROUND_RELEASE_MESSAGE
    assert "plan-review-topological.py" in message
    assert "plan-review-compose" in message
    d = _delta(env)
    assert d.data["whole_plan"] is True
    assert "plan-review-topological.py" in d.detail
    assert "plan-review-compose" in d.detail


def test_tr12_raw_only_depends_on_edge_is_a_pair_and_comes_from_the_plan_file_not_state_stages(env):
    doc = env.doc()
    assert "3-2" in review_pairs(doc)
    stage_3 = next(s for s in doc.stages if s.index == 3)
    assert stage_3.depends_on == [1]
    env.mutate_state(lambda state: setattr(state, "stages", list(doc.stages)))
    env.record_all()
    env.edit(lambda d: d["stage"][1].update(method="edited"))
    assert env.status("3-2") == "stale:service"
    d = _compose(env)
    assert not d.ok
    assert d.data["failing"] == {"3-2": "stale:service"}
    assert "3-2" in d.detail


def test_tr17_target_computes_everything_from_the_target_path(make_env):
    env = make_env()
    copy_path = env.tmp_path / "copy.toml"
    copy_path.write_bytes(env.plan.read_bytes())
    env.record_all()
    assert all(row["status"] == "current" for row in _rows(env).values())
    before = env.store.path(SID).read_bytes()
    d = _walk(env, copy_path)
    assert env.store.path(SID).read_bytes() == before
    assert d.data["plan_path"] == str(copy_path)
    assert all(row["status"] == "missing" for level in d.data["levels"] for row in level)
    assert str(copy_path) in d.data["levels"][0][0]["spawn"]

    refused = _compose(env, copy_path)
    assert not refused.ok
    assert str(copy_path) in refused.detail
    assert set(refused.data["failing"]) == set(review_pairs(env.doc()))
    assert set(refused.data["failing"].values()) == {"missing"}

    for pair in review_pairs(env.doc(copy_path)):
        assert env.record(f"topo:{pair}", "pass", target=copy_path).ok
    assert all(row["status"] == "current" for row in _rows(env, copy_path).values())
    assert all(row["status"] == "missing" for row in _rows(env).values())
    d = _compose(env, copy_path)
    assert d.ok, d.detail
    assert env.state().plan_review.plan_path == str(copy_path)

    # digests come from the target, not from a session plan that moved on
    env = make_env()
    copy_path = env.tmp_path / "copy.toml"
    copy_path.write_bytes(env.plan.read_bytes())
    env.edit(lambda d: d["stage"][0].update(method="the session plan moved on"))
    for pair in review_pairs(env.doc(copy_path)):
        assert env.record(f"topo:{pair}", "pass", target=copy_path).ok
    d = _compose(env, copy_path)
    assert d.ok, d.detail
    review = env.state().plan_review
    assert review.plan_sha256 == _sha(copy_path) != _sha(env.plan)
    assert review.reviewed_meta_digest == cli.plan_meta_digest(env.doc(copy_path))


def _record_pairs(env: Env, *, skip: tuple[str, ...] = ()) -> None:
    for pair in review_pairs(env.doc()):
        if pair not in skip:
            assert env.record(f"topo:{pair}", "pass").ok, pair


def _bounded_env(make_env, *, skip: tuple[str, ...] = ("plan-4",)) -> Env:
    """The four-stage fixture with every pair but `skip` recorded current against the
    pre-edit plan and a whole-plan baseline carrying one binding hash per pair."""
    env = make_env(_bounded_data())
    _record_pairs(env, skip=skip)
    _hand_baseline(env)
    return env


def test_tr13_scoped_discharge_over_the_walk_stale_set(make_env):
    env = make_env()
    _record_pairs(env)
    assert _compose(env).ok
    env.edit(lambda d: d["stage"][2].update(method="edited"))
    state = env.state()
    assert state.plan_stage_reviews == {}
    assert _w(env) == ["plan-3", "3-1", "3-2"]
    blockers = _blockers(env)
    assert blockers and not any("content changed" in b for b in blockers)
    assert all(any(pair in b for b in blockers) for pair in ("plan-3", "3-1", "3-2"))

    assert env.record("topo:3-1", "pass").ok
    assert _blockers(env)
    assert env.record("topo:plan-3", "pass").ok
    blockers = _blockers(env)
    assert blockers and any("3-2" in b for b in blockers)
    assert not any("3-1" in b or "plan-3" in b for b in blockers if "review pair" in b)

    assert env.record("topo:3-2", "pass").ok
    assert _blockers(env) == []

    # never-recorded incident pair
    env = make_env()
    _record_pairs(env, skip=("3-2",))
    _hand_baseline(env)
    env.edit(lambda d: d["stage"][2].update(method="edited"))
    assert env.status("3-2") == "missing"
    assert "3-2" in _w(env)
    assert env.record("topo:plan-3", "pass").ok
    assert env.record("topo:3-1", "pass").ok
    blockers = _blockers(env)
    assert any("3-2 is missing" in b for b in blockers)
    assert env.record("topo:3-2", "pass").ok
    assert _blockers(env) == []

    # bounded W
    env = _bounded_env(make_env)
    env.edit(_edit_n_interface)
    assert env.status("3-2") == "stale:service_file"
    assert _w(env) == ["2-1", "3-2"]
    assert env.state().plan_stage_reviews == {}

    assert env.record("topo:2-1", "pass").ok
    blockers = _blockers(env)
    assert blockers and any("3-2" in b for b in blockers)

    assert env.record("topo:3-2", "pass").ok
    assert env.status("plan-4") == "missing"
    assert _blockers(env) == []

    # new pair outside the baseline
    env = _bounded_env(make_env, skip=())
    assert "plan-2" not in review_pairs(env.doc())
    env.edit(_edit_b_drops_a)
    assert "plan-2" in review_pairs(env.doc())
    assert "plan-2" in _w(env)
    assert env.record("topo:plan-3", "pass").ok
    blockers = _blockers(env)
    assert any("plan-2" in b for b in blockers)
    assert env.record("topo:plan-2", "pass").ok
    assert _blockers(env) == []


    # baseline recorded through each real writer covers exactly the review pairs
    for via in ("cli", "compose"):
        env = make_env(_bounded_data())
        _record_pairs(env)
        if via == "cli":
            assert env.record("", "pass").ok
        else:
            assert _compose(env).ok
        before = review_pairs(env.doc())
        recorded = env.state().plan_review.reviewed_pair_bindings
        assert set(recorded) == set(before) and len(recorded) == len(before), via
        env.edit(_edit_b_drops_a)
        assert "plan-2" in _w(env), via

    # legacy baseline without pair bindings makes W every pair
    env = make_env(_bounded_data())
    _record_pairs(env)
    assert _compose(env).ok
    path = env.store.path(SID)
    raw = json.loads(path.read_text(encoding="utf-8"))
    for record in (raw["plan_review"], raw["plan_review_passes"][""]):
        record.pop("reviewed_pair_bindings")
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert env.state().plan_review.reviewed_pair_bindings in (None, {})
    env.edit(_edit_b_drops_a)
    pairs = list(review_pairs(env.doc()))
    assert _w(env) == pairs
    assert _blockers(env)
    unrecorded = [p for p in pairs if env.status(p) != "current"]
    assert unrecorded and len(unrecorded) < len(pairs)
    for pair in unrecorded[:-1]:
        assert env.record(f"topo:{pair}", "pass").ok
        assert _blockers(env)
    assert env.record(f"topo:{unrecorded[-1]}", "pass").ok
    assert _blockers(env) == []

    # final_check-only edit blocks on the plan-node pairs
    env = make_env()
    _record_pairs(env)
    assert _compose(env).ok
    env.edit(lambda d: d["final_check"][0].update(command="false"))
    state = env.state()
    assert state.plan_stage_reviews == {}
    meta_moved, moved = changed_parts(env.doc(), gates._plan_review_baseline(state.plan_review))
    assert not meta_moved and not moved
    assert env.status("base-plan").startswith("stale:") and env.status("plan-3").startswith("stale:")
    assert env.status("3-1") == env.status("3-2") == "current"
    assert _w(env) == ["base-plan", "plan-3"]
    blockers = _blockers(env)
    assert all(any(pair in b for b in blockers) for pair in ("base-plan", "plan-3"))
    assert env.record("topo:base-plan", "pass").ok
    assert any("plan-3" in b for b in _blockers(env))
    assert env.record("topo:plan-3", "pass").ok
    assert _blockers(env) == []


def test_tr14_delta_names_exactly_the_walk_stale_set(make_env):
    env = _bounded_env(make_env)
    env.edit(_edit_n_interface)
    d = _delta(env)
    assert d.data["pairs"] == ["2-1", "3-2"]
    assert "plan-review-topological.py --pairs 2-1,3-2" in d.detail

    # method-only edit: b-a stays current
    env = _bounded_env(make_env)
    env.edit(_edit_n_method)
    assert env.status("3-2") == "current"
    d = _delta(env)
    assert d.data["pairs"] == ["2-1"]
    assert "plan-review-topological.py --pairs 2-1" in d.detail

    # new-pair fixture
    env = _bounded_env(make_env, skip=())
    env.edit(_edit_b_drops_a)
    assert "plan-2" in _delta(env).data["pairs"]

    # a moved meta stales the whole plan: no --pairs hint, however much of W is stale
    env = _bounded_env(make_env)
    env.edit(lambda d: d["meta"].update(goal="another goal"))
    d = _delta(env)
    assert d.data["whole_plan"] is True
    assert d.data["pairs"] == []
    assert "--pairs" not in d.detail


def test_tr19_walk_discharges_lists_only_stages_clear_through_the_pairs(env):
    _record_pairs(env)
    assert _compose(env).ok
    env.edit(lambda d: (d["stage"][0].update(method="edited"), d["stage"][1].update(method="edited")))
    assert _w(env) == ["3-1", "3-2"]
    assert _walk(env).data["discharges"] == []

    assert env.record("topo:3-1", "pass").ok
    assert _walk(env).data["discharges"] == []

    env.record("stage:1", "pass")
    assert len(env.state().plan_stage_reviews) == 1
    assert env.record("topo:3-2", "pass").ok
    assert _blockers(env) == []
    assert _walk(env).data["discharges"] == ["stage:2"]

    env.record("stage:2", "pass")
    assert len(env.state().plan_stage_reviews) == 2
    assert _walk(env).data["discharges"] == []


def test_tr20_replan_target_composes_a_pass_bound_to_the_replacement_plan(env):
    plan_b = env.tmp_path / "plan-b.toml"
    data_b = copy.deepcopy(env._data)
    data_b["stage"][0]["method"] = "replacement method"
    _write(plan_b, data_b)
    for pair in review_pairs(env.doc(plan_b)):
        assert env.record(f"topo:{pair}", "pass", target=plan_b).ok, pair
    d = _compose(env, plan_b)
    assert d.ok, d.detail
    review = env.state().plan_review
    assert review.plan_path == str(plan_b)
    assert review.plan_sha256 == _sha(plan_b)
    assert review.reviewed_meta_digest == cli.plan_meta_digest(env.doc(plan_b))
    assert _blockers(env, plan_b) == []
    assert _blockers(env, env.plan)


def _override_pair(env: Env, pair: str) -> None:
    assert env.record(f"topo:{pair}", "revise", concerns=["open question"]).ok
    assert env.record(f"topo:{pair}", "override", reviewer="fedor", note="accepted as is").ok


def test_tr22_override_counts_for_readiness_and_compose_names_it_in_the_note(make_env):
    env = make_env(_diamond_data())
    for pair in ("base-plan", "plan-4", "4-2", "4-3"):
        assert env.record(f"topo:{pair}", "pass").ok
    _override_pair(env, "4-1")
    rows = _rows(env)
    assert rows["4-1"]["status"] == "override"
    assert rows["4-1"]["spawn"] is None and rows["4-1"]["record"] is None
    assert rows["2-1"]["ready"] and rows["2-1"]["waiting"] == []
    assert rows["3-1"]["ready"] and rows["3-1"]["waiting"] == []

    assert env.record("topo:2-1", "pass").ok
    assert env.record("topo:3-1", "pass").ok
    d = _compose(env)
    assert d.ok, d.detail
    review = env.state().plan_review
    assert "4-1" in review.note
    assert d.data["overridden"] == ["4-1"]
    assert _blockers(env) == []

    env.edit(lambda d: d["stage"][0].update(method="edited"))
    refused = _compose(env)
    assert not refused.ok
    assert refused.data["failing"]["4-1"].startswith("stale:")
    assert "4-1" in refused.detail


def _stale_and_revise_round(env: Env) -> None:
    """P_OLD = {3-1 stale, plan-3 revise}: the earlier round's records, left behind by
    a later edit and a later revise."""
    assert env.record("topo:base-plan", "pass").ok
    assert env.record("topo:plan-3", "revise", concerns=["gap"]).ok
    assert env.record("topo:3-1", "pass").ok
    assert env.record("topo:3-2", "pass").ok
    env.edit(lambda d: d["stage"][0].update(method="edited in the earlier round"))
    assert env.status("3-1").startswith("stale:")
    assert env.status("plan-3") == "revise"


def test_tr23_fresh_whole_plan_pass_leaves_w_empty_though_old_pair_records_stay_stale(env):
    _stale_and_revise_round(env)
    assert env.record("", "pass").ok
    assert _w(env) == []
    assert _blockers(env) == []
    assert env.status("3-1").startswith("stale:") and env.status("plan-3") == "revise"


def test_tr24_fresh_whole_plan_override_without_a_digest_leaves_w_empty(env):
    _stale_and_revise_round(env)
    d = env.record("", "override", reviewer="thinker", note="round budget spent", digest=None)
    assert d.ok, d.detail
    assert env.state().plan_review.verdict == "override"
    assert _w(env) == []
    assert _blockers(env) == []


def test_tr25_missing_plan_node_pair_in_the_baseline_still_blocks_on_its_own_status(make_env):
    env = make_env(_bounded_data())
    _record_pairs(env, skip=("plan-4",))
    assert env.record("", "pass").ok
    assert "plan-4" in env.state().plan_review.reviewed_pair_bindings
    env.edit(lambda d: d["final_check"][0].update(command="false"))
    assert env.state().plan_stage_reviews == {}
    blockers = _blockers(env)
    assert blockers
    assert any("plan-4 is missing" in b for b in blockers)


def test_tr26_a_whole_plan_revise_baseline_blocks_even_when_w_is_empty(env):
    _record_pairs(env)
    d = env.record("", "revise", concerns=["the plan needs more work"])
    assert not d.ok
    assert env.state().plan_review.verdict == "revise"
    assert _w(env) == []
    assert _blockers(env)


def _unrelated_edit_after_a_fresh_baseline(env: Env, record_baseline) -> None:
    _stale_and_revise_round(env)
    record_baseline()
    assert _w(env) == []
    env.edit(lambda d: d["stage"][1].update(method="edited after the fresh baseline"))
    assert _w(env) == ["3-2"]
    blockers = _blockers(env)
    assert blockers and any("3-2" in b for b in blockers)
    assert not any("3-1" in b or "plan-3" in b for b in blockers)
    assert env.record("topo:3-2", "pass").ok
    assert _blockers(env) == []


def test_tr27_no_recurring_block_on_an_unrelated_edit_after_a_fresh_pass(env):
    _unrelated_edit_after_a_fresh_baseline(env, lambda: env.record("", "pass"))


def test_tr28_no_recurring_block_on_an_unrelated_edit_after_a_fresh_override(env):
    _unrelated_edit_after_a_fresh_baseline(
        env, lambda: env.record("", "override", reviewer="thinker", note="round budget spent", digest=None))


LEDGER_KEYS = ("ts", "session", "plan_path", "task_id", "unit", "dependency_stage",
               "condition", "concern", "concern_sha256", "outcome")


def _load_scan():
    import importlib.util
    import sys

    scripts = str(Path(__file__).resolve().parent.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("improvement_scan_tr9", Path(scripts) / "improvement-scan.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ledger_line(session="s1", plan="/p.toml", unit="3-1", outcome="confirmed-gap", concern="gap") -> dict:
    return dict(zip(LEDGER_KEYS, (
        "2026-10-01T00:00:00+00:00", session, plan, "t", unit, 1, "C4", concern,
        _text_sha(concern), outcome)))


def _threshold_config(tmp_path: Path, threshold: int) -> Path:
    p = tmp_path / f"config{threshold}.md"
    p.write_text(f"| Key | Value |\n|---|---|\n| `principle-promotion-threshold` | `{threshold}` |\n",
                 encoding="utf-8")
    return p


def test_tr9_condition4_gap_detector_counts_distinct_net_confirmed_pair_keys(make_env, tmp_path, monkeypatch):
    scan = _load_scan()
    ledger = tmp_path / "ledger.jsonl"

    def write(lines: list[dict]) -> None:
        ledger.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    def fires(lines: list[dict], threshold: int = 3) -> bool:
        write(lines)
        result = scan._detect_condition4_gap_recurrence({}, [], config_path=_threshold_config(tmp_path, threshold))
        assert result is None or result["detector"] == "condition4-gap-recurrence"
        return result is not None

    # (a) counting
    three = [_ledger_line(unit=u) for u in ("3-1", "3-2", "plan-3")]
    assert fires(three)
    assert not fires(three[:2])
    assert not fires(three + [_ledger_line(unit="3-1", outcome="false-alarm")])
    assert not fires([_ledger_line(unit=u) for u in ("3-1", "3-1", "3-1", "3-2", "3-2")])
    assert not fires([_ledger_line(concern=f"wording {i}") for i in range(3)])
    assert fires(three[:2], threshold=2)

    # (b) literal keys, against the real writer
    env = make_env()
    ledger.write_text("", encoding="utf-8")
    for pair in ("3-1", "3-2", "plan-3"):
        assert env.record(f"topo:{pair}", "revise", concerns=[f"{C4} gap at {pair}"]).ok
    real = env.ledger_lines()
    assert len(real) == 3
    for line in real:
        assert set(line) <= set(LEDGER_KEYS)
        assert set(LEDGER_KEYS) - {"dependency_stage"} <= set(line)
    result = scan._detect_condition4_gap_recurrence({}, [], config_path=_threshold_config(tmp_path, 3))
    assert result is not None and result["measured"]["confirmed_pairs"] == 3

    # (c) wiring through the real telemetry entry point
    assert scan._detect_condition4_gap_recurrence in scan.TELEMETRY_DETECTORS
    monkeypatch.setattr(scan.shell, "refresh_policy_ledger", lambda days, ledger_path=None: (True, ""))
    policy = tmp_path / "policy.jsonl"
    policy.write_text(json.dumps({"session_id": "x", "mtime": 1.0}) + "\n", encoding="utf-8")
    spawn = tmp_path / "spawn.jsonl"
    spawn.write_text("", encoding="utf-8")

    def detectors(n_keys: int, tag: str) -> list[str]:
        write([_ledger_line(unit=f"3-{i}") for i in range(n_keys)])
        evidence = tmp_path / f"evidence-{tag}.json"
        rc = scan.main(["telemetry", "--emit-evidence", str(evidence), "--ledger", str(policy),
                        "--spawn-ledger", str(spawn), "--cursor", str(tmp_path / f"cursor-{tag}.json")])
        assert rc == 0
        return [item["detector"] for item in json.loads(evidence.read_text(encoding="utf-8"))["items"]]

    assert "condition4-gap-recurrence" in detectors(3, "three")
    assert "condition4-gap-recurrence" not in detectors(2, "two")

    # (d) purity
    from ast_purity import impure_names

    assert impure_names(scan._detect_condition4_gap_recurrence) == set()
