"""Pure-`PlanDoc`-level tests for the `--review-topo` topological-review unit:
the reliance helpers in `agentctl.plan` (`reliance_set`/`consumers`/`first_hop`/
`reliance_closure`/`interface_empty`/`load_plan_with_digest`) and the rendering/
materialization surface in `agentctl.render` (`render_stage_interface`/
`render_topo_review_bundle`/`topo_unit_view`/`topo_unit_files`/
`materialize_topo_units`/`verify_topo_units`).

Every `PlanDoc` here is built via `agentctl.plan.parse_plan` on a plain dict, except
where the single-read digest contract needs a real plan file on disk. Spawn-level
coverage of the same feature lives in `test_spawn_topo_review.py`; the origin/main
byte-invariance pin lives in `test_topo_invariance.py`.
"""
from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import threading
from pathlib import Path

import pytest

from agentctl import render
from agentctl.plan import (
    CONDITION_MARKERS,
    PLAN_DIGEST_MARKER,
    REVIEW_MARKER,
    VERDICT_MARKER,
    PlanError,
    consumers,
    first_hop,
    interface_empty,
    load_plan_with_digest,
    parse_plan,
    reliance_closure,
    reliance_set,
)
from agentctl.render import (
    TopoUnitsCorrupt,
    materialize_topo_units,
    render_order_md,
    render_plan_md,
    render_stage_brief,
    render_stage_interface,
    render_topo_review_bundle,
    topo_unit_files,
    topo_unit_view,
    topo_unit_view_dirname,
    verify_topo_units,
)

_DECLARED_ONLY = "declared-only (supplies-wins collapse; not dispatch-ordered)"
_FIRST_HOP = "## First-hop neighbours"
_INTERFACES = "## Neighbour interfaces"
_TRANSITIVE = "## Transitive reliances (interface only, not in the view directory)"
_FULL_BRIEFS = "## Full neighbour briefs"
_PROCEDURE = "## Reconciliation procedure"
_PROTOCOL = "## Review protocol"


def _stage(index=1, **overrides):
    base = {
        "index": index, "title": f"Stage {index}", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "dc",
        "means": "Edit", "method": f"method-{index}", "verify_command": f"true-{index}",
    }
    base.update(overrides)
    return base


def _doc(stages, order=None, **meta_overrides):
    meta = {"task_id": "t", **meta_overrides}
    if order is not None:
        meta["order"] = order
    return parse_plan({"meta": meta, "stage": stages})


def _order(requirements, coverage, **extra):
    return {
        "requirements": [{"id": rid, "text": rid} for rid in requirements],
        "coverage": coverage,
        **extra,
    }


def _method_sentinel(index: int) -> str:
    return f"**Method:** method-{index}"


def _between(text: str, start: str, end: str) -> str:
    """The slice of `text` strictly after the line `start` and before the line `end`.
    Section boundaries are matched as whole lines because neighbour interfaces and
    briefs carry their own `## Stage` headings."""
    head = text.split(f"\n{start}\n", 1)[1]
    return head.split(f"\n{end}\n", 1)[0]


def _first_hop_lines(bundle: str) -> list[str]:
    section = _between(bundle, _FIRST_HOP, _INTERFACES)
    return [line for line in section.splitlines() if line.startswith("- ")]


def _bundle(doc, unit, view_dir="/tmp/v", sha="d"):
    return render_topo_review_bundle(doc, unit, plan_sha256=sha, view_dir=view_dir)


def _tree_files(version_root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(version_root).as_posix(): p.read_bytes()
        for p in sorted(version_root.rglob("*"))
        if p.is_file()
    }


