"""Content-keyed currency of plan-review units and pairs.

A pair (b, s) or a unit is current while the plan CONTENT it was reviewed against
is unchanged -- StageNorm / meta / order digests, never rendered text -- so a
render-code change with unchanged plan content leaves every record current, and a
change moves only the units and pairs whose evidence it touches. The review ids are
one unit kind (`unit:base`, `unit:<n>`) and the `<b>-<s>` pairs; there is no
`plan-<s>` pair and no `base-plan`.

Fixtures are plan dicts serialized to a real TOML file the session points at, so
every record is bound against a fresh load of bytes on disk and an edit is a
rewrite of that file. Entry points are the engine's own: `plan-review`,
`plan-review-walk`, `plan-review-compose`, `gates.pair_status` / `pair_walk`."""
from __future__ import annotations

import copy
import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates
from agentctl import plugins_obligations as obligations
from agentctl.plan import load_plan, pair_binding, review_pairs
from agentctl.state import GateRecord, Node, PlanPairReview, SessionState, StageStatus
from agentctl.store import FileStateStore

SID = "currency-s"
LEGACY_NODE_IDS = ("plan-3", "base-plan", "unit:plan")


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
        "expected_result_image": f"img-{index}", "done_criterion": f"dc-{index}",
        "means": f"means-{index}", "method": f"method-{index}", "verify_command": f"true-{index}",
    }
    stage.update(overrides)
    return stage


def _meta(coverage: dict | None = None, requirements: list | None = None) -> dict:
    return {
        "task_id": "t", "goal": "G", "done_criterion": "DC",
        "order": {
            "requirements": requirements or [{"id": "R1", "text": "R1"}],
            "coverage": coverage or {"R1": ["stage 3 verify_command"]},
        },
    }


def _fan_in() -> dict:
    """Stage 3 relies on stage 1 (typed edge for its `method`) and on stage 2 (raw
    depends_on only); the order's coverage names stage 3. Units: base, 1, 2, 3.
    Pairs: base-3, 3-1, 3-2."""
    return {
        "meta": _meta(),
        "final_check": [{"command": "true", "expected_exit": 0, "label": "fc1"}],
        "stage": [
            _stage(1),
            _stage(2),
            _stage(3, depends_on=[1, 2], supplies=[{"on": 1, "element": "method"}]),
        ],
    }


def _chain() -> dict:
    """1 <- 2 <- 3: stage 2 relies on something and has a concrete interface, so its
    pair shows it only as a declared product. Pairs: base-3, 3-2, 2-1."""
    return {
        "meta": _meta(),
        "final_check": [{"command": "true", "expected_exit": 0, "label": "fc1"}],
        "stage": [
            _stage(1),
            _stage(2, depends_on=[1], supplies=[{"on": 1, "element": "method"}]),
            _stage(3, depends_on=[2], supplies=[{"on": 2, "element": "method"}]),
        ],
    }


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Env:
    def __init__(self, tmp_path: Path, store: FileStateStore, data: dict):
        self.store = store
        self.plan = _write(tmp_path / "plan.toml", data)
        self._data = copy.deepcopy(data)
        self.store.save(SessionState(
            session_id=SID, task_id="t", weight_class="SUBSTANTIVE", plan_verified=True,
            plan_path=str(self.plan),
            approval=GateRecord("plan_approval", armed=True, passed=True),
        ))

    def state(self) -> SessionState:
        return self.store.load(SID)

    def doc(self):
        return load_plan(self.plan)

    def status(self, review_id: str) -> str:
        return gates.pair_status(self.state(), self.doc(), str(self.plan), review_id)

    def edit(self, mutate) -> None:
        data = copy.deepcopy(self._data)
        mutate(data)
        self._data = data
        _write(self.plan, data)

    def mutate_state(self, fn) -> None:
        state = self.state()
        fn(state)
        self.store.save(state)

    def record(self, review_id: str, verdict: str = "pass"):
        return cli.cmd_plan_review(
            Namespace(session=SID, target=None, scope=f"topo:{review_id}", verdict=verdict,
                      reviewer="thinker", concerns=None, note="", plan_digest=_sha(self.plan),
                      regression_command=None),
            store=self.store,
        )

    def record_ok(self, *review_ids: str) -> None:
        for rid in review_ids:
            d = self.record(rid)
            assert d.ok, f"{rid}: {d.detail}"

    def record_every_id(self) -> None:
        from agentctl.plan import review_ids
        self.record_ok(*review_ids(self.doc()))

    def walk_rows(self) -> list[dict]:
        return gates.pair_walk(self.state(), self.doc(), str(self.plan))

    def walk_ids(self) -> list[str]:
        return [row["pair"] for row in self.walk_rows()]

    def compose(self):
        return cli.cmd_plan_review_compose(Namespace(session=SID, target=None), store=self.store)


