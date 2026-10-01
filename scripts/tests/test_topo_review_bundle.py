"""Pure-`PlanDoc`-level tests for the `--review-topo` base-service pair review:
the pair helpers in `agentctl.plan` (`plan_reliance_set`/`review_pairs`/
`parse_pair`/`pair_binding`, and the reliance helpers they read) and the
rendering/materialization surface in `agentctl.render`
(`render_pair_review_bundle`/`topo_pair_view`/`topo_node_files`/
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
    pair_binding,
    parse_pair,
    parse_plan,
    plan_reliance_set,
    reliance_closure,
    reliance_set,
    review_pairs,
)
from agentctl.render import (
    TopoUnitsCorrupt,
    materialize_topo_units,
    node_file_text,
    render_order_md,
    render_pair_review_bundle,
    render_plan_interface,
    render_plan_md,
    render_stage_brief,
    render_stage_interface,
    topo_node_files,
    topo_pair_view,
    topo_pair_view_dirname,
    verify_topo_units,
)

_DECLARED_ONLY = "declared-only (supplies-wins collapse; not dispatch-ordered)"
_SECTION_HEADINGS = (
    "## Order context",
    "## Base: ",
    "## Service declared product: ",
    "## Edge",
    "## Service file",
    "## Per-pair procedure",
    "## Review protocol",
)


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


def _sections(bundle: str) -> dict[str, str]:
    """The bundle split at its own section headings (matched by line prefix,
    since inlined briefs and interfaces carry `## Stage` headings of their
    own), keyed by the heading's prefix from `_SECTION_HEADINGS`."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in bundle.splitlines():
        matched = next((h for h in _SECTION_HEADINGS if line.startswith(h)), None)
        if matched is not None:
            current = matched
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(body).strip("\n") for name, body in sections.items()}


def _edge_lines(bundle: str) -> list[str]:
    return [line for line in _sections(bundle)["## Edge"].splitlines() if line]


def _conditions(bundle: str) -> list[str]:
    lines = bundle.splitlines()
    return [line for line in lines[lines.index("Conditions:") + 1:] if line]


def _bundle(doc, pair, view_dir="/tmp/v", sha="d"):
    return render_pair_review_bundle(doc, pair, plan_sha256=sha, view_dir=view_dir)


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
    node_inodes = {p.stat().st_ino for p in version_root.iterdir() if p.is_file()}
    view_files = [p for p in version_root.glob("view-*/*") if p.is_file()]
    assert view_files
    for p in view_files:
        assert p.stat().st_ino not in node_inodes, p


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


def _order_doc():
    return parse_plan({
        "meta": {
            "task_id": "t",
            "done_criterion": "DC-SENTINEL",
            "external_research": "ER-SENTINEL",
            "order": _order(
                ["R1"],
                {"R1": ["stage 1 verify_command", "stage 2 verify_command"]},
                requires_traceability=True,
            ),
        },
        "final_check": [{"command": "true", "expected_exit": 0, "label": "fc1"}],
        "stage": [_stage(1), _stage(2, depends_on=[1])],
    })


def test_tb1_stage_pair_bundle_holds_order_base_brief_and_digest():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])], order=_order(["R1"], {"R1": ["stage 2 verify_command"]}))
    bundle = _bundle(doc, "2-1", view_dir="/tmp/view-2-1", sha="deadbeef")
    sections = _sections(bundle)
    assert "\n".join(render_order_md(doc)).rstrip("\n") in sections["## Order context"]
    assert render_stage_brief(doc, 2).rstrip("\n") in sections["## Base: "]
    assert bundle.splitlines().count(f"{PLAN_DIGEST_MARKER} deadbeef") == 1
    assert bundle.startswith("# Topological review pair: 2-1\n")


def test_tb1_third_stage_adjacent_to_neither_endpoint_is_absent():
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[1]),
        _stage(3, title="Unrelated third stage", depends_on=[]),
    ])
    bundle = _bundle(doc, "2-1")
    assert "Unrelated third stage" not in bundle
    assert _method_sentinel(3) not in bundle
    assert "stage-3.md" not in bundle