def _assert_manifest_matches_files(version_root: Path) -> None:
    files = _tree_files(version_root)
    manifest = json.loads(files.pop("MANIFEST.json"))
    assert manifest == {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def _assert_views_are_copies(version_root: Path) -> None:
    unit_inodes = {p.stat().st_ino for p in version_root.iterdir() if p.is_file()}
    view_files = [p for p in version_root.glob("view-*/*") if p.is_file()]
    assert view_files
    for p in view_files:
        assert p.stat().st_ino not in unit_inodes, p


def _remedy(version_root: Path) -> str:
    return f"delete {version_root} and re-run to re-materialize it"


def _plan_toml(stage1_title: str = "Stage 1", stage1_method: str = "method-1") -> str:
    return f"""
[meta]
task_id = "t"

[[stage]]
index = 1
title = "{stage1_title}"
executor = "in_thread"
expected_result_image = "img"
done_criterion = "dc"
means = "Edit"
method = "{stage1_method}"
verify_command = "true-1"

[[stage]]
index = 2
title = "Stage 2"
executor = "in_thread"
expected_result_image = "img"
done_criterion = "dc"
means = "Edit"
method = "method-2"
verify_command = "true-2"
depends_on = [1]
"""


def test_tb1_stage_unit_bundle_holds_order_own_brief_and_digest():
    doc = _doc([_stage(1)], order=_order(["R1"], {"R1": ["1"]}))
    bundle = _bundle(doc, 1, view_dir="/tmp/view-1", sha="deadbeef")
    assert "\n".join(render_order_md(doc)) in bundle
    own = _between(bundle, "## Own brief", _FIRST_HOP)
    assert render_stage_brief(doc, 1) in own
    assert bundle.splitlines().count(f"{PLAN_DIGEST_MARKER} deadbeef") == 1


def test_tb2_first_hop_list_tags_supplier_and_customer_with_every_edge():
    doc = _doc([
        _stage(1, output_artifacts=["out/a.json"]),
        _stage(2, depends_on=[1], supplies=[
            {"on": 1, "element": "e1"},
            {"on": 1, "element": "e1b", "artifact": "out/a.json"},
        ]),
        _stage(3, depends_on=[2], supplies=[{"on": 2}]),
        _stage(4, depends_on=[2]),
    ])
    assert _first_hop_lines(_bundle(doc, 2)) == [
        "- stage 1 (Stage 1): supplier — supplies `e1`; supplies `e1b` "
        "(artifact: `out/a.json`) — engine-ordered",
        "- stage 3 (Stage 3): customer — supplies whole product — engine-ordered",
        "- stage 4 (Stage 4): customer — supplies whole product — engine-ordered",
    ]


def test_tb3_first_hop_neighbour_interface_only_no_full_brief():
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[1], supplies=[{"on": 1, "element": "e1"}]),
    ])
    bundle = _bundle(doc, 2)
    assert render_stage_interface(doc, 1, contract=True) in _between(bundle, _INTERFACES, _TRANSITIVE)
    assert render_stage_brief(doc, 1) not in bundle
    assert _method_sentinel(1) not in bundle


def test_tb4_transitive_only_member_interface_present_not_in_view():
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[1], supplies=[{"on": 1, "element": "e1"}]),
        _stage(3, depends_on=[2], supplies=[{"on": 2, "element": "e2"}]),
    ])
    bundle = _bundle(doc, 3)
    assert render_stage_interface(doc, 1, contract=True) in _between(bundle, _TRANSITIVE, _FULL_BRIEFS)
    assert "## Stage 1:" not in _between(bundle, _INTERFACES, _TRANSITIVE)
    assert topo_unit_view(doc, 3) == ["stage-2.md"]
    full_briefs = _between(bundle, _FULL_BRIEFS, _PROCEDURE)
    assert "stage-2.md" in full_briefs
    assert "stage-1.md" not in full_briefs
    assert _method_sentinel(1) not in bundle


def test_tb5_interface_empty_neighbour_falls_back_others_stay_interface_only():
    doc = _doc([
        _stage(1, depends_on=[2, 3], supplies=[{"on": 2, "element": "e2"}, {"on": 3, "element": "e3"}]),
        _stage(2, expected_result_image=" "),
        _stage(3, output_artifacts=[]),
    ])
    bundle = _bundle(doc, 1)
    stage2 = next(s for s in doc.stages if s.index == 2)
    assert interface_empty(stage2)
    neighbours = _between(bundle, _INTERFACES, _TRANSITIVE)
    assert render_stage_brief(doc, 2) in neighbours
    assert _method_sentinel(2) in neighbours
    assert _method_sentinel(3) not in bundle
    assert render_stage_interface(doc, 3, contract=True) in neighbours


def _order_unit_doc():
    return parse_plan({
        "meta": {
            "task_id": "t",
            "done_criterion": "DC-SENTINEL",
            "external_research": "ER-SENTINEL",
            "order": _order(["R1"], {"R1": ["2"]}, requires_traceability=True),
        },
        "final_check": [{"command": "true", "expected_exit": 0, "label": "fc1"}],
        "stage": [_stage(1), _stage(2, depends_on=[1])],
    })


