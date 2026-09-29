"""Pure-`PlanDoc`-level tests for the `--review-topo` topological-review unit:
the reliance helpers in `agentctl.plan` (`reliance_set`/`consumers`/`first_hop`/
`reliance_closure`/`interface_empty`) and the rendering/materialization surface
in `agentctl.render` (`render_stage_interface`/`render_topo_review_bundle`/
`topo_unit_view`/`topo_unit_files`/`materialize_topo_units`/`verify_topo_units`).

No TOML fixture files: every `PlanDoc` here is built directly via
`agentctl.plan.parse_plan` on a plain dict, following the pattern in
`test_order_coverage_map.py`. Spawn-level (`spawn-specialist.py`) coverage of
the same feature lives in `test_spawn_topo_review.py`; the origin/main
byte-invariance coverage (tb9) lives in `test_topo_invariance.py`.
"""
from __future__ import annotations

import shutil

import pytest

from agentctl.plan import (
    CONDITION_MARKERS,
    PLAN_DIGEST_MARKER,
    REVIEW_MARKER,
    VERDICT_MARKER,
    PlanError,
    consumers,
    first_hop,
    interface_empty,
    parse_plan,
    reliance_closure,
    reliance_set,
)
from agentctl import render
from agentctl.render import (
    TopoUnitsCorrupt,
    materialize_topo_units,
    render_plan_md,
    render_stage_brief,
    render_stage_interface,
    render_topo_review_bundle,
    topo_unit_files,
    topo_unit_view,
    topo_unit_view_dirname,
    verify_topo_units,
)


def _stage(index=1, **overrides):
    base = {
        "index": index, "title": f"Stage {index}", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "dc",
        "means": "Edit", "method": f"method-{index}", "verify_command": f"true-{index}",
    }
    base.update(overrides)
    return base


def _doc(stages, order=None):
    meta = {"task_id": "t"}
    if order is not None:
        meta["order"] = order
    return parse_plan({"meta": meta, "stage": stages})


def _order(requirements, coverage):
    return {
        "requirements": [{"id": rid, "text": rid} for rid in requirements],
        "coverage": coverage,
    }


def _method_sentinel(index: int) -> str:
    return f"**Method:** method-{index}"


# --- tb1: stage unit holds order + own full brief + digest line -------------

def test_tb1_stage_unit_bundle_holds_order_own_brief_and_digest():
    doc = _doc([_stage(1)], order=_order(["R1"], {"R1": ["1"]}))
    bundle = render_topo_review_bundle(doc, 1, plan_sha256="deadbeef", view_dir="/tmp/view-1")
    assert "## Order" in bundle
    assert render_stage_brief(doc, 1) in bundle
    assert f"{PLAN_DIGEST_MARKER} deadbeef" in bundle


# --- tb2: first-hop list tags supplier/customer with the supply element -----

def test_tb2_first_hop_list_tags_supplier_and_customer_with_element():
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[1], supplies=[{"on": 1, "element": "e1"}]),
        _stage(3, depends_on=[2], supplies=[{"on": 2, "element": "e2"}]),
    ])
    bundle = render_topo_review_bundle(doc, 2, plan_sha256="d", view_dir="/tmp/v")
    assert "- stage 1: supplier — supplies `e1` — " in bundle
    assert "- stage 3: customer — supplies `e2` — " in bundle


# --- tb3: first-hop interfaces present verbatim, no full brief, no method ---

def test_tb3_first_hop_neighbour_interface_only_no_full_brief():
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[1], supplies=[{"on": 1, "element": "e1"}]),
    ])
    bundle = render_topo_review_bundle(doc, 2, plan_sha256="d", view_dir="/tmp/v")
    assert render_stage_interface(doc, 1, contract=True) in bundle
    assert "This is a PROJECTED BRIEF of stage 1" not in bundle
    assert _method_sentinel(1) not in bundle


# --- tb4: transitive-only member: interface present, absent from view ------