def test_tb1_checklist_states_c3_as_the_bases_full_delivery():
    doc = _doc(
        [_stage(1), _stage(2, depends_on=[1])],
        order=_order(["R1"], {"R1": ["stage 2 verify_command"]}),
    )
    stage_c3 = _conditions(_bundle(doc, "2-1"))[2]
    assert "delivers its FULL declared product" in stage_c3
    assert "the part that depends on stage 1 (Stage 1)'s product measured against it" in stage_c3
    assert "the rest standing on its own" in stage_c3

    plan_c3 = _conditions(_bundle(doc, "plan-2"))[2]
    assert "delivers its goal and done criterion" in plan_c3
    assert "the rest (coverage map, final checks) standing on its own from the plan file" in plan_c3
    assert "as far as that rests on this edge" not in plan_c3


def test_tb2_edge_section_names_the_edge_the_reliance_set_and_the_ordering():
    doc = _doc([
        _stage(1, output_artifacts=["out/a.json"]),
        _stage(2, depends_on=[1], supplies=[
            {"on": 1, "element": "e1"},
            {"on": 1, "element": "e1b", "artifact": "out/a.json"},
        ]),
        _stage(3, depends_on=[2], supplies=[{"on": 2}]),
    ])
    assert _edge_lines(_bundle(doc, "2-1")) == [
        "- Edge: stage 2 relies on stage 1 — supplies `e1`; supplies `e1b` (artifact: `out/a.json`)",
        "- Reliance set of stage 2: stage 1 (supplies)",
        "- Ordering: engine-ordered",
    ]
    assert _edge_lines(_bundle(doc, "3-2")) == [
        "- Edge: stage 3 relies on stage 2 — supplies whole product",
        "- Reliance set of stage 3: stage 2 (supplies)",
        "- Ordering: engine-ordered",
    ]


def test_tb3_non_source_service_shows_interface_only_no_full_brief():
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[1], supplies=[{"on": 1, "element": "e1"}]),
        _stage(3, depends_on=[2], supplies=[{"on": 2}]),
    ])
    bundle = _bundle(doc, "3-2")
    service = _sections(bundle)["## Service declared product: "]
    assert render_stage_interface(doc, 2, contract=True).rstrip("\n") in service
    assert render_stage_brief(doc, 2) not in bundle
    assert _method_sentinel(2) not in bundle
    assert _method_sentinel(3) in bundle


def test_tb3_pair_binding_digests_are_the_sha256_of_the_bytes_the_reviewer_sees(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_TOPO_UNITS_DIR", str(tmp_path))
    doc = _doc(
        [
            _stage(1),
            _stage(2, depends_on=[1], supplies=[{"on": 1, "element": "e1"}]),
            _stage(3, depends_on=[2], supplies=[{"on": 2}]),
        ],
        order=_order(["R1"], {"R1": ["stage 3 verify_command"]}),
    )
    sha = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
    version_root = materialize_topo_units(doc, "plansha", tmp_path)
    for pair, base_file, service_file, service_text in (
        ("3-2", "stage-3.md", "stage-2.md", render_stage_interface(doc, 2, contract=True)),
        # Stage 1 relies on nothing, so it is shown by its full brief.
        ("2-1", "stage-2.md", "stage-1.md", render_stage_brief(doc, 1)),
        ("plan-3", "plan.md", "stage-3.md", render_stage_interface(doc, 3, contract=True)),
        ("base-plan", "base.md", "plan.md", render_plan_interface(doc)),
    ):
        sections = _sections(_bundle(doc, pair))
        binding = pair_binding(doc, pair)
        assert binding["context_digest"] == sha((version_root / "base.md").read_text(encoding="utf-8"))
        assert binding["base_file_digest"] == sha((version_root / base_file).read_text(encoding="utf-8"))
        assert binding["service_file_digest"] == sha((version_root / service_file).read_text(encoding="utf-8"))
        assert service_text.rstrip("\n") in sections["## Service declared product: "]
        assert binding["service_interface_digest"] == sha(service_text)
        assert binding["edge_digest"] == sha(sections["## Edge"] + "\n")


def test_tb3_source_service_shows_its_full_brief_with_a_note():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    service = _sections(_bundle(doc, "2-1"))["## Service declared product: "]
    assert render_stage_brief(doc, 1).rstrip("\n") in service
    assert "relies on nothing" in service


def test_tb4_transitive_only_member_is_absent_from_bundle_and_view():
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[1], supplies=[{"on": 1, "element": "e1"}]),
        _stage(3, depends_on=[2], supplies=[{"on": 2, "element": "e2"}]),
    ])
    bundle = _bundle(doc, "3-2")
    assert topo_pair_view(doc, "3-2") == ["stage-2.md"]
    assert "stage-2.md" in _sections(bundle)["## Service file"]
    assert "stage-1.md" not in bundle
    assert "## Stage 1:" not in bundle
    assert _method_sentinel(1) not in bundle