@pytest.fixture
def make_env(tmp_path, store, monkeypatch):
    monkeypatch.setenv("AGENTCTL_ESCALATION_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    return lambda data=None: Env(tmp_path, store, data if data is not None else _fan_in())


@pytest.fixture
def env(make_env) -> Env:
    return make_env()


def _change_render_code(monkeypatch) -> None:
    """The render module's text builders produce different bytes for the same plan
    content: what a change to the bundle layout looks like to a stored record."""
    from agentctl import render
    for name in ("node_file_text", "pair_service_text", "pair_edge_text"):
        original = getattr(render, name)
        monkeypatch.setattr(
            render, name, lambda *a, _original=original, **k: _original(*a, **k) + "\nrender-code-change")


def _set(stage_pos: int, **fields):
    return lambda d: d["stage"][stage_pos].update(**fields)


# --- the review id set --------------------------------------------------------------


def test_pc01_walk_has_no_plan_node_ids(env):
    ids = env.walk_ids()
    assert not [i for i in ids if i.startswith("plan-")], ids
    assert "base-plan" not in ids and "unit:plan" not in ids
    assert {"base-3", "3-1", "3-2"} <= set(ids)


def test_pc02_walk_lists_units_before_pairs_from_unit_base(env):
    ids = env.walk_ids()
    assert ids[:4] == ["unit:base", "unit:1", "unit:2", "unit:3"], ids
    assert ids[4:] == ["base-3", "3-1", "3-2"], ids
    kinds = [row["kind"] for row in env.walk_rows()]
    assert kinds == ["unit"] * 4 + ["pair"] * 3


def test_pc03_orphan_stage_is_caught_by_its_unit_not_a_plan_pair(make_env):
    data = _fan_in()
    data["stage"].append(_stage(4))
    env = make_env(data)
    ids = env.walk_ids()
    assert "plan-4" not in ids, ids
    assert "unit:4" in ids
    assert not [i for i in ids if i.endswith("-4")]
    assert env.status("unit:4") == "missing"


def test_pc04_orderless_plan_still_has_unit_base(make_env):
    data = _fan_in()
    del data["meta"]["order"]
    env = make_env(data)
    assert env.walk_ids()[0] == "unit:base"
    env.record_ok("unit:base")
    assert env.status("unit:base") == "current"


def test_pc05_walk_rows_carry_kind_and_service_of_a_unit(env):
    rows = {row["pair"]: row for row in env.walk_rows()}
    assert rows["unit:base"]["kind"] == "unit" and rows["unit:base"]["service"] == ""
    assert rows["3-1"]["kind"] == "pair" and rows["3-1"]["base"] == "3" and rows["3-1"]["service"] == "1"
    assert rows["unit:3"]["level"] < rows["3-1"]["level"] or rows["3-1"]["ready"] is False


# --- units ----------------------------------------------------------------------------


def test_pc06_unit_record_is_content_keyed_and_current(env):
    from agentctl.plan import unit_currency_hash
    env.record_ok("unit:base", "unit:3")
    state = env.state()
    record = state.plan_pair_reviews["unit:3"]
    assert record.service == "" and record.unit_norm == unit_currency_hash(env.doc(), "unit:3")
    assert env.status("unit:3") == "current" and env.status("unit:base") == "current"


def test_pc07_stage_unit_goes_stale_on_its_own_field_edit(env):
    env.record_ok("unit:1", "unit:2")
    env.edit(_set(0, title="Stage 1, retitled"))
    assert env.status("unit:1") == "stale:unit"
    assert env.status("unit:2") == "current"


def test_pc08_stage_unit_goes_stale_when_an_inbound_edge_changes(env):
    env.record_ok("unit:1")
    env.edit(_set(2, supplies=[{"on": 1, "element": "method"}, {"on": 1, "element": "result"}]))
    assert env.status("unit:1") == "stale:unit"


def test_pc09_stage_unit_stays_current_when_an_unrelated_stage_is_edited(env):
    env.record_ok("unit:1")
    env.edit(_set(1, method="a different method of stage 2"))
    assert env.status("unit:1") == "current"


def test_pc10_unit_base_follows_goal_and_requirements_but_not_stages(env):
    env.record_ok("unit:base", "unit:3", "3-1")
    env.edit(_set(0, method="a different method of stage 1"))
    assert env.status("unit:base") == "current"
    env.edit(lambda d: d["meta"].update(goal="another goal"))
    assert env.status("unit:base") == "stale:unit"
    assert env.status("unit:3") == "current" and env.status("3-1") == "stale:service"


def test_pc11_unit_base_goes_stale_on_a_requirement_edit(env):
    env.record_ok("unit:base")
    env.edit(lambda d: d["meta"]["order"]["requirements"][0].update(text="R1, restated"))
    assert env.status("unit:base") == "stale:unit"


# --- pairs: render code is not evidence -------------------------------------------


@pytest.mark.parametrize("pair", ["3-1", "3-2"])
def test_pc12_render_code_change_leaves_a_recorded_pair_current(env, monkeypatch, pair):
    env.record_ok(pair)
    assert env.status(pair) == "current"
    _change_render_code(monkeypatch)
    assert env.status(pair) == "current"


def test_pc13_render_code_change_leaves_every_recorded_unit_and_pair_current(env, monkeypatch):
    env.record_every_id()
    ids = env.walk_ids()
    _change_render_code(monkeypatch)
    stale = {i: env.status(i) for i in ids if env.status(i) != "current"}
    assert stale == {}


def test_pc14_pair_content_and_currency_hashes_ignore_render_text(env, monkeypatch):
    from agentctl.plan import pair_content, pair_currency_hash, unit_currency_hash
    doc = env.doc()
    before = (pair_content(doc, "3-1"), pair_currency_hash(doc, "base-3"),
              unit_currency_hash(doc, "unit:base"))
    _change_render_code(monkeypatch)
    after = (pair_content(doc, "3-1"), pair_currency_hash(doc, "base-3"),
             unit_currency_hash(doc, "unit:base"))
    assert before == after


def test_pc15_blank_interface_service_is_keyed_on_its_carry_digest(make_env):
    from agentctl.plan import pair_content
    from agentctl.stage_norm import StageNorm
    data = _fan_in()
    data["stage"][0].update(expected_result_image=" ", done_criterion=" ")
    env = make_env(data)
    doc = env.doc()
    token = pair_content(doc, "3-1")["service_iface_norm"]
    stage = next(s for s in doc.stages if s.index == 1)
    assert token == "full:" + StageNorm.from_stage(stage).carry_digest()


# --- pairs: which movement stales a record ------------------------------------------


def test_pc16_edit_of_the_supplied_element_stales_the_pair(env):
    env.record_ok("3-1")
    env.edit(_set(2, method="stage 3 method, rewritten"))
    assert env.status("3-1") == "stale:base"


def test_pc17_edit_outside_the_supplied_element_keeps_the_pair_current(env):
    env.record_ok("3-1")
    env.edit(_set(2, means="Read, Edit and Write"))
    assert env.status("3-1") == "current"


def test_pc18_element_less_edge_takes_the_whole_base(make_env):
    data = _fan_in()
    data["stage"][2]["supplies"] = [{"on": 1}]
    env = make_env(data)
    env.record_ok("3-1")
    env.edit(_set(2, means="Read, Edit and Write"))
    assert env.status("3-1") == "stale:base"


def test_pc19_edge_set_change_stales_the_pair(env):
    env.record_ok("3-1")
    env.edit(_set(2, supplies=[{"on": 1, "element": "method", "delivery": "artifact"}]))
    assert env.status("3-1") == "stale:edge"


def test_pc20_dropping_one_reliance_keeps_the_other_pair_current(env):
    env.record_ok("3-1", "3-2")
    env.edit(_set(2, depends_on=[1]))
    assert env.status("3-1") == "current"
    assert "3-2" not in env.walk_ids()


def test_pc21_source_service_construction_edit_stales_the_pair(env):
    env.record_ok("3-1")
    env.edit(_set(0, method="stage 1 method, rewritten"))
    assert env.status("3-1") == "stale:service"


def test_pc22_declared_product_service_construction_edit_keeps_the_pair_current(make_env):
    env = make_env(_chain())
    env.record_ok("3-2")
    env.edit(_set(1, method="stage 2 method, rewritten"))
    assert env.status("3-2") == "current"


def test_pc23_declared_product_service_interface_edit_stales_the_pair(make_env):
    env = make_env(_chain())
    env.record_ok("3-2")
    env.edit(_set(1, done_criterion="a different done criterion"))
    assert env.status("3-2") == "stale:service_iface"


def test_pc24_base_pair_follows_only_the_requirements_it_names(make_env):
    data = _fan_in()
    data["meta"] = _meta(
        coverage={"R1": ["stage 3 verify_command"], "R2": ["stage 2 verify_command"]},
        requirements=[{"id": "R1", "text": "R1"}, {"id": "R2", "text": "R2"}],
    )
    env = make_env(data)
    assert "base-3" in env.walk_ids() and "base-2" in env.walk_ids()
    env.record_ok("base-3", "unit:base")
    env.edit(lambda d: d["meta"]["order"]["requirements"][1].update(text="R2, restated"))
    assert env.status("unit:base") == "stale:unit"
    assert env.status("base-3") == "current"
    env.edit(lambda d: d["meta"]["order"]["requirements"][0].update(text="R1, restated"))
    assert env.status("base-3") == "stale:base"


def test_pc25_coverage_retarget_moves_the_base_pair(env):
    env.edit(lambda d: d["meta"]["order"].update(coverage={"R1": ["stage 2 verify_command"]}))
    ids = env.walk_ids()
    assert "base-2" in ids and "base-3" not in ids


# --- records written before the content digests -------------------------------------


def _legacy_binding_record(env: Env, pair: str) -> PlanPairReview:
    doc = env.doc()
    b, s = pair.split("-", 1)
    return PlanPairReview(
        pair=pair, base=int(b) if b.isdigit() else b, service=int(s), verdict="pass",
        reviewer="thinker", plan_path=str(env.plan), plan_sha256=_sha(env.plan),
        **pair_binding(doc, pair),
    )


def test_pc26_legacy_binding_record_is_current_while_the_plan_is_unchanged(env):
    record = _legacy_binding_record(env, "3-1")
    env.mutate_state(lambda st: st.plan_pair_reviews.update({"3-1": record}))
    assert env.status("3-1") == "current"


def test_pc27_legacy_binding_record_survives_a_render_code_change(env, monkeypatch):
    record = _legacy_binding_record(env, "3-1")
    env.mutate_state(lambda st: st.plan_pair_reviews.update({"3-1": record}))
    _change_render_code(monkeypatch)
    assert env.status("3-1") == "current"


def test_pc28_legacy_binding_record_goes_stale_when_plan_content_moves(env):
    record = _legacy_binding_record(env, "3-1")
    env.mutate_state(lambda st: st.plan_pair_reviews.update({"3-1": record}))
    env.edit(_set(0, method="stage 1 method, rewritten"))
    assert env.status("3-1") == "stale:service"


def test_pc29_retired_plan_node_records_load_and_are_stale_never_current(env):
    def seed(state):
        for rid in LEGACY_NODE_IDS:
            base, _, service = rid.partition("-") if not rid.startswith("unit:") else ("plan", "", "")
            state.plan_pair_reviews[rid] = PlanPairReview(
                pair=rid, base=base, service=int(service) if service.isdigit() else service,
                verdict="pass", reviewer="thinker", plan_path=str(env.plan), plan_sha256=_sha(env.plan))
    env.mutate_state(seed)
    state = env.state()
    assert set(LEGACY_NODE_IDS) <= set(state.plan_pair_reviews)
    for rid in LEGACY_NODE_IDS:
        assert gates.pair_status(state, env.doc(), str(env.plan), rid) == "stale:legacy", rid
    assert not set(LEGACY_NODE_IDS) & set(env.walk_ids())


# --- whole-plan composition and the walk-stale sets ---------------------------------------


def test_pc30_compose_is_refused_while_a_unit_is_missing(env):
    env.record_ok(*review_pairs(env.doc()))
    d = env.compose()
    assert not d.ok
    assert "unit:" in d.detail


def test_pc31_compose_names_the_orphan_stage_unit_it_still_lacks(make_env):
    from agentctl.plan import review_ids
    data = _fan_in()
    data["stage"].append(_stage(4))
    env = make_env(data)
    env.record_ok(*[i for i in review_ids(env.doc()) if i != "unit:4"])
    d = env.compose()
    assert not d.ok and "unit:4" in d.detail


def test_pc32_compose_accepts_every_unit_and_pair_and_keeps_unit_currency(env):
    from agentctl.plan import review_units, unit_currency_hash
    env.record_every_id()
    d = env.compose()
    assert d.ok, d.detail
    baseline = env.state().plan_review
    assert baseline.reviewed_unit_currency == {
        u: unit_currency_hash(env.doc(), u) for u in review_units(env.doc())}


def test_pc33_unit_records_alone_never_switch_the_plan_onto_the_pair_route(env):
    env.record_ok("unit:base", "unit:1")
    assert not gates.has_pair_records_for(env.state(), str(env.plan))
    env.record_ok("3-1")
    assert gates.has_pair_records_for(env.state(), str(env.plan))


def test_pc34_walk_stale_sets_follow_only_what_a_stage_edit_touched(env):
    env.record_every_id()
    assert env.compose().ok
    env.edit(_set(0, method="stage 1 method, rewritten"))
    state, doc, path = env.state(), env.doc(), str(env.plan)
    baseline = state.plan_review
    assert gates.walk_stale_units(state, doc, path, baseline) == ["unit:1"]
    assert gates.walk_stale_pairs(state, doc, path, baseline) == ["3-1"]


def test_pc35_render_code_change_leaves_the_walk_stale_sets_empty(env, monkeypatch):
    env.record_every_id()
    assert env.compose().ok
    _change_render_code(monkeypatch)
    state, doc, path = env.state(), env.doc(), str(env.plan)
    assert gates.walk_stale_units(state, doc, path, state.plan_review) == []
    assert gates.walk_stale_pairs(state, doc, path, state.plan_review) == []


def test_pc36_baseline_without_unit_currency_walks_every_unit(env):
    from agentctl.plan import review_units
    env.record_every_id()
    assert env.compose().ok
    env.mutate_state(lambda st: setattr(st.plan_review, "reviewed_unit_currency", {}))
    state = env.state()
    stale = gates.walk_stale_units(state, env.doc(), str(env.plan), state.plan_review)
    assert stale == list(review_units(env.doc()))


# --- declared fields the stage file renders beyond the question vocabulary ------------


def _effects(resolver: str) -> list[dict]:
    return [{"path": "scripts/x.sh", "sha256": "a" * 64, "resolver": resolver}]


def test_pc37_effects_edit_stales_the_unit_and_the_pairs_that_show_the_stage_in_full(make_env):
    data = _fan_in()
    data["stage"][0]["effects"] = _effects("r1")
    env = make_env(data)
    env.record_every_id()
    env.edit(_set(0, effects=_effects("r2")))
    assert env.status("unit:1") == "stale:unit"
    assert env.status("3-1") == "stale:service"
    assert {i for i in env.walk_ids() if env.status(i) != "current"} == {"unit:1", "3-1"}


def test_pc38_effects_edit_of_a_declared_product_service_leaves_its_consumer_pair_current(make_env):
    data = _chain()
    data["stage"][1]["effects"] = _effects("r1")
    env = make_env(data)
    env.record_every_id()
    env.edit(_set(1, effects=_effects("r2")))
    assert env.status("unit:2") == "stale:unit"
    assert env.status("3-2") == "current"
    assert env.status("2-1") == "current"


def test_pc39_output_artifacts_edit_stales_the_stage_unit(env):
    env.record_ok("unit:1", "unit:2")
    env.edit(_set(0, output_artifacts=["build/out.txt"]))
    assert env.status("unit:1") == "stale:unit"
    assert env.status("unit:2") == "current"


def test_pc40_a_stage_without_those_fields_keeps_the_question_vocabulary_digest(env):
    from agentctl.plan import WHOLE_STAGE_ELEMENT, stage_element_keys, unit_content
    doc = env.doc()
    stage = next(s for s in doc.stages if s.index == 1)
    assert unit_content(doc, 1)["stage"] == stage_element_keys(stage)[WHOLE_STAGE_ELEMENT]


# --- the walk order ----------------------------------------------------------------


def test_pc41_a_prerequisite_pair_sits_at_a_strictly_earlier_level_than_its_dependant(make_env):
    from agentctl.plan import review_pairs, split_pair_id
    data = _fan_in()
    data["stage"] = [
        _stage(1),
        _stage(2, depends_on=[1], supplies=[{"on": 1}]),
        _stage(3, depends_on=[1], supplies=[{"on": 1}]),
        _stage(4, depends_on=[1, 2, 3], supplies=[{"on": 2}, {"on": 3}]),
    ]
    data["meta"] = _meta(coverage={"R1": ["stage 4 verify_command"]})
    env = make_env(data)
    doc = env.doc()
    levels = {row["pair"]: row["level"] for row in env.walk_rows()}
    depths = gates.pair_depths(doc)
    assert {"4-1", "2-1"} <= set(review_pairs(doc))
    for pid in review_pairs(doc):
        assert levels[pid] >= depths[split_pair_id(pid)[1]], pid
        for prereq in gates.pair_prereqs(doc, pid):
            assert levels[prereq] < levels[pid], (pid, prereq)
    assert levels["4-1"] < levels["2-1"]


# --- base-<s>: the evidence is what the bundle shows of s --------------------------


def _covered_stage_one(*, blank_interface: bool) -> dict:
    data = _fan_in()
    data["meta"] = _meta(coverage={"R1": ["stage 1 verify_command"]})
    if blank_interface:
        data["stage"][0].update(expected_result_image=" ", done_criterion=" ")
    return data


def test_pc42_blank_interface_service_goes_stale_on_a_material_edit_in_its_base_pair(make_env):
    env = make_env(_covered_stage_one(blank_interface=True))
    env.record_ok("base-1")
    env.edit(_set(0, material="the files this stage reads, restated"))
    assert env.status("base-1") == "stale:service"


def test_pc43_blank_interface_service_goes_stale_on_an_output_artifacts_edit_in_its_base_pair(make_env):
    env = make_env(_covered_stage_one(blank_interface=True))
    env.record_ok("base-1")
    env.edit(_set(0, output_artifacts=["build/out.txt"]))
    assert env.status("base-1").startswith("stale:")


def test_pc44_declared_product_service_keeps_its_base_pair_on_an_edit_the_bundle_hides(make_env):
    env = make_env(_covered_stage_one(blank_interface=False))
    env.record_ok("base-1")
    env.edit(_set(0, material="the files this stage reads, restated"))
    assert env.status("base-1") == "current"
    env.edit(_set(0, method="stage 1 method, rewritten"))
    assert env.status("base-1") == "current"


def test_pc45_declared_product_service_goes_stale_on_the_output_artifacts_its_bundle_shows(make_env):
    env = make_env(_covered_stage_one(blank_interface=False))
    env.record_ok("base-1")
    env.edit(_set(0, output_artifacts=["build/out.txt"]))
    assert env.status("base-1") == "stale:service_iface"


def test_pc46_base_pair_bundle_of_a_blank_interface_service_shows_the_edited_field(make_env):
    from agentctl import render
    from agentctl.plan import PAIR_BASE_NODE
    env = make_env(_covered_stage_one(blank_interface=True))
    env.edit(_set(0, material="the files this stage reads, restated"))
    shown = render.pair_service_text(env.doc(), 1, PAIR_BASE_NODE)
    assert "the files this stage reads, restated" in shown


# --- the obligations backstop through the real verbs --------------------------------


def _ns(**kw) -> Namespace:
    return Namespace(**kw)


class Engine:
    """One SUBSTANTIVE session driven through the engine's own verbs with the
    plan-review gate live, so the plan-review obligation is minted by the real
    `submit_plan` observer and the replan runs the real `cmd_replan`."""

    def __init__(self, tmp_path: Path, store: FileStateStore):
        self.tmp, self.store, self._n = tmp_path, store, 0

    def plan_file(self, data: dict, *, at: Path | None = None) -> Path:
        """Write `data` as a plan; with `at`, over that path, so a recorded review
        keeps its plan path and only the content moved."""
        self._n += 1
        declared = copy.deepcopy(data)
        declared["meta"]["weight_class"] = "small_change"
        return _write(at or self.tmp / f"engine-plan-{self._n}.toml", declared)

    def _run(self, command: str, fn, **kw):
        """`cli.main`'s own sequence -- the verb, then the plugin event it fires --
        without its argv parsing."""
        args = _ns(command=command, session=SID, **kw)
        directive = fn(args, store=self.store)
        cli._fire_plugins(args, self.store, directive)
        return directive

    def open(self, plan: Path):
        self._run("start", cli.cmd_start, task="t", goal="", done_criterion="",
                  criterion_type="measurable", recursion_depth=0)
        self._run("classify", cli.cmd_classify, chat=False, changed_lines=200, files=5,
                  wall_clock_min=60, tracker_key=None, architectural=True,
                  external_effect=False, new_dependency=False, public_api_change=False)
        self._run("plan", cli.cmd_plan)
        return self._run("submit-plan", cli.cmd_submit_plan, plan=str(plan))

    def review_every_id(self, plan: Path) -> None:
        from agentctl.plan import review_ids
        for rid in review_ids(load_plan(plan)):
            d = cli.cmd_plan_review(
                _ns(session=SID, target=str(plan), scope=f"topo:{rid}", verdict="pass",
                    reviewer="thinker", concerns=None, note="", plan_digest=_sha(plan),
                    regression_command=None),
                store=self.store)
            assert d.ok, f"{rid}: {d.detail}"
        composed = cli.cmd_plan_review_compose(_ns(session=SID, target=str(plan)), store=self.store)
        assert composed.ok, composed.detail

    def approve(self) -> None:
        d = self._run("approve", cli.cmd_approve, by="user")
        assert d.ok, d.detail

    def start_execution(self) -> None:
        self._run("partition", cli.cmd_partition, m1=False, m2=False, m3=False, m4=False,
                  m3_severe=False, m4_severe=False)
        self._run("next-stage", cli.cmd_next_stage)

    def replan(self, plan: Path):
        return self._run("replan", cli.cmd_replan, plan=str(plan))

    def state(self) -> SessionState:
        return self.store.load(SID)

    def guardian(self) -> list[str]:
        state = self.state()
        return obligations._resolution_guardian(state, state.plugins["obligations"])


@pytest.fixture
def engine(tmp_path, store, monkeypatch) -> Engine:
    monkeypatch.setenv("AGENTCTL_ESCALATION_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_REVIEW_DISPATCH", "1")
    monkeypatch.setenv("AGENTCTL_OBLIGATIONS", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    return Engine(tmp_path, store)


def _started_session(engine: Engine) -> Path:
    plan = engine.plan_file(_fan_in())
    engine.open(plan)
    engine.review_every_id(plan)
    engine.approve()
    engine.start_execution()
    return plan


def test_pc47_submit_plan_mints_the_obligation_and_it_holds_until_the_review_is_current(engine):
    plan = engine.plan_file(_fan_in())
    engine.open(plan)
    assert len(engine.guardian()) == 1
    engine.review_every_id(plan)
    assert engine.guardian() == []


def test_pc48_a_refinement_that_moves_a_pairs_service_leaves_the_started_plan_discharged(
        engine, monkeypatch):
    plan = _started_session(engine)
    assert engine.state().stage(1).outcome.status != StageStatus.PENDING.value
    refined = _fan_in()
    refined["stage"][0]["expected_result_image"] = "stage 1 image, reworded"
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "0")
    d = engine.replan(engine.plan_file(refined, at=plan))
    assert d.ok and d.action == "continue", d.detail
    state = engine.state()
    assert state.node == Node.EXECUTING.value and state.approval.passed
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    assert gates.pair_status(state, load_plan(state.plan_path), state.plan_path,
                             "unit:1") == "stale:unit"
    assert engine.guardian() == []


def test_pc49_a_substantive_replan_reopens_the_obligation_until_the_new_plan_is_reviewed(
        engine, monkeypatch):
    _started_session(engine)
    bigger = _fan_in()
    bigger["stage"].append(_stage(4))
    bigger_plan = engine.plan_file(bigger)
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "0")
    d = engine.replan(bigger_plan)
    assert d.marker == "PLAN-READY", d.detail
    assert not engine.state().approval.passed
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    assert len(engine.guardian()) == 1
    engine.review_every_id(bigger_plan)
    assert engine.guardian() == []


def test_pc50_the_inferred_latch_is_a_stage_having_left_pending_not_a_stored_flag(engine):
    plan = engine.plan_file(_fan_in())
    engine.open(plan)
    engine.review_every_id(plan)
    engine.approve()
    assert all(s.outcome.status == StageStatus.PENDING.value for s in engine.state().stages)
    state = engine.state()
    state.plan_pair_reviews.clear()
    state.plan_review = None
    engine.store.save(state)
    assert len(engine.guardian()) == 1
    engine.start_execution()
    assert engine.state().stage(1).outcome.status != StageStatus.PENDING.value
    assert engine.guardian() == []