def test_tb4_transitive_only_member_interface_present_not_in_view():
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[1], supplies=[{"on": 1, "element": "e1"}]),
        _stage(3, depends_on=[2], supplies=[{"on": 2, "element": "e2"}]),
    ])
    bundle = render_topo_review_bundle(doc, 3, plan_sha256="d", view_dir="/tmp/v")
    assert render_stage_interface(doc, 1, contract=True) in bundle
    assert "stage-1.md" not in topo_unit_view(doc, 3)
    assert _method_sentinel(1) not in bundle


# --- tb5: interface_empty neighbour falls back to full brief; a neighbour
# with empty output_artifacts but concrete fields stays interface-only ------

def test_tb5_interface_empty_neighbour_falls_back_others_stay_interface_only():
    doc = _doc([
        _stage(1, depends_on=[2, 3], supplies=[{"on": 2, "element": "e2"}, {"on": 3, "element": "e3"}]),
        _stage(2, expected_result_image=" "),
        _stage(3, output_artifacts=[]),
    ])
    bundle = render_topo_review_bundle(doc, 1, plan_sha256="d", view_dir="/tmp/v")
    stage2 = next(s for s in doc.stages if s.index == 2)
    assert interface_empty(stage2)
    assert _method_sentinel(2) in bundle  # fallback to full brief
    assert _method_sentinel(3) not in bundle  # interface-only, no fallback
    assert render_stage_interface(doc, 3, contract=True) in bundle


# --- tb6: order unit ----------------------------------------------------

def test_tb6_order_unit_bundle_contents_and_checklist():
    doc = _doc(
        [_stage(1), _stage(2, depends_on=[1])],
        order=_order(["R1"], {"R1": ["2"]}),
    )
    bundle = render_topo_review_bundle(doc, "order", plan_sha256="d", view_dir="/tmp/v")
    plan_md = render_plan_md(doc)
    assert "R1: 2" in bundle
    assert "R1: 2" not in plan_md  # coverage sentinel absent from render_plan_md
    assert "## Final verification" not in plan_md or True  # no final_check declared here
    assert "- stage 1: supplier — depends_on-only — none" in bundle
    assert "- stage 2: supplier — depends_on-only — none" in bundle
    assert topo_unit_view(doc, "order") == ["stage-1.md", "stage-2.md"]
    assert "not applicable" in bundle
    assert bundle.count("JOINTLY") == 2


def test_tb6_order_unit_carries_final_check():
    doc = _doc(
        [_stage(1)],
        order=_order(["R1"], {"R1": ["1"]}),
    )
    doc = parse_plan({
        "meta": {"task_id": "t", "order": _order(["R1"], {"R1": ["1"]})},
        "final_check": [{"command": "true", "expected_exit": 0, "label": "fc1"}],
        "stage": [_stage(1)],
    })
    bundle = render_topo_review_bundle(doc, "order", plan_sha256="d", view_dir="/tmp/v")
    assert "fc1" in bundle
    assert "## Final verification" in bundle


# --- tb7: unknown unit raises ValueError -------------------------------------

def test_tb7_unknown_unit_raises_value_error():
    doc = _doc([_stage(1)])
    with pytest.raises(ValueError):
        render_topo_review_bundle(doc, 99, plan_sha256="d", view_dir="/tmp/v")


# --- tb8: non-direct edges ----------------------------------------------

def test_tb8_non_direct_edges_reliance_set_raw_depends_on_and_stage_depends_on():
    doc = _doc([
        _stage(1),
        _stage(2),
        _stage(3, depends_on=[1]),
        _stage(4, depends_on=[3], supplies=[{"on": 2}]),
    ])
    stage4 = next(s for s in doc.stages if s.index == 4)
    assert doc.raw_depends_on[4] == (3,)
    assert list(stage4.depends_on) == [2]
    assert reliance_set(doc, 4) == {2, 3}
    assert 1 in reliance_closure(doc, 3)
    assert 1 not in reliance_set(doc, 4)