def test_tb5_interface_empty_service_falls_back_others_stay_interface_only():
    doc = _doc([
        _stage(1, depends_on=[2, 3], supplies=[{"on": 2, "element": "e2"}, {"on": 3, "element": "e3"}]),
        _stage(2, depends_on=[4], expected_result_image=" "),
        _stage(3, depends_on=[4], output_artifacts=[]),
        _stage(4),
    ])
    assert interface_empty(next(s for s in doc.stages if s.index == 2))
    fallback = _bundle(doc, "1-2")
    assert render_stage_brief(doc, 2).rstrip("\n") in _sections(fallback)["## Service declared product: "]
    assert _method_sentinel(2) in fallback
    interface_only = _bundle(doc, "1-3")
    assert render_stage_interface(doc, 3, contract=True).rstrip("\n") in _sections(interface_only)["## Service declared product: "]
    assert _method_sentinel(3) not in interface_only


def test_tb6_plan_base_pair_carries_meta_coverage_and_final_check():
    doc = _order_doc()
    bundle = _bundle(doc, "plan-2")
    lines = bundle.splitlines()
    base = _sections(bundle)["## Base: "]
    assert base.rstrip("\n") == node_file_text(doc, "plan").rstrip("\n")
    for sentinel in (
        "- **Done criterion:** DC-SENTINEL",
        "- **External research:** ER-SENTINEL",
        "- fc1: `true` (expected exit 0)",
        "  - R1: stage 1 verify_command, stage 2 verify_command",
        "- **Requires traceability:** True",
    ):
        assert sentinel in base.splitlines()
    assert "\n".join(render_order_md(doc)).rstrip("\n") in _sections(bundle)["## Order context"]
    assert _edge_lines(bundle) == [
        "- Edge: the plan as a whole relies on stage 2 — covers R1; sink",
        "- Ordering: none",
    ]
    assert _edge_lines(_bundle(doc, "plan-1")) == [
        "- Edge: the plan as a whole relies on stage 1 — covers R1",
        "- Ordering: none",
    ]
    assert lines.count(f"{PLAN_DIGEST_MARKER} d") == 1


def test_tb6_base_plan_pair_inlines_the_base_twice_and_marks_c3_not_applicable():
    doc = _order_doc()
    bundle = _bundle(doc, "base-plan")
    sections = _sections(bundle)
    order_text = node_file_text(doc, "base").rstrip("\n")
    assert order_text in sections["## Order context"]
    assert sections["## Base: "].rstrip("\n") == order_text
    assert "same text" in sections["## Order context"]
    assert render_plan_interface(doc).rstrip("\n") in sections["## Service declared product: "]
    assert "- `/tmp/v/plan.md`" in sections["## Service file"].splitlines()
    assert _edge_lines(bundle) == [
        "- Edge: the base activity relies on the plan as a whole — order",
        "- Ordering: none",
    ]
    assert _conditions(bundle) == [
        "- `C1:` the base is organized in a non-arbitrary way",
        "- `C2:` the requirements are genuinely derived from the functional place",
        "- `C3:` not applicable — the base activity delivers no product of its own for a "
        "consumer to rely on",
        "- `C4:` the goal and done criterion answer every requirement",
    ]


def test_tb6_plan_stage_pair_conditions_read_the_coverage_map_and_the_plan_delivery():
    doc = _order_doc()
    assert _conditions(_bundle(doc, "plan-2")) == [
        "- `C1:` the coverage map is total and non-arbitrary",
        "- `C2:` the plan as a whole is a genuine derivation from the order through this edge",
        "- `C3:` the plan as a whole delivers its goal and done criterion — the part "
        "attributed to stage 2 (Stage 2) measured against that stage's declared product, "
        "the rest (coverage map, final checks) standing on its own from the plan file",
        "- `C4:` stage 2 (Stage 2)'s declared product decides the requirements the "
        "coverage map attributes to it",
    ]