def test_tb6_order_unit_carries_meta_order_coverage_and_final_check():
    doc = _order_unit_doc()
    bundle = _bundle(doc, "order")
    lines = bundle.splitlines()
    assert "- **Done criterion:** DC-SENTINEL" in lines
    assert "- **External research:** ER-SENTINEL" in lines
    assert "\n".join(render_order_md(doc)) in bundle
    assert "- fc1: `true` (expected exit 0)" in lines
    plan_md_lines = render_plan_md(doc).splitlines()
    for sentinel in ("  - R1: 2", "- **Requires traceability:** True"):
        assert sentinel in lines
        assert sentinel not in plan_md_lines


def test_tb6_order_unit_neighbours_view_and_joint_conditions():
    doc = _order_unit_doc()
    bundle = _bundle(doc, "order")
    lines = bundle.splitlines()
    assert _first_hop_lines(bundle) == [
        "- stage 1 (Stage 1): supplier — order-node reliance (no declared supply edge) — none",
        "- stage 2 (Stage 2): supplier — order-node reliance (no declared supply edge) — none",
    ]
    neighbours = _between(bundle, _INTERFACES, _TRANSITIVE)
    for n in (1, 2):
        assert render_stage_interface(doc, n, contract=True) in neighbours
    assert topo_unit_view(doc, "order") == ["stage-1.md", "stage-2.md"]
    full_briefs = _between(bundle, _FULL_BRIEFS, _PROCEDURE).splitlines()
    assert "- `/tmp/v/stage-1.md`" in full_briefs
    assert "- `/tmp/v/stage-2.md`" in full_briefs

    joint = " — evaluated jointly over all stages against each Order.coverage requirement, not per-stage"
    conditions = lines[lines.index("Conditions:") + 1:]
    conditions = [c for c in conditions if c]
    assert conditions == [
        "- `C1:` the unit is organized in a non-arbitrary way",
        "- `C2:` the unit is a genuine derivation from the order" + joint,
        "- `C3:` not applicable — the order node delivers no product of its own for a "
        "consumer to rely on",
        "- `C4:` what this unit relies on is declared, completely, precisely, and jointly "
        "with its suppliers — its consumer precondition holds" + joint,
    ]
    assert bundle.count(joint) == 2


def test_tb7_unknown_unit_raises_value_error():
    doc = _doc([_stage(1)])
    with pytest.raises(ValueError):
        _bundle(doc, 99)
    with pytest.raises(ValueError):
        _bundle(doc, "order")


def test_tb8_non_direct_edges_reliance_set_raw_depends_on_and_closure():
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
    assert reliance_closure(doc, 4) == {1, 2, 3}
    assert 1 not in reliance_set(doc, 4)
    bundle = _bundle(doc, 4)
    assert render_stage_interface(doc, 1, contract=True) in _between(bundle, _TRANSITIVE, _FULL_BRIEFS)
    assert "## Stage 1:" not in _between(bundle, _INTERFACES, _TRANSITIVE)


def test_tb10_dangling_raw_edge_raises_planerror():
    # The dangling raw edge hides behind an explicit (valid) supplies list, so
    # parse_plan's own graph validation (derived, supplies-collapsed edges only)
    # never sees it.
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[99, 1], supplies=[{"on": 1}]),
    ])
    with pytest.raises(PlanError):
        reliance_set(doc, 2)


def test_tb10_raw_union_cycle_hidden_behind_acyclic_derived_graph_raises_planerror():
    # Raw depends_on cycle 1 -> 2 -> 3 -> 1; stage 3's supplies point the DERIVED
    # graph at a plain sink instead, so parse-time validation sees an acyclic graph.
    doc = _doc([
        _stage(1, depends_on=[2]),
        _stage(2, depends_on=[3]),
        _stage(3, depends_on=[1], supplies=[{"on": 4}]),
        _stage(4),
    ])
    stage3 = next(s for s in doc.stages if s.index == 3)
    assert list(stage3.depends_on) == [4]
    with pytest.raises(PlanError):
        reliance_closure(doc, 1)


def test_tb10_cycle_reached_through_an_already_visited_node_raises_planerror():
    # 2 <-> 3 is reachable from 1 both directly and via 2; the derived graph
    # (stage 3 supplies on 4) stays acyclic.
    doc = _doc([
        _stage(1, depends_on=[2, 3]),
        _stage(2, depends_on=[3]),
        _stage(3, depends_on=[2], supplies=[{"on": 4}]),
        _stage(4),
    ])
    with pytest.raises(PlanError, match="2 -> 3 -> 2"):
        reliance_closure(doc, 1)