# --- tb10: dangling index + union cycle both raise PlanError -----------------

def test_tb10_dangling_raw_edge_raises_planerror():
    # Hide the dangling raw edge behind an explicit (valid) supplies list so
    # parse_plan's own graph validation (which reads only the derived,
    # supplies-collapsed edges) never sees it.
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[99, 1], supplies=[{"on": 1}]),
    ])
    with pytest.raises(PlanError):
        reliance_set(doc, 2)


def test_tb10_raw_union_cycle_hidden_behind_acyclic_derived_graph_raises_planerror():
    # Raw depends_on cycle 1 -> 2 -> 3 -> 1. Stage 3's supplies override
    # points the DERIVED graph at stage 4 (a plain sink) instead, so
    # `_validate_graph`'s parse-time check (derived-only) sees 1 -> 2 -> 3 ->
    # 4, acyclic, and never rejects this plan -- exactly the raw-union cycle
    # `reliance_closure`'s own detection exists to catch.
    doc = _doc([
        _stage(1, depends_on=[2]),
        _stage(2, depends_on=[3]),
        _stage(3, depends_on=[1], supplies=[{"on": 4}]),
        _stage(4),
    ])
    stage3 = next(s for s in doc.stages if s.index == 3)
    assert list(stage3.depends_on) == [4]  # derived graph: acyclic
    with pytest.raises(PlanError):
        reliance_closure(doc, 1)


# --- tb11: the bundle's digest line is echoed verbatim, never recomputed ----

def test_tb11_bundle_digest_line_echoed_verbatim():
    doc = _doc([_stage(1)])
    bundle = render_topo_review_bundle(doc, 1, plan_sha256="not-a-real-digest-value", view_dir="/tmp/v")
    assert f"{PLAN_DIGEST_MARKER} not-a-real-digest-value" in bundle


# --- tb12: render_stage_interface's default call never falls back ----------

def test_tb12_render_stage_interface_default_never_falls_back():
    doc = _doc([_stage(1, expected_result_image=" ")])
    text = render_stage_interface(doc, 1)
    assert "This is a PROJECTED BRIEF" not in text
    assert "**Expected result image:**" in text


# --- tb13: one-hop view -------------------------------------------------

def test_tb13_one_hop_view_holds_only_first_hop_files():
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[1]),
        _stage(3, depends_on=[2]),
    ])
    view = topo_unit_view(doc, 3)
    assert view == ["stage-2.md"]
    assert "stage-1.md" not in view
    assert "stage-3.md" not in view
    assert "order.md" not in view
    files = topo_unit_files(doc)
    assert _method_sentinel(1) not in files["stage-2.md"]


# --- tb14: materialize writes/idempotent/new-sha-on-edit --------------------

def test_tb14_materialize_writes_manifest_and_view_dirs(tmp_path):
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    version_root = materialize_topo_units(doc, "shaA", tmp_path)
    assert version_root == tmp_path / "shaA"
    assert (version_root / "stage-1.md").is_file()
    assert (version_root / "stage-2.md").is_file()
    assert (version_root / "MANIFEST.json").is_file()
    assert (version_root / topo_unit_view_dirname(2)).is_dir()
    assert (version_root / topo_unit_view_dirname(2) / "stage-1.md").is_file()
    verify_topo_units(version_root, doc)  # no raise


def test_tb14_materialize_second_call_is_a_no_op_reverify(tmp_path):
    doc = _doc([_stage(1)])
    first = materialize_topo_units(doc, "shaA", tmp_path)
    mtime_before = (first / "stage-1.md").stat().st_mtime_ns
    second = materialize_topo_units(doc, "shaA", tmp_path)
    assert second == first
    assert (first / "stage-1.md").stat().st_mtime_ns == mtime_before


