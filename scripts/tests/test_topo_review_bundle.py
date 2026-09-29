"""Pure-`PlanDoc`-level tests for the `--review-topo` topological-review unit:
the reliance helpers in `agentctl.plan` (`reliance_set`/`consumers`/`first_hop`/
`reliance_closure`/`interface_empty`) and the rendering/materialization surface
in `agentctl.render` (`render_stage_interface`/`render_topo_review_bundle`/
`topo_unit_view`/`topo_unit_files`/`materialize_topo_units`/`verify_topo_units`).

No TOML fixture files: every `PlanDoc` here is built directly via
`agentctl.plan.parse_plan` on a plain dict, following the pattern in
`test_order_coverage_map.py`. Spawn-level (`spawn-specialist.py`) coverage of
the same feature lives in `test_spawn_topo_review.py`.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

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
from agentctl.render import (
    TopoUnitsCorrupt,
    materialize_topo_units,
    render_stage_brief,
    render_stage_interface,
    render_topo_review_bundle,
    topo_unit_files,
    topo_unit_view,
    verify_topo_units,
)


def _stage(index=1, **overrides):
    base = {
        "index": index, "title": f"Stage {index}", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "dc",
        "means": "Edit", "method": "do", "verify_command": f"true-{index}",
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


# --- raw_depends_on capture --------------------------------------------------

def test_raw_depends_on_captured_for_every_stage_including_empty():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    assert doc.raw_depends_on[1] == ()
    assert doc.raw_depends_on[2] == (1,)


# --- the supplies-wins-collapse regression + its fix -------------------------

def test_supplies_override_collapses_the_depends_on_property():
    # The bug this whole feature works around: `[[stage.supplies]]`, when
    # present, replaces `depends_on` WHOLESALE on the derived property.
    doc = _doc([
        _stage(1, output_artifacts=["a.txt"]),
        _stage(2),
        _stage(3, depends_on=[1, 2], supplies=[{"on": 2}]),
    ])
    stage3 = next(s for s in doc.stages if s.index == 3)
    assert [s.on for s in stage3.supplies] == [2]
    assert list(stage3.depends_on) == [2]  # stage 1 is invisible on the derived property


def test_reliance_set_recovers_the_edge_the_derived_property_lost():
    doc = _doc([
        _stage(1, output_artifacts=["a.txt"]),
        _stage(2),
        _stage(3, depends_on=[1, 2], supplies=[{"on": 2}]),
    ])
    assert reliance_set(doc, 3) == {1, 2}


def test_consumers_reads_the_raw_union_not_the_derived_depends_on():
    doc = _doc([
        _stage(1, output_artifacts=["a.txt"]),
        _stage(2),
        _stage(3, depends_on=[1, 2], supplies=[{"on": 2}]),
    ])
    # A consumers() built on the derived Stage.depends_on would miss stage 1
    # entirely, since stage3.depends_on == (2,) alone.
    assert consumers(doc, 1) == {3}
    assert consumers(doc, 2) == {3}


def test_first_hop_is_the_union_of_relies_on_and_consumed_by():
    doc = _doc([
        _stage(1, output_artifacts=["a.txt"]),
        _stage(2),
        _stage(3, depends_on=[1, 2], supplies=[{"on": 2}]),
    ])
    assert first_hop(doc, 3) == {1, 2}
    assert first_hop(doc, 1) == {3}


def test_reliance_closure_is_transitive_and_excludes_the_unit_itself():
    doc = _doc([
        _stage(1, output_artifacts=["a.txt"]),
        _stage(2),
        _stage(3, depends_on=[1, 2], supplies=[{"on": 2}]),
        _stage(4, depends_on=[3]),
    ])
    closure = reliance_closure(doc, 4)
    assert closure == {1, 2, 3}
    assert 4 not in closure


# --- dangling / missing stage indices ----------------------------------------

def test_reliance_set_raises_on_a_wholly_unknown_stage():
    doc = _doc([_stage(1)])
    with pytest.raises(PlanError):
        reliance_set(doc, 99)


def test_reliance_set_raises_on_a_raw_only_dangling_edge():
    # depends_on=[99] is dangling, but the supplies override hides it from
    # the DERIVED graph (which _validate_graph already checked at parse
    # time) -- reliance_set must catch it independently.
    doc = _doc([
        _stage(1, depends_on=[99], supplies=[{"on": 2}]),
        _stage(2),
    ])
    with pytest.raises(PlanError, match="unknown stage"):
        reliance_set(doc, 1)


def test_reliance_closure_detects_a_raw_cycle_hidden_behind_an_acyclic_derived_graph():
    # Derived (supplies-projected) graph is the acyclic 1 -> 2 -> 3, which
    # _validate_graph accepts at parse time. The RAW union graph reliance_set
    # reads has an extra edge (2's raw depends_on=[1]) closing 1 -> 2 -> 1.
    doc = _doc([
        _stage(1, depends_on=[2]),
        _stage(2, depends_on=[1], supplies=[{"on": 3}]),
        _stage(3),
    ])
    with pytest.raises(PlanError, match="reliance cycle detected"):
        reliance_closure(doc, 1)


# --- interface_empty + render_stage_interface's contract fallback -----------

def test_interface_empty_is_governed_solely_by_output_artifacts():
    doc = _doc([_stage(1, output_artifacts=["a.txt"]), _stage(2)])
    stage1 = next(s for s in doc.stages if s.index == 1)
    stage2 = next(s for s in doc.stages if s.index == 2)
    assert interface_empty(stage1) is False
    assert interface_empty(stage2) is True


def test_render_stage_interface_projects_when_not_empty():
    doc = _doc([_stage(1, output_artifacts=["a.txt"])])
    text = render_stage_interface(doc, 1, contract=True)
    assert "## Stage 1: Stage 1" in text
    assert "**Output artifacts:**" in text
    assert "**Means:**" not in text  # a projection, not the full brief


def test_render_stage_interface_falls_back_to_full_brief_when_empty():
    doc = _doc([_stage(1)])  # no output_artifacts -> interface_empty
    text = render_stage_interface(doc, 1, contract=True)
    assert "**Means:** Edit" in text  # only the full brief renders this
    assert "This is a PROJECTED BRIEF" in text


def test_render_stage_interface_without_contract_never_falls_back():
    doc = _doc([_stage(1)])  # interface_empty, but contract defaults False
    text = render_stage_interface(doc, 1)
    assert "**Means:**" not in text
    assert "**Output artifacts:** *(none declared)*" in text


def test_render_stage_interface_raises_on_unknown_stage():
    doc = _doc([_stage(1)])
    with pytest.raises(PlanError):
        render_stage_interface(doc, 99)


# --- topo_unit_view / topo_unit_files ---------------------------------------

def test_topo_unit_view_naming():
    assert topo_unit_view(3) == "view-3"
    assert topo_unit_view("order") == "view-order"


def test_topo_unit_files_for_a_stage_unit():
    doc = _doc([
        _stage(1, output_artifacts=["a.txt"]),
        _stage(2, depends_on=[1]),
    ])
    files = topo_unit_files(doc, 2)
    assert set(files.keys()) == {"own.md", "1.md"}
    assert files["own.md"] == render_stage_brief(doc, 2)  # own.md is stage 2's full brief
    assert files["1.md"] == render_stage_brief(doc, 1)  # neighbour files are always full briefs


def test_topo_unit_files_for_the_order_unit():
    # _order_coverage_stage_indices only picks out coverage values that
    # parse as a BARE stage index -- a free-text control string (e.g.
    # "stage 1 verify_command") is silently skipped, per its own docstring.
    order = _order(["R1"], {"R1": ["1", "2"]})
    doc = _doc([_stage(1, output_artifacts=["a.txt"]), _stage(2)], order=order)
    files = topo_unit_files(doc, "order")
    assert set(files.keys()) == {"own.md", "1.md", "2.md"}


def test_order_coverage_stage_indices_skips_free_text_refs():
    order = _order(["R1"], {"R1": ["stage 1 verify_command", "2"]})
    doc = _doc([_stage(1, output_artifacts=["a.txt"]), _stage(2)], order=order)
    files = topo_unit_files(doc, "order")
    assert set(files.keys()) == {"own.md", "2.md"}


# --- materialize_topo_units / verify_topo_units ------------------------------

def test_materialize_topo_units_writes_the_expected_files(tmp_path):
    doc = _doc([_stage(1, output_artifacts=["a.txt"]), _stage(2, depends_on=[1])])
    out = materialize_topo_units(doc, tmp_path, [2], plan_sha256="deadbeef")
    view_dir = out["2"]
    assert view_dir == tmp_path / "deadbeef" / "view-2"
    assert {p.name for p in view_dir.iterdir()} == {"own.md", "1.md"}
    assert (view_dir / "own.md").read_text(encoding="utf-8") == topo_unit_files(doc, 2)["own.md"]


def test_materialize_topo_units_is_idempotent(tmp_path):
    doc = _doc([_stage(1, output_artifacts=["a.txt"])])
    materialize_topo_units(doc, tmp_path, [1], plan_sha256="d")
    out = materialize_topo_units(doc, tmp_path, [1], plan_sha256="d")
    assert out["1"].is_dir()


def test_materialize_topo_units_race_loser_falls_back_to_verify(tmp_path, monkeypatch):
    from agentctl import render as render_mod

    doc = _doc([_stage(1, output_artifacts=["a.txt"])])

    def fake_replace(src, dst):
        # Simulate a concurrent winner: it populates dst with the correct
        # files and discards its own tmp dir, then this call raises OSError
        # the way os.replace does onto a non-empty destination.
        os.makedirs(dst, exist_ok=True)
        for f in Path(src).iterdir():
            shutil.copy(f, Path(dst) / f.name)
        shutil.rmtree(src, ignore_errors=True)
        raise OSError("simulated concurrent winner")

    monkeypatch.setattr(render_mod.os, "replace", fake_replace)
    out = materialize_topo_units(doc, tmp_path, [1], plan_sha256="d")
    assert out["1"].is_dir()
    assert (out["1"] / "own.md").exists()


def test_verify_topo_units_detects_a_missing_directory(tmp_path):
    doc = _doc([_stage(1, output_artifacts=["a.txt"])])
    with pytest.raises(TopoUnitsCorrupt):
        verify_topo_units(doc, tmp_path, "d", [1])


def test_verify_topo_units_detects_content_tampering(tmp_path):
    doc = _doc([_stage(1, output_artifacts=["a.txt"])])
    out = materialize_topo_units(doc, tmp_path, [1], plan_sha256="d")
    (out["1"] / "own.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(TopoUnitsCorrupt):
        verify_topo_units(doc, tmp_path, "d", [1])


def test_verify_topo_units_detects_an_extra_file(tmp_path):
    doc = _doc([_stage(1, output_artifacts=["a.txt"])])
    out = materialize_topo_units(doc, tmp_path, [1], plan_sha256="d")
    (out["1"] / "extra.md").write_text("x", encoding="utf-8")
    with pytest.raises(TopoUnitsCorrupt):
        verify_topo_units(doc, tmp_path, "d", [1])


# --- render_topo_review_bundle: markers sourced from plan.py's constants ----

def test_render_topo_review_bundle_sources_every_marker_from_plan_constants():
    doc = _doc([_stage(1, output_artifacts=["a.txt"])])
    bundle = render_topo_review_bundle(doc, 1, plan_sha256="deadbeef", view_dir=Path("/tmp/view-1"))
    assert f"{PLAN_DIGEST_MARKER} deadbeef" in bundle
    assert f"reply with the {REVIEW_MARKER} block" in bundle
    for marker in CONDITION_MARKERS:
        assert f"`{marker}`" in bundle
    assert f"`{VERDICT_MARKER}` pass | revise | override" in bundle


def test_render_topo_review_bundle_stage_unit_neighbours_and_transitive_sections():
    doc = _doc([
        _stage(1, output_artifacts=["a.txt"]),
        _stage(2),
        _stage(3, depends_on=[1, 2], supplies=[{"on": 2}]),
        _stage(4, depends_on=[3]),
    ])
    view_dir = Path("/tmp/view-4")
    bundle = render_topo_review_bundle(doc, 4, plan_sha256="abc123", view_dir=view_dir)
    assert "# Topological review unit: 4" in bundle
    assert "- relies-on: stage 3" in bundle
    assert "- consumed-by:" not in bundle  # nothing consumes stage 4
    assert f"reachable via exactly one `Read` under `{view_dir}`" in bundle
    # stage 3's own interface is embedded verbatim (first-hop, empty interface -> fallback)
    assert render_stage_interface(doc, 3, contract=True).rstrip() in bundle
    # stages 1 and 2 are transitive-only (beyond the first hop)
    assert render_stage_interface(doc, 1, contract=True).rstrip() in bundle
    assert render_stage_interface(doc, 2, contract=True).rstrip() in bundle


def test_render_topo_review_bundle_embeds_neighbour_interface_verbatim():
    doc = _doc([_stage(1, output_artifacts=["a.txt"]), _stage(2, depends_on=[1])])
    view_dir = Path("/tmp/view-2")
    bundle = render_topo_review_bundle(doc, 2, plan_sha256="x", view_dir=view_dir)
    assert render_stage_interface(doc, 1, contract=True).rstrip() in bundle
    assert "*(none beyond the first hop)*" in bundle  # nothing transitive beyond stage 1


def test_render_topo_review_bundle_no_reliance_edges_renders_the_none_marker():
    doc = _doc([_stage(1, output_artifacts=["a.txt"])])
    bundle = render_topo_review_bundle(doc, 1, plan_sha256="x", view_dir=Path("/tmp/view-1"))
    assert "*(none — this unit has no declared reliance edges)*" in bundle
    assert "*(none)*" in bundle  # no first-hop neighbour interfaces either


def test_render_topo_review_bundle_order_unit_reports_consumed_by_never_relies_on():
    order = _order(["R1"], {"R1": ["1", "2"]})
    doc = _doc([_stage(1, output_artifacts=["a.txt"]), _stage(2)], order=order)
    bundle = render_topo_review_bundle(doc, "order", plan_sha256="ord1", view_dir=Path("/tmp/view-order"))
    assert "# Topological review unit: order" in bundle
    assert "- consumed-by: stage 1" in bundle
    assert "- consumed-by: stage 2" in bundle
    assert "- relies-on:" not in bundle
    assert "*(none beyond the first hop)*" in bundle