def test_tb10_diamond_is_not_a_cycle():
    doc = _doc([
        _stage(1, depends_on=[2, 3]),
        _stage(2, depends_on=[4]),
        _stage(3, depends_on=[4]),
        _stage(4),
    ])
    assert reliance_closure(doc, 1) == {2, 3, 4}


def test_tb11_digest_and_doc_come_from_a_single_plan_read(tmp_path, monkeypatch):
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text(_plan_toml(stage1_title="Version one"), encoding="utf-8")
    v1_bytes = plan_path.read_bytes()
    reads: list[bytes] = []
    real_read_bytes = Path.read_bytes

    def read_then_edit(self):
        data = real_read_bytes(self)
        if self == plan_path:
            reads.append(data)
            self.write_text(_plan_toml(stage1_title="Version two"), encoding="utf-8")
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_edit)
    doc, data, digest = load_plan_with_digest(plan_path)
    monkeypatch.setattr(Path, "read_bytes", real_read_bytes)

    assert reads == [v1_bytes]
    assert data == v1_bytes
    assert digest == hashlib.sha256(v1_bytes).hexdigest()
    assert digest != hashlib.sha256(plan_path.read_bytes()).hexdigest()
    assert doc.stages[0].title == "Version one"
    bundle = _bundle(doc, 1, sha=digest)
    assert bundle.splitlines().count(f"{PLAN_DIGEST_MARKER} {digest}") == 1


def test_tb12_render_stage_interface_default_never_falls_back():
    doc = _doc([_stage(1, expected_result_image=" ")])
    text = render_stage_interface(doc, 1)
    assert "This is a PROJECTED BRIEF" not in text
    assert "**Expected result image:**" in text


def test_tb13_one_hop_view_holds_only_first_hop_files(tmp_path):
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[1]),
        _stage(3, depends_on=[2]),
    ])
    view = topo_unit_view(doc, 3)
    assert view == ["stage-2.md"]
    files = topo_unit_files(doc)
    assert _method_sentinel(1) not in files["stage-2.md"]

    version_root = materialize_topo_units(doc, "shaV", tmp_path)
    view_dir = version_root / topo_unit_view_dirname(3)
    assert sorted(p.name for p in view_dir.iterdir()) == ["stage-2.md"]
    for p in view_dir.iterdir():
        assert _method_sentinel(1) not in p.read_text(encoding="utf-8")


def test_tb14_materialize_manifest_sha256s_match_the_files(tmp_path):
    doc = _doc(
        [_stage(1), _stage(2, depends_on=[1]), _stage(3, depends_on=[2])],
        order=_order(["R1"], {"R1": ["3"]}),
    )
    version_root = materialize_topo_units(doc, "shaA", tmp_path)
    assert version_root == tmp_path / "shaA"
    _assert_manifest_matches_files(version_root)
    assert sorted(p.name for p in version_root.iterdir() if p.is_dir()) == [
        "view-1", "view-2", "view-3", "view-order",
    ]
    verify_topo_units(version_root, doc)


def test_tb14_materialize_second_call_is_a_no_op_reverify(tmp_path):
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    first = materialize_topo_units(doc, "shaA", tmp_path)
    before = {p: p.stat().st_mtime_ns for p in first.rglob("*")}
    second = materialize_topo_units(doc, "shaA", tmp_path)
    assert second == first
    assert {p: p.stat().st_mtime_ns for p in first.rglob("*")} == before


def test_tb14_edited_plan_materializes_into_a_new_sha_dir(tmp_path):
    plan_path = tmp_path / "plan.toml"
    units = tmp_path / "units"
    plan_path.write_text(_plan_toml(), encoding="utf-8")
    doc1, _, sha1 = load_plan_with_digest(plan_path)
    root1 = materialize_topo_units(doc1, sha1, units)

    plan_path.write_text(_plan_toml(stage1_method="edited-method"), encoding="utf-8")
    doc2, _, sha2 = load_plan_with_digest(plan_path)
    root2 = materialize_topo_units(doc2, sha2, units)

    assert sha1 != sha2
    assert root1 == units / sha1 and root2 == units / sha2
    assert root1.is_dir() and root2.is_dir()
    assert (root1 / "stage-1.md").read_bytes() != (root2 / "stage-1.md").read_bytes()
    verify_topo_units(root1, doc1)
    with pytest.raises(TopoUnitsCorrupt):
        verify_topo_units(root1, doc2)