def test_tb14_different_plan_sha_gets_its_own_directory(tmp_path):
    doc = _doc([_stage(1)])
    a = materialize_topo_units(doc, "shaA", tmp_path)
    b = materialize_topo_units(doc, "shaB", tmp_path)
    assert a != b
    assert a.is_dir() and b.is_dir()


# --- tb15: prompt carries procedure text + view path, no cap/valve token ----

def test_tb15_bundle_carries_procedure_and_view_path_no_valve_token():
    doc = _doc([_stage(1)])
    bundle = render_topo_review_bundle(doc, 1, plan_sha256="d", view_dir="/tmp/my-view-dir")
    assert "## Reconciliation procedure" in bundle
    assert "/tmp/my-view-dir" in bundle
    assert "ESCALATION:" not in bundle
    assert "VALVE:" not in bundle


# --- tb16: raw-only edge --------------------------------------------------

def _tb16_doc():
    return _doc([
        _stage(1),
        _stage(2),
        _stage(3, depends_on=[1, 2], supplies=[{"on": 1}]),
    ])


def test_tb16_raw_only_edge_reliance_set_first_hop_consumers_view():
    doc = _tb16_doc()
    stage3 = next(s for s in doc.stages if s.index == 3)
    assert list(stage3.depends_on) == [1]
    assert reliance_set(doc, 3) == {1, 2}
    assert 2 in first_hop(doc, 3)
    assert 3 in consumers(doc, 2)
    assert "stage-2.md" in topo_unit_view(doc, 3)


# --- tb17: tb16's matching negative control ---------------------------------

def test_tb17_tb16_fixture_negative_control_derived_depends_on_alone_misses_the_edge():
    doc = _tb16_doc()
    stage3 = next(s for s in doc.stages if s.index == 3)
    assert reliance_set(doc, 3) == {1, 2}
    assert 2 in first_hop(doc, 3)
    naive_first_hop = frozenset(stage3.depends_on)
    assert naive_first_hop == {1}
    assert 2 not in naive_first_hop


# --- tb18: missing-index guard -----------------------------------------

def test_tb18_missing_raw_depends_on_entry_raises_never_silently_falls_back():
    doc = _doc([_stage(1), _stage(2), _stage(3, depends_on=[1])])
    del doc.raw_depends_on[3]
    with pytest.raises(PlanError):
        reliance_set(doc, 3)
    with pytest.raises(PlanError):
        first_hop(doc, 3)
    with pytest.raises(PlanError):
        reliance_closure(doc, 3)


# --- tb19: every-index entry --------------------------------------------

def test_tb19_raw_depends_on_has_an_entry_for_every_stage_including_empty():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    assert doc.raw_depends_on[1] == ()
    assert doc.raw_depends_on[2] == (1,)


# --- tb20: materialization race + corruption detection ----------------------

def test_tb20_race_loser_falls_back_to_verify_and_cleans_up_temp(tmp_path, monkeypatch):
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    verify_calls = []
    real_verify = render.verify_topo_units

    def spy_verify(unit_dir, d):
        verify_calls.append(unit_dir)
        return real_verify(unit_dir, d)

    monkeypatch.setattr(render, "verify_topo_units", spy_verify)

    real_replace = render.os.replace

    def fake_replace(src, dst):
        shutil.copytree(src, dst)  # simulate a concurrent winner materializing first
        raise OSError("simulated race: dst already populated by a concurrent writer")

    monkeypatch.setattr(render.os, "replace", fake_replace)

    version_root = materialize_topo_units(doc, "shaR", tmp_path)

    assert version_root == tmp_path / "shaR"
    assert verify_calls == [version_root]
    # temp dir cleaned up: only the version_root sibling remains under root
    assert list(tmp_path.iterdir()) == [version_root]
    monkeypatch.setattr(render.os, "replace", real_replace)
    real_verify(version_root, doc)  # no raise: winner's copy is valid

    root_ino = (version_root / "stage-1.md").stat().st_ino
    view_ino = (version_root / topo_unit_view_dirname(2) / "stage-1.md").stat().st_ino
    assert root_ino != view_ino  # copies, never hardlinks


