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


def test_tr2_pass_without_plan_sha256_is_refused(env):
    d = env.record("topo:3-1", "pass", digest=None)
    assert not d.ok
    assert "--plan-digest" in d.detail
    assert env.state().plan_pair_reviews == {}


def test_tr2_pair_with_every_prerequisite_pair_missing_still_records(env):
    assert env.state().plan_pair_reviews == {}
    d = env.record("topo:3-2", "pass")
    assert d.ok, d.detail
    assert env.status("3-2") == "current"


def test_tr3_condition4_revise_appends_one_ledger_line_and_override_nets_it_out(env):
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


def test_tr3_revise_without_condition4_concern_and_any_pass_append_nothing(env):
    assert env.record("topo:3-1", "revise", concerns=[f"{CONDITION_MARKERS[0]} wording only"]).ok
    assert env.record("topo:3-2", "pass", concerns=[f"{C4} stray marker on a pass"]).ok
    assert env.record("topo:plan-3", "revise").ok
    assert env.ledger_lines() == []


def test_tr3_prior_whole_plan_pass_leaves_whole_plan_records_untouched(env):
    _seed_whole_plan_pass(env)
    before = _review_dicts(env.state())
    concern = f"{C4} gap behind a whole-plan pass"
    d = env.record("topo:3-1", "revise", concerns=[concern])
    assert d.ok, d.detail
    assert [row["outcome"] for row in env.ledger_lines()] == ["confirmed-gap"]
    state = env.state()
    assert state.plan_pair_reviews["3-1"].verdict == "revise"
    assert _review_dicts(state) == before


def test_tr6_method_only_edits_stale_exactly_the_pairs_whose_nodes_moved(env):
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


def test_tr6_order_requirement_edit_stales_every_pair_on_context(env):
    env.record_all()
    env.edit(lambda d: d["meta"]["order"]["requirements"][0].update(text="changed"))
    assert env.statuses() == {pair: "stale:context" for pair in review_pairs(env.doc())}


def test_tr6_final_check_command_edit_stales_plan_pairs_only(env):
    env.record_all()
    env.edit(lambda d: d["final_check"][0].update(command="false"))
    statuses = env.statuses()
    assert statuses["base-plan"].startswith("stale:")
    assert statuses["plan-3"].startswith("stale:")
    assert statuses["3-1"] == statuses["3-2"] == "current"


@pytest.mark.parametrize("mutate", [
    lambda d: d["meta"].update(goal="another goal"),
    lambda d: d["final_check"][0].update(label="fc-renamed"),
], ids=["goal", "final_check_label"])
def test_tr6_plan_meta_edit_stales_stage_stage_pairs_on_base_file(env, mutate):
    env.record_all()
    env.edit(mutate)
    statuses = env.statuses()
    assert statuses["3-1"] == statuses["3-2"] == "stale:base_file"
    assert statuses["plan-3"] == "stale:base"


def test_tr6_output_artifacts_edit_stales_base_plan_on_service_interface(env):
    env.record_all()
    env.edit(lambda d: d["stage"][1].update(output_artifacts=["new-artifact.py"]))
    assert env.status("base-plan") == "stale:service_interface"


def test_tr6_effects_only_edit_stales_the_pair_on_service_file(make_env):
    def effects(resolver):
        return [{"path": "scripts/x.sh", "sha256": "a" * 64, "resolver": resolver}]

    data = _tb8_data()
    data["stage"][2]["effects"] = effects("r1")
    env = make_env(data)
    env.record_all()
    env.edit(lambda d: d["stage"][2].update(effects=effects("r2")))
    assert env.status("4-3") == "stale:service_file"
    assert env.status("4-2") == "current"


def test_tr6_removing_a_raw_only_edge_stales_the_pair_on_edge_alone(env):
    env.record_all()
    before = pair_binding(env.doc(), "3-1")
    env.edit(lambda d: d["stage"][2].update(depends_on=[1]))
    after = pair_binding(env.doc(), "3-1")
    assert {k for k in before if before[k] != after[k]} == {"edge_digest"}
    assert env.status("3-1") == "stale:edge"


def test_tr6_ordering_tag_flip_via_a_third_stage_stales_the_pair_on_edge_alone(make_env):
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