def test_tb6_synthetic_node_files_hold_no_stage_content_and_coverage_is_new_to_plan_md():
    doc = _doc(
        [_stage(1), _stage(2, depends_on=[1])],
        order=_order(
            ["R1"],
            {"R1": ["stage 1 verify_command", "stage 2 verify_command"]},
            requires_traceability=True,
            customer="CUSTOMER-SENTINEL",
            customer_id="cust-id",
            functional_place="PLACE-SENTINEL",
        ),
    )
    base_md = node_file_text(doc, "base")
    plan_md = node_file_text(doc, "plan")
    assert "CUSTOMER-SENTINEL" in base_md
    assert "PLACE-SENTINEL" in base_md
    assert "**R1**" in base_md
    for stage_content in (_method_sentinel(1), _method_sentinel(2), "Stage 1", "Stage 2", "Expected result image"):
        assert stage_content not in base_md
        assert stage_content not in plan_md
    assert "## Stage" not in plan_md
    assert "R1: stage 1 verify_command, stage 2 verify_command" in plan_md
    assert "- **Requires traceability:** True" in plan_md
    whole_plan = render_plan_md(doc)
    assert "R1: stage 1 verify_command, stage 2 verify_command" not in whole_plan
    assert "Requires traceability" not in whole_plan


def test_tb6_stage_pair_conditions_name_both_nodes_and_never_mark_c3_not_applicable():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    conditions = _conditions(_bundle(doc, "2-1"))
    assert conditions == [
        "- `C1:` every need of stage 2 (Stage 2) is attributed to a declared edge — it is "
        "organized in a non-arbitrary way",
        "- `C2:` stage 2 (Stage 2) is a genuine derivation from the order through this edge",
        "- `C3:` stage 2 (Stage 2) delivers its FULL declared product — the part that "
        "depends on stage 1 (Stage 1)'s product measured against it, the rest standing on "
        "its own",
        "- `C4:` stage 1 (Stage 1)'s declared product covers the part of stage 2 (Stage 2) "
        "attributed to this edge",
    ]


def test_tb7_unknown_pair_and_unit_form_ids_raise_value_error():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    for pair in ("2-1", "plan-2"):
        _bundle(doc, pair)
    for refused in ("1-2", "plan-1", "99-1", "base-plan", "2", "order", "1", "", "2-"):
        with pytest.raises(ValueError):
            _bundle(doc, refused)
        with pytest.raises(ValueError):
            parse_pair(doc, refused)


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
    assert [p for p in review_pairs(doc) if p.startswith("4-")] == ["4-2", "4-3"]
    bundle = _bundle(doc, "4-3")
    assert "## Stage 1:" not in bundle
    assert _method_sentinel(1) not in bundle


def test_tb8_review_pairs_order_and_plan_reliance_set():
    doc = parse_plan({
        "meta": {
            "task_id": "t",
            "order": _order(["R1"], {"R1": ["stage 1 verify_command", "final_check 1"]}),
        },
        "stage": [_stage(1), _stage(2, depends_on=[1]), _stage(3, depends_on=[1, 2])],
    })
    assert plan_reliance_set(doc) == {1, 3}
    assert review_pairs(doc) == ("base-plan", "plan-1", "plan-3", "2-1", "3-1", "3-2")
    assert parse_pair(doc, "3-2") == (3, 2)
    assert parse_pair(doc, "plan-3") == ("plan", 3)
    assert parse_pair(doc, "base-plan") == ("base", "plan")


def test_tb8_plan_without_an_order_has_no_base_plan_pair_and_no_base_file():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    assert review_pairs(doc) == ("plan-2", "2-1")
    assert "base.md" not in topo_node_files(doc)
    with pytest.raises(ValueError):
        parse_pair(doc, "base-plan")