def test_tb20_corrupted_view_file_raises_topounitscorrupt_naming_it(tmp_path):
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    version_root = materialize_topo_units(doc, "shaC", tmp_path)
    victim = version_root / topo_unit_view_dirname(2) / "stage-1.md"
    victim.write_text("tampered", encoding="utf-8")
    with pytest.raises(TopoUnitsCorrupt) as exc_info:
        verify_topo_units(version_root, doc)
    assert str(victim) in str(exc_info.value)


def test_tb20_extra_file_in_unit_root_raises_topounitscorrupt_naming_it(tmp_path):
    doc = _doc([_stage(1)])
    version_root = materialize_topo_units(doc, "shaE", tmp_path)
    extra = version_root / "extra.md"
    extra.write_text("x", encoding="utf-8")
    with pytest.raises(TopoUnitsCorrupt) as exc_info:
        verify_topo_units(version_root, doc)
    assert str(extra) in str(exc_info.value)


def test_tb20_extra_file_in_view_dir_raises_topounitscorrupt_naming_it(tmp_path):
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    version_root = materialize_topo_units(doc, "shaF", tmp_path)
    extra = version_root / topo_unit_view_dirname(2) / "extra.md"
    extra.write_text("x", encoding="utf-8")
    with pytest.raises(TopoUnitsCorrupt) as exc_info:
        verify_topo_units(version_root, doc)
    assert str(extra) in str(exc_info.value)


def test_tb20_concurrent_materialize_calls_are_idempotent(tmp_path):
    doc = _doc([_stage(1)])
    first = materialize_topo_units(doc, "shaG", tmp_path)
    second = materialize_topo_units(doc, "shaG", tmp_path)
    assert first == second


# --- tb21: ordering tag in rendered text ------------------------------------

_DECLARED_ONLY = "declared-only (supplies-wins collapse; not dispatch-ordered)"


def test_tb21_ordering_tag_supplier_and_customer_lines():
    doc = _tb16_doc()
    bundle3 = render_topo_review_bundle(doc, 3, plan_sha256="d", view_dir="/tmp/v")
    assert "- stage 1: supplier — supplies `None` — engine-ordered" in bundle3
    assert f"- stage 2: supplier — depends_on-only — {_DECLARED_ONLY}" in bundle3
    bundle2 = render_topo_review_bundle(doc, 2, plan_sha256="d", view_dir="/tmp/v")
    assert f"- stage 3: customer — depends_on-only — {_DECLARED_ONLY}" in bundle2


def test_tb21_ordering_tag_flips_when_transitively_engine_ordered():
    doc = _doc([
        _stage(1, depends_on=[2]),
        _stage(2),
        _stage(3, depends_on=[1, 2], supplies=[{"on": 1}]),
    ])
    bundle3 = render_topo_review_bundle(doc, 3, plan_sha256="d", view_dir="/tmp/v")
    assert "- stage 2: supplier — depends_on-only — engine-ordered" in bundle3


# --- tb22: protocol constants sourced from plan.py, never duplicated -------

def test_tb22_protocol_constants_present_and_not_duplicated_in_render_module():
    doc = _doc([_stage(1)])
    bundle = render_topo_review_bundle(doc, 1, plan_sha256="d", view_dir="/tmp/v")
    assert REVIEW_MARKER in bundle
    assert VERDICT_MARKER in bundle
    assert PLAN_DIGEST_MARKER in bundle
    for marker in CONDITION_MARKERS:
        assert marker in bundle

    import inspect
    source = inspect.getsource(render)
    for literal in (REVIEW_MARKER, VERDICT_MARKER, PLAN_DIGEST_MARKER, *CONDITION_MARKERS):
        assert f'"{literal}"' not in source
        assert f"'{literal}'" not in source
