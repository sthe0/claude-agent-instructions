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
    is_unit_id,
    load_plan_with_digest,
    pair_binding,
    parse_pair,
    parse_plan,
    parse_unit,
    plan_reliance_set,
    reliance_closure,
    reliance_set,
    review_ids,
    review_pairs,
    review_units,
)
from agentctl.render import (
    TopoUnitsCorrupt,
    materialize_topo_units,
    node_file_text,
    pair_service_text,
    render_order_md,
    render_pair_review_bundle,
    render_plan_md,
    render_stage_brief,
    render_stage_fields,
    render_stage_interface,
    render_unit_review_bundle,
    topo_node_files,
    topo_pair_view,
    topo_pair_view_dirname,
    unit_base_text,
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
_UNIT_SECTION_HEADINGS = (
    "## Fields",
    "## Edges",
    "## Per-unit procedure",
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


def _doc(stages, order=None, final_check=None, **meta_overrides):
    meta = {"task_id": "t", **meta_overrides}
    if order is not None:
        meta["order"] = order
    data = {"meta": meta, "stage": stages}
    if final_check is not None:
        data["final_check"] = final_check
    return parse_plan(data)


def _doc_with_raw(stages, raw_depends_on):
    """A plan whose raw `depends_on` carries edges the supplies-derived graph lacks:
    parsing merges the two, so the divergence is written onto the parsed doc."""
    doc = _doc(stages)
    doc.raw_depends_on.update({n: tuple(edges) for n, edges in raw_depends_on.items()})
    return doc


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


def _unit_sections(bundle: str) -> dict[str, str]:
    """A unit bundle split at its own section headings (`_UNIT_SECTION_HEADINGS`)."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in bundle.splitlines():
        matched = next((h for h in _UNIT_SECTION_HEADINGS if line.startswith(h)), None)
        if matched is not None:
            current = matched
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(body).strip("\n") for name, body in sections.items()}


def _unit_bundle(doc, unit, sha="d"):
    return render_unit_review_bundle(doc, unit, plan_sha256=sha)


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

    # A `base-<s>` pair has no `plan-<s>` form: C3 reads the stage's declared product
    # against the requirements the edge names, never as the base's own FULL product.
    base_c3 = _conditions(_bundle(doc, "base-2"))[2]
    assert "every named requirement is discharged through this edge" in base_c3
    assert "declared product delivers what the requirement asks" in base_c3
    assert "delivers its FULL declared product" not in base_c3


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
        # The coverage edge of stage 3: base side is the order file, the service text is
        # the stage's contract interface plus the control the entry names.
        ("base-3", "base.md", "stage-3.md", pair_service_text(doc, 3, "base")),
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


def test_tb6_unit_base_bundle_carries_meta_order_coverage_and_final_check():
    # E5: the reconstructed activity is an ordinary unit (`unit:base`) holding the order
    # AND the meta's goal, done criterion and final checks; there is no `plan-<s>` pair.
    doc = _order_doc()
    bundle = _unit_bundle(doc, "unit:base")
    sections = _unit_sections(bundle)
    assert bundle.startswith("# Topological review unit: unit:base\n")
    fields = sections["## Fields"]
    assert fields.rstrip("\n") == unit_base_text(doc).rstrip("\n")
    for sentinel in (
        "- **Done criterion:** DC-SENTINEL",
        "- **External research:** ER-SENTINEL",
        "- fc1: `true` (expected exit 0)",
        "  - R1: stage 1 verify_command, stage 2 verify_command",
        "- **Requires traceability:** True",
        "  - **R1**: R1",
    ):
        assert sentinel in fields.splitlines()
    assert sections["## Edges"].splitlines() == [
        "Outbound — the coverage entries, each a typed edge of the base on a stage:",
        "- R1 → stage 1: `stage 1 verify_command`",
        "- R1 → stage 2: `stage 2 verify_command`",
        "",
        "Inbound — what relies on the base: nothing (the base is the root).",
    ]
    assert bundle.splitlines().count(f"{PLAN_DIGEST_MARKER} d") == 1
    # Self-contained: no view directory, no service file, no order-context section.
    for absent in ("## Service file", "## Order context", "## Base: ", "view-"):
        assert absent not in bundle


def test_tb6_base_stage_pair_shows_only_what_the_coverage_edge_names():
    # E5: `base-<s>` is an ordinary `<b>-<s>` pair. The base side is only the requirements
    # the coverage entries on stage s name -- never the goal, done criterion, final checks,
    # the customer / functional place, or another requirement.
    doc = _doc(
        [_stage(1), _stage(2, depends_on=[1])],
        order=_order(
            ["R1", "R2"],
            {"R1": ["stage 1 verify_command"], "R2": ["stage 2 verify_command"]},
            customer="CUSTOMER-SENTINEL",
            customer_id="cust-id",
            functional_place="PLACE-SENTINEL",
        ),
        done_criterion="DC-SENTINEL",
    )
    assert review_pairs(doc) == ("base-1", "base-2", "2-1")
    bundle = _bundle(doc, "base-2")
    sections = _sections(bundle)
    assert bundle.startswith("# Topological review pair: base-2\n")
    assert "## Order context" not in sections
    base = sections["## Base: "]
    assert "  - **R2**: R2" in base.splitlines()
    assert "R1" not in base
    for withheld in ("CUSTOMER-SENTINEL", "PLACE-SENTINEL", "DC-SENTINEL", "stage 1 verify_command"):
        assert withheld not in bundle
    service = sections["## Service declared product: "]
    assert pair_service_text(doc, 2, "base").rstrip("\n") in service
    assert render_stage_interface(doc, 2, contract=True).rstrip("\n") in service
    assert "Controls named by the coverage entries" in service
    assert "- R2 → `stage 2 verify_command`: " in service
    assert _method_sentinel(2) not in bundle
    assert "- `/tmp/v/stage-2.md`" in sections["## Service file"].splitlines()
    assert _edge_lines(bundle) == [
        "- Edge: the base activity relies on stage 2 — order coverage",
        "  - R2 → stage 2 verify_command",
        "- Ordering: none",
    ]
    assert bundle.splitlines().count(f"{PLAN_DIGEST_MARKER} d") == 1


def test_tb6_base_stage_pair_conditions_read_the_named_requirements_through_the_edge():
    doc = _order_doc()
    assert _conditions(_bundle(doc, "base-2")) == [
        "- `C1:` each requirement this edge names is stated unambiguously and attributed "
        "to stage 2 (Stage 2) in a non-arbitrary way",
        "- `C2:` each named requirement is genuinely derived from the difficulty and the "
        "functional place",
        "- `C3:` every named requirement is discharged through this edge — stage 2 "
        "(Stage 2)'s declared product delivers what the requirement asks",
        "- `C4:` the control each coverage entry names, in stage 2 (Stage 2), genuinely "
        "decides its requirement and is checkable from that stage's declared product",
    ]


def test_tb6_node_files_hold_no_stage_content_and_the_unit_adds_meta_coverage_and_checks():
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
    unit_md = unit_base_text(doc)
    assert base_md == "\n".join(render_order_md(doc)).rstrip() + "\n"
    assert "CUSTOMER-SENTINEL" in base_md
    assert "PLACE-SENTINEL" in base_md
    assert "**R1**" in base_md
    # The node file is the order only; coverage and traceability are fields of the unit.
    assert "R1: stage 1 verify_command, stage 2 verify_command" not in base_md
    assert "Requires traceability" not in base_md
    for stage_content in (_method_sentinel(1), _method_sentinel(2), "Stage 1", "Stage 2", "Expected result image"):
        assert stage_content not in base_md
        assert stage_content not in unit_md
    assert "## Stage" not in unit_md
    assert base_md.rstrip("\n") in unit_md
    assert "R1: stage 1 verify_command, stage 2 verify_command" in unit_md
    assert "- **Requires traceability:** True" in unit_md
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
    _bundle(doc, "2-1")
    # `plan-<s>` and `base-plan` are retired ids; a unit id is not a pair id; an order-less
    # plan has no `base-<s>` pair (no coverage entry names a stage).
    for refused in (
        "1-2", "plan-1", "plan-2", "99-1", "base-plan", "base-2", "2", "order", "1", "", "2-",
        "unit:base", "unit:1",
    ):
        with pytest.raises(ValueError):
            _bundle(doc, refused)
        with pytest.raises(ValueError):
            parse_pair(doc, refused)
    for unit in ("unit:base", "unit:1", "unit:2"):
        _unit_bundle(doc, unit)
    for refused in ("unit:plan", "unit:99", "unit:", "2-1", "base", "1", "plan-1", ""):
        with pytest.raises(ValueError):
            _unit_bundle(doc, refused)
        with pytest.raises(ValueError):
            parse_unit(doc, refused)


def test_tb8_non_direct_edges_reliance_set_raw_depends_on_and_closure():
    doc = _doc_with_raw([
        _stage(1),
        _stage(2),
        _stage(3, depends_on=[1]),
        _stage(4, supplies=[{"on": 2}]),
    ], {4: [3]})
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
    # `base-<s>` only for a stage a coverage entry names (stage 1; `final_check 1` is not
    # stage-addressed), then the stage reliance edges; no `plan-<s>`, no `base-plan`.
    assert review_pairs(doc) == ("base-1", "2-1", "3-1", "3-2")
    assert parse_pair(doc, "3-2") == (3, 2)
    assert parse_pair(doc, "base-1") == ("base", 1)
    for retired in ("plan-1", "plan-3", "base-plan", "base-2", "base-3"):
        with pytest.raises(ValueError):
            parse_pair(doc, retired)
    assert review_units(doc) == ("unit:base", "unit:1", "unit:2", "unit:3")
    assert review_ids(doc) == review_units(doc) + review_pairs(doc)
    assert parse_unit(doc, "unit:base") == "base"
    assert parse_unit(doc, "unit:3") == 3
    assert all(is_unit_id(u) for u in review_units(doc))
    assert not any(is_unit_id(p) for p in review_pairs(doc))


def test_tb8_plan_without_an_order_has_no_base_pair_and_no_base_file_but_still_a_base_unit():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    assert review_pairs(doc) == ("2-1",)
    assert review_units(doc) == ("unit:base", "unit:1", "unit:2")
    assert "base.md" not in topo_node_files(doc)
    assert node_file_text(doc, "base") == ""
    for retired in ("base-plan", "plan-2", "base-2"):
        with pytest.raises(ValueError):
            parse_pair(doc, retired)
    bundle = _unit_bundle(doc, "unit:base")
    assert bundle.startswith("# Topological review unit: unit:base\n")
    assert "- none" in _unit_sections(bundle)["## Edges"].splitlines()


def test_tb8_pair_binding_has_seven_digests_and_base_pair_context_identity():
    doc = _order_doc()
    binding = pair_binding(doc, "base-2")
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
        return _doc_with_raw([
            _stage(1),
            _stage(2, supplies=[{"on": 1, "element": "e1"}]),
            _stage(3),
        ], {2: raw_depends_on})

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
    # parse_plan validates the merged graph, so a dangling raw edge is only reachable
    # on a doc whose raw depends_on was written after parsing.
    doc = _doc_with_raw([
        _stage(1),
        _stage(2, supplies=[{"on": 1}]),
    ], {2: [99, 1]})
    with pytest.raises(PlanError):
        reliance_set(doc, 2)
    with pytest.raises(PlanError):
        review_pairs(doc)


def test_tb10_raw_union_cycle_hidden_behind_acyclic_derived_graph_raises_planerror():
    # Raw depends_on cycle 1 -> 2 -> 3 -> 1; stage 3's supplies point the DERIVED
    # graph at a plain sink instead, so the derived graph is acyclic.
    doc = _doc_with_raw([
        _stage(1, depends_on=[2]),
        _stage(2, depends_on=[3]),
        _stage(3, supplies=[{"on": 4}]),
        _stage(4),
    ], {3: [1]})
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
    doc = _doc_with_raw([
        _stage(1, depends_on=[2, 3]),
        _stage(2, depends_on=[3]),
        _stage(3, supplies=[{"on": 4}]),
        _stage(4),
    ], {3: [2]})
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


def test_tb13_node_files_are_stage_and_base_when_an_order_is_declared():
    doc = _order_doc()
    assert sorted(topo_node_files(doc)) == ["base.md", "stage-1.md", "stage-2.md"]
    assert topo_node_files(doc)["base.md"] == node_file_text(doc, "base")
    assert topo_node_files(doc)["stage-1.md"] == node_file_text(doc, 1)
    assert topo_pair_view(doc, "base-1") == ["stage-1.md"]
    assert topo_pair_view(doc, "base-2") == ["stage-2.md"]
    for retired in ("plan-1", "base-plan", "unit:base", "unit:1"):
        with pytest.raises(ValueError):
            topo_pair_view(doc, retired)


def test_tb14_materialize_manifest_sha256s_match_the_files(tmp_path):
    doc = _doc(
        [_stage(1), _stage(2, depends_on=[1]), _stage(3, depends_on=[2])],
        order=_order(["R1"], {"R1": ["stage 3 verify_command"]}),
    )
    version_root = materialize_topo_units(doc, "shaA", tmp_path)
    assert version_root == tmp_path / "shaA"
    _assert_manifest_matches_files(version_root)
    # One view directory per PAIR (the coverage edge `base-3` included); a unit is
    # self-contained and has no view directory.
    assert sorted(p.name for p in version_root.iterdir() if p.is_dir()) == [
        "view-2-1", "view-3-2", "view-base-3",
    ]
    assert sorted(p.name for p in version_root.iterdir() if p.is_file()) == [
        "MANIFEST.json", "base.md", "stage-1.md", "stage-2.md", "stage-3.md",
    ]
    assert [p.name for p in (version_root / "view-base-3").iterdir()] == ["stage-3.md"]
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
    return _doc_with_raw([
        _stage(1),
        _stage(2),
        _stage(3, supplies=[{"on": 1}]),
    ], {3: [1, 2]})


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
    doc = _doc_with_raw([
        _stage(1, depends_on=[2]),
        _stage(2),
        _stage(3, supplies=[{"on": 1}]),
    ], {3: [1, 2]})
    assert _edge_lines(_bundle(doc, "3-1"))[-1] == "- Ordering: engine-ordered"
    assert _edge_lines(_bundle(doc, "3-2"))[-1] == "- Ordering: engine-ordered"


def test_tb22_protocol_constants_present_and_not_duplicated_in_render_module():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    # The pair bundle and the unit bundle share one protocol.
    for bundle in (_bundle(doc, "2-1"), _unit_bundle(doc, "unit:1")):
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
        "- one concern per line, written `blocking: [re:<concern-id>] <marker> <concern>` "
        "or `note: ...`, where <marker> is the condition the concern concerns "
        "(`C1:`, `C2:`, `C3:`, `C4:`); a condition-4 gap is a `C4:` line. "
        "An untagged concern line is refused.",
    ]
    assert "  - Block only on a part that changed since the last review of this pair, or on " \
        "a part that still carries an unresolved blocker, re-raised as `re:<concern-id>` " \
        "(the stable id of the earlier concern). A blocking concern on an unchanged part " \
        "is recorded as advisory." in _sections(bundle)["## Review protocol"].splitlines()
    assert "- `/tmp/v/stage-1.md`" in _sections(bundle)["## Service file"].splitlines()

    procedure = _sections(bundle)["## Per-pair procedure"].splitlines()
    assert any(line.startswith("1. Decide `C4:`") for line in procedure)
    assert "3. Report any gap as one `C4:` line." in procedure
    assert f"{PLAN_DIGEST_MARKER} abc123" in procedure


def test_tb23_stage_unit_bundle_is_self_contained_fields_and_full_edge_set():
    doc = _doc(
        [
            _stage(
                1, title="SUPPLIER-TITLE", expected_result_image="SUPPLIER-RESULT",
                output_artifacts=["supplier/OUT-SENTINEL.py"],
            ),
            _stage(2, depends_on=[1], supplies=[{"on": 1, "element": "e1"}]),
            _stage(3, depends_on=[2], supplies=[{"on": 2}]),
        ],
        order=_order(["R1"], {"R1": ["stage 3 verify_command"]}),
        goal="GOAL-SENTINEL",
        final_check=[{"command": "true", "expected_exit": 0, "label": "FINAL-CHECK-SENTINEL"}],
    )
    bundle = _unit_bundle(doc, "unit:2", sha="abc123")
    sections = _unit_sections(bundle)
    assert bundle.startswith("# Topological review unit: unit:2\n")
    assert sections["## Fields"].rstrip("\n") == render_stage_fields(doc, 2).rstrip("\n")
    assert "- **Depends on:** stage 1" in sections["## Fields"].splitlines()
    # The unit shows its own fields only: no plan header, no final checks, no supplier block.
    for outside in (
        "GOAL-SENTINEL", "FINAL-CHECK-SENTINEL", "SUPPLIER-TITLE", "SUPPLIER-RESULT",
        "OUT-SENTINEL", "# Plan:", "PROJECTED BRIEF", "Final verification",
    ):
        assert outside not in bundle
    assert sections["## Edges"].splitlines() == [
        "Outbound — what this stage relies on:",
        "- stage 1 — supplies `e1`",
        "",
        "Inbound — what relies on this stage:",
        "- stage 3 — supplies whole product",
    ]
    # Another stage's fields are not part of this unit.
    assert _method_sentinel(1) not in bundle
    assert _method_sentinel(3) not in bundle
    # No service file, no view directory: the bundle is the whole review input.
    for absent in ("## Service file", "## Order context", "## Base: ", "view-"):
        assert absent not in bundle
    assert f"{PLAN_DIGEST_MARKER} abc123" in bundle.splitlines()
    procedure = sections["## Per-unit procedure"].splitlines()
    assert "2. Report any gap as one `C4:` line." in procedure
    protocol = sections["## Review protocol"]
    assert f"`{REVIEW_MARKER}` on a line of its own;" in protocol
    assert "since the last review of this unit" in protocol
    conditions = _conditions(bundle)
    assert [line.split(" ")[1] for line in conditions] == ["`C1:`", "`C2:`", "`C3:`", "`C4:`"]


def test_tb23_unit_edges_name_coverage_inbound_and_state_an_orphan_explicitly():
    doc = _doc(
        [_stage(1), _stage(2, depends_on=[1]), _stage(3, depends_on=[1])],
        order=_order(["R1"], {"R1": ["stage 3 verify_command"]}),
    )
    # Stage 3 is a sink but a requirement names it: inbound is the base's coverage edge.
    assert _unit_sections(_unit_bundle(doc, "unit:3"))["## Edges"].splitlines() == [
        "Outbound — what this stage relies on:",
        "- stage 1 — supplies whole product",
        "",
        "Inbound — what relies on this stage:",
        "- the base: requirement R1 → `stage 3 verify_command`",
    ]
    # Stage 2: nothing relies on it and no requirement names it -- an orphan, said outright.
    orphan_edges = _unit_sections(_unit_bundle(doc, "unit:2"))["## Edges"]
    assert "- none — ORPHAN: no stage relies on this stage and no requirement names it" in orphan_edges
    assert "ORPHAN" not in _unit_sections(_unit_bundle(doc, "unit:1"))["## Edges"]
    assert "ORPHAN" not in _unit_sections(_unit_bundle(doc, "unit:3"))["## Edges"]


def test_tb23_base_unit_conditions_judge_the_requirements_and_the_goal_against_each_other():
    doc = _order_doc()
    conditions = _conditions(_unit_bundle(doc, "unit:base"))
    assert len(conditions) == 4
    assert "follows from the difficulty and the functional place" in conditions[1]
    assert "coverage entry or a final check" in conditions[2]
    assert "the goal, the done criterion and the final checks match the requirements" in conditions[3]
    stage_conditions = _conditions(_unit_bundle(doc, "unit:2"))
    assert len(stage_conditions) == 4
    assert "the inbound edge set is not empty" in stage_conditions[2]


def test_tb23_unit_bundle_re_review_carries_prior_review_and_changed_parts():
    doc = _doc([_stage(1), _stage(2, depends_on=[1])])
    history = {
        "records": [{
            "record_seq": 3, "reviewer_verdict": "revise", "effective_verdict": "revise",
            "concerns": [{
                "id": "c-1", "severity": "blocking", "effective_severity": "blocking",
                "unresolved": True, "parts": ["stage:2"], "text": "result image is vague",
            }],
        }],
        "changed_parts_since_last": ["stage:2"],
    }
    first = _unit_bundle(doc, "unit:2")
    again = render_unit_review_bundle(doc, "unit:2", plan_sha256="d", history=history)
    assert "## Prior review of this unit" not in first
    assert "## Prior review of this unit" in again
    assert "- `c-1` — blocking (effective: blocking) — UNRESOLVED BLOCKER — parts: stage:2" in again
    assert "Parts of this unit changed since the last review: `stage:2`" in again
    assert "This is a re-review." in again