def test_tb15_bundle_carries_procedure_and_view_path_no_valve_token():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    bundle = _bundle(doc, 2, view_dir="/tmp/my-view-dir")
    assert "1. Take the first-hop pairs listed above one at a time, in the listed order." in bundle
    assert "`/tmp/my-view-dir`" in bundle
    assert "- `/tmp/my-view-dir/stage-1.md`" in bundle.splitlines()
    assert "escalation" not in bundle.lower()
    assert "valve" not in bundle.lower()


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


def test_tb17_derived_depends_on_projection_misses_the_raw_only_edge():
    doc = _tb16_doc()
    projection = {s.index: tuple(s.depends_on) for s in doc.stages}
    doc.raw_depends_on.clear()
    doc.raw_depends_on.update(projection)
    assert reliance_set(doc, 3) == {1}
    assert 2 not in first_hop(doc, 3)


def test_tb18_missing_raw_depends_on_entry_raises_never_silently_falls_back():
    doc = _doc([_stage(1), _stage(2), _stage(3, depends_on=[1])])
    del doc.raw_depends_on[3]
    with pytest.raises(PlanError):
        reliance_set(doc, 3)
    with pytest.raises(PlanError):
        first_hop(doc, 3)
    with pytest.raises(PlanError):
        reliance_closure(doc, 3)


def test_tb19_raw_depends_on_has_an_entry_for_every_stage_including_empty():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    assert doc.raw_depends_on[1] == ()
    assert doc.raw_depends_on[2] == (1,)


def _race_doc():
    return _doc(
        [_stage(1), _stage(2, depends_on=[1]), _stage(3, depends_on=[1, 2])],
        order=_order(["R1"], {"R1": ["3"]}),
    )


def test_tb20_forced_race_loser_reverifies_winner_and_removes_its_temp(tmp_path, monkeypatch):
    doc = _race_doc()
    version_root = tmp_path / "shaR"
    real_replace = os.replace
    real_verify = render.verify_topo_units
    verify_calls: list[Path] = []
    raised: list[OSError] = []
    loser_tmp: list[Path] = []

    def spy_verify(unit_dir, d):
        verify_calls.append(Path(unit_dir))
        return real_verify(unit_dir, d)

    def replace_after_a_real_winner(src, dst):
        if loser_tmp:
            return real_replace(src, dst)
        loser_tmp.append(Path(src))
        assert materialize_topo_units(doc, "shaR", tmp_path) == version_root
        try:
            return real_replace(src, dst)
        except OSError as exc:
            raised.append(exc)
            raise

    monkeypatch.setattr(render, "verify_topo_units", spy_verify)
    monkeypatch.setattr(render.os, "replace", replace_after_a_real_winner)

    result = materialize_topo_units(doc, "shaR", tmp_path)

    assert result == version_root
    assert len(raised) == 1
    assert raised[0].errno in (errno.ENOTEMPTY, errno.EEXIST)
    assert not loser_tmp[0].exists()
    assert list(tmp_path.iterdir()) == [version_root]
    assert verify_calls == [version_root]
    _assert_manifest_matches_files(version_root)
    _assert_views_are_copies(version_root)


def test_tb20_non_race_oserror_propagates_and_removes_temp(tmp_path, monkeypatch):
    doc = _race_doc()

    def replace_with_unrelated_error(src, dst):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(render.os, "replace", replace_with_unrelated_error)

    with pytest.raises(OSError) as exc_info:
        materialize_topo_units(doc, "shaX", tmp_path)

    assert exc_info.value.errno == errno.EXDEV
    assert list(tmp_path.iterdir()) == []


def test_tb20_concurrent_threads_all_return_one_verified_tree(tmp_path):
    doc = _race_doc()
    workers = 6
    barrier = threading.Barrier(workers)
    results: list[Path] = []
    errors: list[BaseException] = []

    def run():
        barrier.wait()
        try:
            results.append(materialize_topo_units(doc, "shaT", tmp_path))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert results == [tmp_path / "shaT"] * workers
    assert list(tmp_path.iterdir()) == [tmp_path / "shaT"]
    _assert_manifest_matches_files(tmp_path / "shaT")
    _assert_views_are_copies(tmp_path / "shaT")


def test_tb20_modified_view_file_fails_rematerialize_with_remedy(tmp_path):
    doc = _race_doc()
    version_root = materialize_topo_units(doc, "shaC", tmp_path)
    victim = version_root / topo_unit_view_dirname(2) / "stage-1.md"
    victim.write_text("tampered", encoding="utf-8")
    with pytest.raises(TopoUnitsCorrupt) as exc_info:
        materialize_topo_units(doc, "shaC", tmp_path)
    assert str(victim) in str(exc_info.value)
    assert _remedy(version_root) in str(exc_info.value)