def test_tb8_pair_binding_has_seven_digests_and_base_plan_context_identity():
    doc = _order_doc()
    binding = pair_binding(doc, "base-plan")
    assert sorted(binding) == [
        "base_file_digest", "base_key", "context_digest", "edge_digest",
        "service_file_digest", "service_interface_digest", "service_key",
    ]
    assert binding["context_digest"] == binding["base_key"] == binding["base_file_digest"]
    assert binding["context_digest"] == hashlib.sha256(node_file_text(doc, "base").encode("utf-8")).hexdigest()
    stage_binding = pair_binding(doc, "2-1")
    assert stage_binding["context_digest"] == binding["context_digest"]
    assert stage_binding["base_key"] != stage_binding["service_key"]


def test_tb8_pair_binding_moves_with_the_part_of_the_plan_each_digest_covers():
    def doc_with(stage1_method="method-1", stage2_method="method-2", stage2_image="img", requirement="R1"):
        return parse_plan({
            "meta": {"task_id": "t", "order": _order([requirement], {requirement: ["stage 2 verify_command"]})},
            "stage": [
                _stage(1, method=stage1_method),
                _stage(2, method=stage2_method, expected_result_image=stage2_image, depends_on=[1]),
            ],
        })

    reference = pair_binding(doc_with(), "2-1")
    assert pair_binding(doc_with(), "2-1") == reference

    service_edit = pair_binding(doc_with(stage1_method="edited"), "2-1")
    assert service_edit["service_file_digest"] != reference["service_file_digest"]
    assert service_edit["base_file_digest"] == reference["base_file_digest"]

    base_edit = pair_binding(doc_with(stage2_method="edited"), "2-1")
    assert base_edit["base_file_digest"] != reference["base_file_digest"]
    assert base_edit["service_file_digest"] == reference["service_file_digest"]

    base_interface_edit = pair_binding(doc_with(stage2_image="other"), "2-1")
    assert base_interface_edit["base_key"] != reference["base_key"]

    order_edit = pair_binding(doc_with(requirement="R2"), "2-1")
    assert order_edit["context_digest"] != reference["context_digest"]


def test_tb8_removing_a_raw_only_edge_moves_edge_digest_and_nothing_else():
    def doc_with(raw_depends_on):
        return _doc([
            _stage(1),
            _stage(2, depends_on=raw_depends_on, supplies=[{"on": 1, "element": "e1"}]),
            _stage(3),
        ])

    with_edge = doc_with([1, 3])
    without_edge = doc_with([1])
    assert reliance_set(with_edge, 2) == {1, 3}
    assert reliance_set(without_edge, 2) == {1}
    # The supplies-derived edges, hence every node and interface digest, are untouched.
    assert [s.depends_on for s in with_edge.stages] == [s.depends_on for s in without_edge.stages]

    before = pair_binding(with_edge, "2-1")
    after = pair_binding(without_edge, "2-1")
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"edge_digest"}


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
    with pytest.raises(PlanError):
        review_pairs(doc)


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
    # Every pair-level entry point refuses the plan; none reaches a closure of its own.
    with pytest.raises(PlanError, match="reliance cycle"):
        review_pairs(doc)
    with pytest.raises(PlanError, match="reliance cycle"):
        parse_pair(doc, "1-2")
    with pytest.raises(PlanError, match="reliance cycle"):
        _bundle(doc, "1-2")
    with pytest.raises(PlanError, match="reliance cycle"):
        pair_binding(doc, "1-2")


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
    bundle = _bundle(doc, "2-1", sha=digest)
    assert bundle.splitlines().count(f"{PLAN_DIGEST_MARKER} {digest}") == 1


def test_tb12_render_stage_interface_default_never_falls_back():
    doc = _doc([_stage(1, expected_result_image=" ")])
    text = render_stage_interface(doc, 1)
    assert "This is a PROJECTED BRIEF" not in text
    assert "**Expected result image:**" in text


def test_tb13_one_pair_view_holds_only_the_service_file_copy(tmp_path):
    doc = _doc([
        _stage(1),
        _stage(2, depends_on=[1]),
        _stage(3, depends_on=[2]),
    ])
    assert topo_pair_view(doc, "3-2") == ["stage-2.md"]
    files = topo_node_files(doc)
    assert _method_sentinel(1) not in files["stage-2.md"]

    version_root = materialize_topo_units(doc, "shaV", tmp_path)
    view_dir = version_root / topo_pair_view_dirname("3-2")
    assert sorted(p.name for p in view_dir.iterdir()) == ["stage-2.md"]
    assert (view_dir / "stage-2.md").read_bytes() == node_file_text(doc, 2).encode("utf-8")
    assert _method_sentinel(1) not in (view_dir / "stage-2.md").read_text(encoding="utf-8")