def test_tr10_state_files_load_across_schema_variants(env):
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


def test_tr10_effects_edit_moves_the_service_file_digest(make_env):
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


def test_tr15_pass_echoing_a_digest_the_plan_no_longer_has_is_refused(env):
    spawn_time_digest = _sha(env.plan)
    env.edit(lambda d: d["stage"][0].update(method="edited after the node files were cut"))
    d = env.record("topo:3-1", "pass", digest=spawn_time_digest)
    assert not d.ok
    assert "fresh load" in d.detail
    assert env.state().plan_pair_reviews == {}


def test_tr15_record_against_target_copy_names_the_target(env):
    copy_path = env.tmp_path / "copy.toml"
    copy_path.write_bytes(env.plan.read_bytes())
    env.edit(lambda d: d["stage"][0].update(method="session plan moved on"))
    d = env.record("topo:3-1", "pass", target=copy_path)
    assert d.ok, d.detail
    record = env.state().plan_pair_reviews["3-1"]
    assert record.plan_path == str(copy_path)
    assert env.status("3-1", copy_path) == "current"
    assert env.status("3-1") == "missing"


def test_tr16_pair_revise_ignores_a_whole_plan_pass_over_superseded_content(env):
    _seed_whole_plan_pass(env, reviewed_meta_digest="superseded", reviewed_stage_keys={"1": "old"})
    d = env.record("topo:3-1", "revise", concerns=["needs work"])
    assert d.ok, d.detail
    assert env.state().plan_pair_reviews["3-1"].verdict == "revise"


def test_tr16_current_same_pair_pass_is_overturned_only_by_failing_regression_evidence(env):
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


def test_tr16_revise_over_an_override_needs_no_regression_command(env):
    assert env.record("topo:3-1", "pass").ok
    assert env.record("topo:3-1", "override", reviewer="fedor", note="accepted").ok
    assert env.status("3-1") == "override"
    assert env.record("topo:3-1", "revise", concerns=["reopened"]).ok


def test_tr16_stage_and_whole_plan_scopes_keep_their_terminal_pass_behaviour(env):
    for scope in ("", "stage:1"):
        assert env.record(scope, "pass").ok
        d = env.record(scope, "revise", concerns=["late objection"])
        assert d.data["plan_review_post_pass_unevidenced"] is True
        assert env.state().plan_review_passes[scope].verdict == "pass"


@pytest.mark.parametrize("pair", ["3-1", "plan-3", "base-plan"])
def test_tr16_scope_parsing_accepts_review_pairs(env, pair):
    assert plan_review_pair_scope(f"topo:{pair}") == pair
    assert env.record(f"topo:{pair}", "pass").ok
    assert pair in env.state().plan_pair_reviews


@pytest.mark.parametrize("scope", ["topo:9-1", "topo:3", "topo:order", "topo:foo", "topo:", "foo"])
def test_tr16_scope_parsing_refuses_everything_else(env, scope):
    d = env.record(scope, "pass")
    assert not d.ok
    assert env.state().plan_pair_reviews == {}


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


def test_tr18_interface_empty_service_binds_the_full_brief_fallback(make_env):
    env = make_env(_interface_empty_data())
    doc = env.doc()
    assert pair_binding(doc, "1-2")["service_interface_digest"] == _text_sha(
        render_stage_interface(doc, 2, contract=True))
    env.record_all()
    before = pair_binding(doc, "1-2")
    env.edit(lambda d: d["stage"][1].update(method="edited"))
    after = pair_binding(env.doc(), "1-2")
    assert after["service_interface_digest"] != before["service_interface_digest"]
    assert env.status("1-2") == "stale:service"


def test_tr18_source_service_binds_the_full_brief(make_env):
    env = make_env(_interface_empty_data())
    doc = env.doc()
    assert pair_binding(doc, "2-4")["service_interface_digest"] == _text_sha(render_stage_brief(doc, 4))
    env.record_all()
    before = pair_binding(doc, "2-4")
    env.edit(lambda d: d["stage"][3].update(method="edited"))
    after = pair_binding(env.doc(), "2-4")
    assert after["service_interface_digest"] != before["service_interface_digest"]
    assert env.status("2-4") == "stale:service"


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