def test_tb20_extra_file_in_view_dir_fails_rematerialize_with_remedy(tmp_path):
    doc = _race_doc()
    version_root = materialize_topo_units(doc, "shaF", tmp_path)
    extra = version_root / topo_unit_view_dirname(2) / "extra.md"
    extra.write_text("x", encoding="utf-8")
    with pytest.raises(TopoUnitsCorrupt) as exc_info:
        materialize_topo_units(doc, "shaF", tmp_path)
    assert str(extra) in str(exc_info.value)
    assert _remedy(version_root) in str(exc_info.value)


def test_tb20_extra_file_in_unit_root_fails_rematerialize_with_remedy(tmp_path):
    doc = _race_doc()
    version_root = materialize_topo_units(doc, "shaE", tmp_path)
    extra = version_root / "extra.md"
    extra.write_text("x", encoding="utf-8")
    with pytest.raises(TopoUnitsCorrupt) as exc_info:
        materialize_topo_units(doc, "shaE", tmp_path)
    assert str(extra) in str(exc_info.value)
    assert _remedy(version_root) in str(exc_info.value)


def test_tb21_ordering_tag_supplier_and_customer_lines():
    doc = _tb16_doc()
    assert _first_hop_lines(_bundle(doc, 3)) == [
        "- stage 1 (Stage 1): supplier — supplies whole product — engine-ordered",
        f"- stage 2 (Stage 2): supplier — depends_on-only — {_DECLARED_ONLY}",
    ]
    assert _first_hop_lines(_bundle(doc, 2)) == [
        f"- stage 3 (Stage 3): customer — depends_on-only — {_DECLARED_ONLY}",
    ]


def test_tb21_ordering_tag_flips_when_transitively_engine_ordered():
    doc = _doc([
        _stage(1, depends_on=[2]),
        _stage(2),
        _stage(3, depends_on=[1, 2], supplies=[{"on": 1}]),
    ])
    assert _first_hop_lines(_bundle(doc, 3)) == [
        "- stage 1 (Stage 1): supplier — supplies whole product — engine-ordered",
        "- stage 2 (Stage 2): supplier — depends_on-only — engine-ordered",
    ]
    assert _first_hop_lines(_bundle(doc, 2)) == [
        "- stage 1 (Stage 1): customer — supplies whole product — engine-ordered",
        "- stage 3 (Stage 3): customer — depends_on-only — engine-ordered",
    ]


def test_tb22_protocol_constants_present_and_not_duplicated_in_render_module():
    doc = _doc([_stage(1)])
    bundle = _bundle(doc, 1)
    assert REVIEW_MARKER in bundle
    assert VERDICT_MARKER in bundle
    assert PLAN_DIGEST_MARKER in bundle
    for marker in CONDITION_MARKERS:
        assert marker in bundle

    source = inspect.getsource(render)
    for literal in (REVIEW_MARKER, VERDICT_MARKER, PLAN_DIGEST_MARKER, *CONDITION_MARKERS):
        assert f'"{literal}"' not in source
        assert f"'{literal}'" not in source


def test_tb22_protocol_text_pins_conditions_echo_concerns_and_pairwise_step():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    bundle = _bundle(doc, 2, sha="abc123")
    lines = bundle.splitlines()

    protocol = _between(bundle, _PROTOCOL, "Conditions:").splitlines()
    assert [line for line in protocol if line.startswith("- ")] == [
        "- `REVIEW:` on a line of its own;",
        "- `Verdict: <pass|revise>`;",
        "- `Plan digest: <sha256>` — echo the `Plan digest:` line above verbatim; "
        "do not compute it;",
        "- one concern per line, each prefixed by the marker of the condition it "
        "concerns (`C1:`, `C2:`, `C3:`, `C4:`); a condition-4 gap is a `C4:` line.",
    ]
    assert "- `C1:` the unit is organized in a non-arbitrary way" in lines
    assert "- `C2:` the unit is a genuine derivation from the order" in lines

    assert "- `/tmp/v/stage-1.md`" in _between(bundle, _FULL_BRIEFS, _PROCEDURE).splitlines()

    procedure = _between(bundle, _PROCEDURE, f"{PLAN_DIGEST_MARKER} abc123").splitlines()
    assert "1. Take the first-hop pairs listed above one at a time, in the listed order." in procedure
    assert "4. Report any gap a pair reveals as a condition-4 concern: one `C4:` line." in procedure