def test_tb13_node_files_are_stage_plan_and_base_when_an_order_is_declared():
    doc = _order_doc()
    assert sorted(topo_node_files(doc)) == ["base.md", "plan.md", "stage-1.md", "stage-2.md"]
    assert topo_node_files(doc)["plan.md"] == node_file_text(doc, "plan")
    assert topo_node_files(doc)["base.md"] == node_file_text(doc, "base")
    assert topo_pair_view(doc, "base-plan") == ["plan.md"]
    assert topo_pair_view(doc, "plan-1") == ["stage-1.md"]


def test_tb14_materialize_manifest_sha256s_match_the_files(tmp_path):
    doc = _doc(
        [_stage(1), _stage(2, depends_on=[1]), _stage(3, depends_on=[2])],
        order=_order(["R1"], {"R1": ["stage 3 verify_command"]}),
    )
    version_root = materialize_topo_units(doc, "shaA", tmp_path)
    assert version_root == tmp_path / "shaA"
    _assert_manifest_matches_files(version_root)
    assert sorted(p.name for p in version_root.iterdir() if p.is_dir()) == [
        "view-2-1", "view-3-2", "view-base-plan", "view-plan-3",
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
    bundle = _bundle(doc, "2-1", view_dir="/tmp/my-view-dir")
    service_file = _sections(bundle)["## Service file"]
    assert "`/tmp/my-view-dir`" in service_file
    assert "- `/tmp/my-view-dir/stage-1.md`" in service_file.splitlines()
    procedure = _sections(bundle)["## Per-pair procedure"].splitlines()
    assert any(line.startswith("1. Decide `C4:`") for line in procedure)
    assert "escalation" not in bundle.lower()
    assert "valve" not in bundle.lower()


def _tb16_doc():
    return _doc([
        _stage(1),
        _stage(2),
        _stage(3, depends_on=[1, 2], supplies=[{"on": 1}]),
    ])


def test_tb16_raw_only_edge_reliance_set_first_hop_consumers_pairs_view():
    doc = _tb16_doc()
    stage3 = next(s for s in doc.stages if s.index == 3)
    assert list(stage3.depends_on) == [1]
    assert reliance_set(doc, 3) == {1, 2}
    assert 2 in first_hop(doc, 3)
    assert 3 in consumers(doc, 2)
    assert "3-2" in review_pairs(doc)
    assert topo_pair_view(doc, "3-2") == ["stage-2.md"]


def test_tb17_derived_depends_on_projection_misses_the_raw_only_edge():
    doc = _tb16_doc()
    projection = {s.index: tuple(s.depends_on) for s in doc.stages}
    doc.raw_depends_on.clear()
    doc.raw_depends_on.update(projection)
    assert reliance_set(doc, 3) == {1}
    assert 2 not in first_hop(doc, 3)
    assert "3-2" not in review_pairs(doc)


def test_tb18_missing_raw_depends_on_entry_raises_never_silently_falls_back():
    doc = _doc([_stage(1), _stage(2), _stage(3, depends_on=[1])])
    del doc.raw_depends_on[3]
    with pytest.raises(PlanError):
        reliance_set(doc, 3)
    with pytest.raises(PlanError):
        first_hop(doc, 3)
    with pytest.raises(PlanError):
        reliance_closure(doc, 3)
    with pytest.raises(PlanError):
        review_pairs(doc)


def test_tb19_raw_depends_on_has_an_entry_for_every_stage_including_empty():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    assert doc.raw_depends_on[1] == ()
    assert doc.raw_depends_on[2] == (1,)


def _race_doc():
    return _doc(
        [_stage(1), _stage(2, depends_on=[1]), _stage(3, depends_on=[1, 2])],
        order=_order(["R1"], {"R1": ["stage 3 verify_command"]}),
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
    victim = version_root / topo_pair_view_dirname("2-1") / "stage-1.md"
    victim.write_text("tampered", encoding="utf-8")
    with pytest.raises(TopoUnitsCorrupt) as exc_info:
        materialize_topo_units(doc, "shaC", tmp_path)
    assert str(victim) in str(exc_info.value)
    assert _remedy(version_root) in str(exc_info.value)


def test_tb20_extra_file_in_view_dir_fails_rematerialize_with_remedy(tmp_path):
    doc = _race_doc()
    version_root = materialize_topo_units(doc, "shaF", tmp_path)
    extra = version_root / topo_pair_view_dirname("2-1") / "extra.md"
    extra.write_text("x", encoding="utf-8")
    with pytest.raises(TopoUnitsCorrupt) as exc_info:
        materialize_topo_units(doc, "shaF", tmp_path)
    assert str(extra) in str(exc_info.value)
    assert _remedy(version_root) in str(exc_info.value)


def test_tb20_extra_file_in_version_root_fails_rematerialize_with_remedy(tmp_path):
    doc = _race_doc()
    version_root = materialize_topo_units(doc, "shaE", tmp_path)
    extra = version_root / "extra.md"
    extra.write_text("x", encoding="utf-8")
    with pytest.raises(TopoUnitsCorrupt) as exc_info:
        materialize_topo_units(doc, "shaE", tmp_path)
    assert str(extra) in str(exc_info.value)
    assert _remedy(version_root) in str(exc_info.value)


def test_tb21_ordering_tag_lines_per_pair():
    doc = _tb16_doc()
    assert _edge_lines(_bundle(doc, "3-1")) == [
        "- Edge: stage 3 relies on stage 1 — supplies whole product",
        "- Reliance set of stage 3: stage 1 (supplies), stage 2 (depends_on-only)",
        "- Ordering: engine-ordered",
    ]
    assert _edge_lines(_bundle(doc, "3-2")) == [
        "- Edge: stage 3 relies on stage 2 — depends_on-only",
        "- Reliance set of stage 3: stage 1 (supplies), stage 2 (depends_on-only)",
        f"- Ordering: {_DECLARED_ONLY}",
    ]


def test_tb21_ordering_tag_flips_when_transitively_engine_ordered():
    doc = _doc([
        _stage(1, depends_on=[2]),
        _stage(2),
        _stage(3, depends_on=[1, 2], supplies=[{"on": 1}]),
    ])
    assert _edge_lines(_bundle(doc, "3-1"))[-1] == "- Ordering: engine-ordered"
    assert _edge_lines(_bundle(doc, "3-2"))[-1] == "- Ordering: engine-ordered"


def test_tb22_protocol_constants_present_and_not_duplicated_in_render_module():
    doc = _doc([_stage(1)])
    bundle = _bundle(doc, "plan-1")
    assert REVIEW_MARKER in bundle
    assert VERDICT_MARKER in bundle
    assert PLAN_DIGEST_MARKER in bundle
    for marker in CONDITION_MARKERS:
        assert marker in bundle

    source = inspect.getsource(render)
    for literal in (REVIEW_MARKER, VERDICT_MARKER, PLAN_DIGEST_MARKER, *CONDITION_MARKERS):
        assert f'"{literal}"' not in source
        assert f"'{literal}'" not in source


def test_tb22_protocol_text_pins_markers_echo_concerns_and_per_pair_steps():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    bundle = _bundle(doc, "2-1", sha="abc123")

    protocol = _sections(bundle)["## Review protocol"].split("Conditions:")[0].splitlines()
    assert [line for line in protocol if line.startswith("- ")] == [
        "- `REVIEW:` on a line of its own;",
        "- `Verdict: <pass|revise>`;",
        "- `Plan digest: <sha256>` — echo the `Plan digest:` line above verbatim; "
        "do not compute it;",
        "- one concern per line, each prefixed by the marker of the condition it "
        "concerns (`C1:`, `C2:`, `C3:`, `C4:`); a condition-4 gap is a `C4:` line.",
    ]
    assert "- `/tmp/v/stage-1.md`" in _sections(bundle)["## Service file"].splitlines()

    procedure = _sections(bundle)["## Per-pair procedure"].splitlines()
    assert any(line.startswith("1. Decide `C4:`") for line in procedure)
    assert "3. Report any gap as one `C4:` line." in procedure
    assert f"{PLAN_DIGEST_MARKER} abc123" in procedure
