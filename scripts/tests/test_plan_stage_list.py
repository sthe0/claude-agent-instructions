"""The stage list of a hypertext plan is assembled from the stage files in the plan folder,
and dependencies are the only source of execution order (R15).

Difficulty removed: a hypertext plan kept its stage list by hand, and a converted plan's run
order rested on array position, which a file layout does not carry. Now `plan-seal` builds
the list from `<stem>.parts/stage-<n>.toml`, sorted by the index each file declares; every
stage of a plan with references declares `depends_on`; and the converter writes the
dependencies an old plan relied on implicitly.

Every plan here is synthetic. Tests whose names start with `test_stage_list_` run the sealer,
the resolver, the converter or the engine on plans built in this file.
"""
from __future__ import annotations

import hashlib
import sys
import tomllib
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

ISSUE = "owner/repo#12"
LIMITS = {"field": 100, "stage": 60000}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _meta() -> str:
    return (
        "[meta]\n"
        'task_id = "stage-list"\n'
        'goal = "exercise the stage list"\n'
        'done_criterion = "all stages PASSED"\n'
        'criterion_type = "measurable"\n'
    )


def _body(index: int | None, depends_on: list[int] | None = None, *, optional: bool = False,
          extra: str = "") -> str:
    """The keys of one stage; `index` or `depends_on` left out when given as None."""
    lines = []
    if index is not None:
        lines.append(f"index = {index}")
    if depends_on is not None:
        lines.append(f"depends_on = {depends_on}")
    label = index if index is not None else "unnumbered"
    lines += [
        f'title = "Stage {label}"',
        'executor = "spawn:developer"',
        f'expected_result_image = "image {label}"',
        'criterion_type = "measurable"',
        f'done_criterion = "stage {label} done"',
        f'output_artifacts = ["s{label}.py"]',
        f'method = "method of {label}"',
    ]
    if optional:
        lines += ["optional = true", f'backlog_issue = "{ISSUE}"']
    return "\n".join(lines) + "\n" + extra


def _flat(*bodies: str) -> bytes:
    """A single-file plan holding the given stage bodies, in the given order."""
    return (_meta() + "".join("\n[[stage]]\n" + body for body in bodies)).encode("utf-8")


def _tree(root: Path, files: dict[str, str]) -> Path:
    """An index with no `stage` key and the given files under `plan.parts/`."""
    parts = root / "plan.parts"
    parts.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (parts / name).write_text(body, encoding="utf-8")
    index = root / "plan.toml"
    index.write_text(_meta(), encoding="utf-8")
    return index


def _seal(index: Path, root: Path) -> str:
    from agentctl import plan_refs

    return plan_refs.seal(index, root=root)


def _listed(index: Path) -> list[dict]:
    return tomllib.loads(index.read_text(encoding="utf-8"))["stage"]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _convert(source: bytes, stem: str = "plan"):
    from agentctl import plan_convert

    return plan_convert.convert_with_report(
        source, stem, field_max_chars=LIMITS["field"], stage_max_chars=LIMITS["stage"])


def _materialize(files: dict[str, bytes], root: Path) -> Path:
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root / "plan.toml"


# --- assembly --------------------------------------------------------------------------------


def test_stage_list_is_assembled_from_the_files_sorted_by_the_index_each_declares(tmp_path):
    from agentctl import plan

    index = _tree(tmp_path, {"stage-10.toml": _body(10, [2]), "stage-2.toml": _body(2, [])})

    _seal(index, tmp_path)

    listed = _listed(index)
    assert [e["ref"] for e in listed] == ["plan.parts/stage-2.toml", "plan.parts/stage-10.toml"]
    assert [e["sha256"] for e in listed] == [
        _sha((tmp_path / e["ref"]).read_bytes()) for e in listed]
    assert [s.index for s in plan.load_plan(index, root=tmp_path).stages] == [2, 10]


def test_stage_list_an_index_without_a_stage_key_gets_one_and_keeps_the_rest(tmp_path):
    index = _tree(tmp_path, {"stage-1.toml": _body(1, [])})

    _seal(index, tmp_path)

    text = index.read_text(encoding="utf-8")
    assert text.endswith(_meta())
    assert text.startswith("stage = [")
    assert tomllib.loads(text)["meta"]["task_id"] == "stage-list"


def test_stage_list_removing_a_stage_file_and_resealing_drops_the_stage(tmp_path):
    index = _tree(tmp_path, {"stage-1.toml": _body(1, []), "stage-2.toml": _body(2, [1])})
    _seal(index, tmp_path)
    (tmp_path / "plan.parts" / "stage-2.toml").unlink()

    _seal(index, tmp_path)

    assert [e["ref"] for e in _listed(index)] == ["plan.parts/stage-1.toml"]


def test_stage_list_adding_a_stage_file_and_resealing_adds_the_stage(tmp_path):
    index = _tree(tmp_path, {"stage-1.toml": _body(1, []), "stage-3.toml": _body(3, [1])})
    _seal(index, tmp_path)
    (tmp_path / "plan.parts" / "stage-2.toml").write_text(_body(2, [1]), encoding="utf-8")

    _seal(index, tmp_path)

    assert [e["ref"] for e in _listed(index)] == [
        f"plan.parts/stage-{n}.toml" for n in (1, 2, 3)]


def test_stage_list_a_second_seal_changes_no_byte(tmp_path):
    index = _tree(tmp_path, {"stage-1.toml": _body(1, []), "stage-2.toml": _body(2, [1])})
    first = _seal(index, tmp_path)
    before = _snapshot(tmp_path)

    second = _seal(index, tmp_path)

    assert second == first
    assert _snapshot(tmp_path) == before


def test_stage_list_a_stage_file_edit_changes_only_hashes_on_reseal(tmp_path):
    index = _tree(tmp_path, {"stage-1.toml": _body(1, []), "stage-2.toml": _body(2, [1])})
    _seal(index, tmp_path)
    before = _listed(index)
    stage_file = tmp_path / "plan.parts" / "stage-2.toml"
    stage_file.write_text(stage_file.read_text().replace("method of 2", "other method"),
                          encoding="utf-8")

    _seal(index, tmp_path)

    after = _listed(index)
    assert [e["ref"] for e in after] == [e["ref"] for e in before]
    assert after[0] == before[0]
    assert after[1]["sha256"] != before[1]["sha256"]


def test_stage_list_a_hand_formatted_list_and_its_comments_survive_a_reseal(tmp_path):
    index = _tree(tmp_path, {"stage-1.toml": _body(1, [])})
    _seal(index, tmp_path)
    entry = _listed(index)[0]
    index.write_text(
        "# the list, by hand\n"
        f'stage = [ {{ ref = "{entry["ref"]}", sha256 = "{entry["sha256"]}" }} ]  # one\n\n'
        + _meta(), encoding="utf-8")
    before = index.read_bytes()

    _seal(index, tmp_path)

    assert index.read_bytes() == before


def _refuse_mismatch(root: Path) -> tuple[Path, str]:
    return _tree(root, {"stage-2.toml": _body(3, [])}), "stage-2.toml"


def _refuse_missing_index(root: Path) -> tuple[Path, str]:
    return _tree(root, {"stage-2.toml": _body(None, [])}), "stage-2.toml"


def _refuse_duplicate(root: Path) -> tuple[Path, str]:
    return _tree(root, {"stage-01.toml": _body(1, []), "stage-1.toml": _body(1, [])}), "stage-1.toml"


def _refuse_stray(root: Path) -> tuple[Path, str]:
    return _tree(root, {"stage-1.toml": _body(1, []), "stage-x.toml": _body(2, [])}), "stage-x.toml"


def _refuse_mixed(root: Path) -> tuple[Path, str]:
    index = _tree(root, {"stage-2.toml": _body(2, [])})
    index.write_text(_meta() + "\n[[stage]]\n" + _body(1, []), encoding="utf-8")
    return index, "stage-2.toml"


def _refuse_convention(root: Path) -> tuple[Path, str]:
    index = _tree(root, {})
    other = root / "elsewhere"
    other.mkdir()
    body = _body(1, [])
    (other / "stage-1.toml").write_text(body, encoding="utf-8")
    index.write_text(
        f'stage = [{{ ref = "elsewhere/stage-1.toml", sha256 = "{_sha(body.encode())}" }}]\n\n'
        + _meta(), encoding="utf-8")
    return index, "stage-1.toml"


REFUSALS = {
    "name-and-declared-index-differ": (_refuse_mismatch, "index"),
    "file-declares-no-index": (_refuse_missing_index, "index"),
    "two-files-one-index": (_refuse_duplicate, "index"),
    "stray-name": (_refuse_stray, "path"),
    "inline-stages-and-an-unreferenced-file": (_refuse_mixed, "path"),
    "listed-ref-off-the-convention": (_refuse_convention, "path"),
}


@pytest.mark.parametrize("case", REFUSALS)
def test_stage_list_a_plan_folder_that_is_not_a_stage_list_is_refused_naming_the_file(
        case, tmp_path):
    from agentctl.plan import PlanError

    build, kind = REFUSALS[case]
    index, named = build(tmp_path)
    before = _snapshot(tmp_path)

    with pytest.raises(PlanError) as exc:
        _seal(index, tmp_path)

    assert exc.value.ref_kind == kind
    assert named in str(exc.value)
    assert _snapshot(tmp_path) == before


def test_stage_list_an_index_holding_every_stage_inline_seals_as_before_and_is_not_reordered(
        tmp_path):
    parts = tmp_path / "plan.parts"
    parts.mkdir()
    text = _meta()
    for n in (3, 1, 2):
        data = f"method of {n} ".encode() * 20
        (parts / f"m{n}.md").write_bytes(data)
        stale = "0" * 64
        text += ("\n[[stage]]\n" + _body(n, []).replace(
            f'method = "method of {n}"',
            f'method = {{ ref = "plan.parts/m{n}.md", sha256 = "{stale}" }}'))
    index = tmp_path / "plan.toml"
    index.write_text(text, encoding="utf-8")

    _seal(index, tmp_path)

    raw = tomllib.loads(index.read_text(encoding="utf-8"))
    assert [s["index"] for s in raw["stage"]] == [3, 1, 2]
    assert [s["method"]["sha256"] for s in raw["stage"]] == [
        _sha((parts / f"m{n}.md").read_bytes()) for n in (3, 1, 2)]


# --- dependencies are explicit ---------------------------------------------------------------


def test_stage_list_a_stage_file_without_depends_on_is_refused_by_the_sealer(tmp_path):
    from agentctl.plan import PlanError

    index = _tree(tmp_path, {"stage-1.toml": _body(1, None)})

    with pytest.raises(PlanError) as exc:
        _seal(index, tmp_path)

    assert exc.value.ref_kind == "depends_on"
    assert "stage-1.toml" in str(exc.value)


def _hand_listed(tmp_path: Path, body: str) -> tuple[Path, dict]:
    file = tmp_path / "s.toml"
    file.write_text(body, encoding="utf-8")
    index = tmp_path / "plan.toml"
    index.write_text(
        f'stage = [{{ ref = "s.toml", sha256 = "{_sha(body.encode())}" }}]\n\n' + _meta(),
        encoding="utf-8")
    return index, tomllib.loads(index.read_text(encoding="utf-8"))


def test_stage_list_the_resolver_refuses_a_stage_file_without_depends_on(tmp_path):
    from agentctl import plan_refs
    from agentctl.plan import PlanError

    index, raw = _hand_listed(tmp_path, _body(1, None))

    with pytest.raises(PlanError) as exc:
        plan_refs.resolve_refs(raw, tmp_path, tmp_path, index_path=index)

    assert exc.value.ref_kind == "depends_on"


def test_stage_list_the_resolver_accepts_an_empty_depends_on(tmp_path):
    from agentctl import plan_refs

    index, raw = _hand_listed(tmp_path, _body(1, []))

    resolved, _visited = plan_refs.resolve_refs(raw, tmp_path, tmp_path, index_path=index)

    assert resolved["stage"][0]["depends_on"] == []


@pytest.mark.parametrize("missing", ["depends_on", "index"])
def test_stage_list_an_inline_stage_of_a_plan_with_references_declares_its_keys(
        missing, tmp_path):
    from agentctl import plan_refs
    from agentctl.plan import PlanError

    field = b"a field that lives in its own file"
    (tmp_path / "f.md").write_bytes(field)
    body = _body(None if missing == "index" else 1, None if missing == "depends_on" else [])
    body = body.replace(
        'method = "method of 1"' if missing != "index" else 'method = "method of unnumbered"',
        f'method = {{ ref = "f.md", sha256 = "{_sha(field)}" }}')
    index = tmp_path / "plan.toml"
    index.write_text(_meta() + "\n[[stage]]\n" + body, encoding="utf-8")
    raw = tomllib.loads(index.read_text(encoding="utf-8"))

    with pytest.raises(PlanError) as exc:
        plan_refs.resolve_refs(raw, tmp_path, tmp_path, index_path=index)

    assert exc.value.ref_kind == missing


def test_stage_list_a_single_file_plan_without_references_keeps_todays_rules(tmp_path):
    from agentctl import plan

    index = tmp_path / "plan.toml"
    index.write_bytes(_flat(_body(None, None), _body(None, None)))

    doc = plan.load_plan(index, root=tmp_path)

    assert [s.index for s in doc.stages] == [1, 2]
    assert [list(s.depends_on) for s in doc.stages] == [[], []]


def test_stage_list_the_sealer_leaves_a_single_file_plan_without_references_alone(tmp_path):
    index = tmp_path / "plan.toml"
    index.write_bytes(_flat(_body(None, None)))
    before = index.read_bytes()

    _seal(index, tmp_path)

    assert index.read_bytes() == before


# --- the converter ---------------------------------------------------------------------------


def _staged(files: dict[str, bytes]) -> list[int]:
    return [int(rel.rsplit("-", 1)[1].split(".")[0]) for rel in files
            if rel.startswith("plan.parts/stage-") and rel.endswith(".toml")]


def test_stage_list_the_converter_writes_the_list_sorted_by_index(tmp_path):
    source = _flat(_body(3, []), _body(1, []), _body(2, []))

    files, report = _convert(source)

    index = tomllib.loads(files["plan.toml"].decode())
    assert [e["ref"] for e in index["stage"]] == [f"plan.parts/stage-{n}.toml" for n in (1, 2, 3)]
    assert report["run_order_source"] == [3, 1, 2]
    assert report["run_order_converted"] == [1, 2, 3]
    assert report["raw_equivalence"] == "ok"
    assert report["parsed_equivalence"] == "ok"


def test_stage_list_a_stage_depended_on_ahead_of_its_list_position_gets_the_edge_it_relied_on(
        tmp_path):
    from agentctl import plan

    source = _flat(_body(1, []), _body(2, [3]), _body(3, None))

    files, report = _convert(source)

    assert report["added_depends_on"] == [{"stage": 3, "depends_on": [1]}]
    assert report["run_order_source"] == [1, 3, 2]
    assert report["run_order_converted"] == [1, 3, 2]
    index = _materialize(files, tmp_path)
    tree = plan.load_plan(index, root=tmp_path)
    assert [s.index for s in tree.stages] == [1, 2, 3]


def test_stage_list_sealing_a_fresh_converted_tree_changes_no_byte(tmp_path):
    source = _flat(_body(10, None), _body(2, None), _body(None, None), _body(4, [2]))
    files, _report = _convert(source)
    index = _materialize(files, tmp_path)
    before = _snapshot(tmp_path)

    _seal(index, tmp_path)

    assert _snapshot(tmp_path) == before


def test_stage_list_the_sealer_assembles_the_bytes_the_converter_writes(tmp_path):
    source = _flat(*(_body(n, [n - 1] if n > 1 else []) for n in range(1, 9)))
    files, _report = _convert(source)
    index = _materialize(files, tmp_path)
    written = index.read_bytes()
    index.write_bytes(written.split(b"\n\n", 1)[1])

    _seal(index, tmp_path)

    assert index.read_bytes() == written


def test_stage_list_a_depends_on_in_the_source_is_copied_unchanged_even_when_empty(tmp_path):
    source = _flat(_body(1, []), _body(2, []), _body(3, [1]))

    files, report = _convert(source)

    assert report["added_depends_on"] == []
    stages = {n: tomllib.loads(files[f"plan.parts/stage-{n}.toml"].decode()) for n in (1, 2, 3)}
    assert [stages[n]["depends_on"] for n in (1, 2, 3)] == [[], [], [1]]


def test_stage_list_a_stage_without_depends_on_gets_the_latest_required_stage_before_it():
    source = _flat(
        _body(1, None), _body(2, None), _body(3, None, optional=True), _body(4, None))

    files, report = _convert(source)

    assert report["added_depends_on"] == [
        {"stage": 1, "depends_on": []}, {"stage": 2, "depends_on": [1]},
        {"stage": 3, "depends_on": [2]}, {"stage": 4, "depends_on": [2]}]
    assert report["parsed_equivalence"] == "ok"
    assert report["run_order_source"] == report["run_order_converted"] == [1, 2, 3, 4]
    for n, expected in ((1, []), (2, [1]), (3, [2]), (4, [2])):
        assert tomllib.loads(files[f"plan.parts/stage-{n}.toml"].decode())["depends_on"] == expected


def test_stage_list_an_added_edge_never_points_at_an_optional_stage():
    from agentctl import plan

    source = _flat(_body(1, None), _body(2, None, optional=True), _body(3, None))

    files, report = _convert(source)

    optional = {2}
    for added in report["added_depends_on"]:
        if added["stage"] not in optional:
            assert not set(added["depends_on"]) & optional
    doc = plan.parse_plan(tomllib.loads(source.decode()))
    assert plan.optional_stage_violations(doc) == []
    assert report["parsed_equivalence"] == "ok"


def test_stage_list_the_added_dependency_follows_the_run_order_not_the_list_order():
    source = _flat(_body(2, [1]), _body(1, []), _body(3, None))

    _files, report = _convert(source)

    assert report["run_order_source"] == [1, 2, 3]
    assert report["added_depends_on"] == [{"stage": 3, "depends_on": [2]}]


def test_stage_list_an_added_index_is_listed_and_numbers_the_stage_by_its_position():
    source = _flat(_body(None, []), _body(None, None))

    files, report = _convert(source)

    assert report["added_indices"] == [1, 2]
    assert report["added_depends_on"] == [{"stage": 2, "depends_on": [1]}]
    assert tomllib.loads(files["plan.parts/stage-2.toml"].decode())["index"] == 2


def test_stage_list_the_converted_plan_equals_the_source_modulo_the_additions(tmp_path):
    from agentctl import plan, plan_refs

    source = _flat(_body(1, None), _body(None, None), _body(3, [1]))
    raw = tomllib.loads(source.decode())

    files, report = _convert(source)
    index = _materialize(files, tmp_path)

    keys = plan_refs.plan_stage_keys(raw)
    expected = plan.parse_plan({**raw, "stage": [t for _n, t in keys["stages"]]})
    tree = plan.load_plan(index, root=tmp_path)
    assert plan.plan_content_digest(tree) == plan.plan_content_digest(expected)
    assert report["parsed_equivalence"] == "ok"
    assert report["raw_equivalence"] == "ok"
    assert report["content_digest"] == plan.plan_content_digest(expected)
    assert report["source_content_digest"] != report["content_digest"]


def test_stage_list_has_additions_is_true_for_any_added_dependency_an_empty_one_included():
    from agentctl import plan_convert

    nothing = _convert(_flat(_body(1, []), _body(2, [1])))[1]
    an_empty_one = _convert(_flat(_body(1, None)))[1]

    assert not plan_convert.has_additions(nothing)
    assert an_empty_one["added_depends_on"] == [{"stage": 1, "depends_on": []}]
    assert plan_convert.has_additions(an_empty_one)


def test_stage_list_the_text_report_names_every_addition(tmp_path):
    from agentctl import plan_convert

    _files, report = _convert(_flat(_body(None, None), _body(None, None)))

    text = "\n".join(plan_convert._report_lines(report))
    assert "depends_on" in text
    assert "[1]" in text


# --- the converter simulates the engine ------------------------------------------------------

ENGINE_PLAN = (
    ("a", _body(4, [2])),
    ("b", _body(3, [])),
    ("c", _body(1, [3])),
    ("d", _body(2, [3])),
)


def _engine_run_order(stages: list, store) -> list[int]:
    """The order `cmd_next_stage` hands out `stages` when each one passes as soon as it runs."""
    from agentctl import cli
    from agentctl.state import GateRecord, Node, SessionState, StageStatus

    state = SessionState(session_id="engine-pin", task_id="pin")
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by="user")
    state.stages = stages
    state.node = Node.PARTITIONED.value
    store.save(state)
    order: list[int] = []
    for _ in range(len(state.stages) + 1):
        result = cli.cmd_next_stage(Namespace(session="engine-pin"), store=store)
        if not result.ok:
            break
        state = store.load("engine-pin")
        order.append(state.current_stage)
        state.stage(state.current_stage).outcome.status = StageStatus.PASSED.value
        state.node = Node.VERIFYING.value
        store.save(state)
    return order


PIN_SOURCES = {
    "list-order-is-not-index-order": _flat(*(body for _n, body in ENGINE_PLAN)),
    "with-an-optional-stage": _flat(
        _body(1, []), _body(2, [1], optional=True), _body(3, [1]), _body(4, [3])),
    "supplies-edge": _flat(
        _body(1, []), _body(2, None, extra="\n[[stage.supplies]]\non = 1\n"), _body(3, [2])),
}


@pytest.mark.parametrize("name", PIN_SOURCES)
def test_stage_list_the_converters_simulation_is_the_engines_run_order(name, store):
    from agentctl import plan, plan_refs

    raw = tomllib.loads(PIN_SOURCES[name].decode())
    rows = [(n, plan_refs.run_dependencies(t)) for n, t in plan_refs.stage_tables(raw)]

    assert plan_refs.simulate_run_order(rows) == _engine_run_order(
        plan.parse_plan(raw).stages, store)


def test_stage_list_the_converted_tree_runs_in_the_order_the_report_gives(tmp_path, store):
    from agentctl import plan

    source = _flat(_body(2, [3]), _body(1, []), _body(3, [1]), _body(4, None))
    files, report = _convert(source)
    index = _materialize(files, tmp_path)

    tree = plan.load_plan(index, root=tmp_path)

    assert _engine_run_order(tree.stages, store) == report["run_order_converted"]
    assert report["run_order_converted"] == report["run_order_source"] == [1, 3, 2, 4]


# --- plan-externalize and the live verb ------------------------------------------------------


def test_stage_list_externalize_adds_the_missing_keys_and_lists_them(tmp_path):
    from agentctl import plan, plan_refs

    index = tmp_path / "plan.toml"
    long_text = "x" * 400
    index.write_bytes(_flat(
        _body(1, None), _body(None, None).replace('"method of unnumbered"', f'"{long_text}"')))

    directive = plan_refs.cmd_plan_externalize(
        Namespace(plan=str(index), stage=2, field="method"))

    assert directive.ok, directive.detail
    assert directive.data["added_indices"] == [2]
    assert directive.data["added_depends_on"] == [
        {"stage": 1, "depends_on": []}, {"stage": 2, "depends_on": [1]}]
    assert "depends_on [1] to stage 2" in directive.detail
    doc = plan.load_plan(index, root=tmp_path)
    assert [(s.index, list(s.depends_on)) for s in doc.stages] == [(1, []), (2, [1])]
    raw = tomllib.loads(index.read_text(encoding="utf-8"))
    assert [(s["index"], s["depends_on"]) for s in raw["stage"]] == [(1, []), (2, [1])]


def test_stage_list_externalize_changes_no_dependency_a_plan_already_declares(tmp_path):
    from agentctl import plan_refs

    index = tmp_path / "plan.toml"
    index.write_bytes(_flat(_body(1, []), _body(2, [1]).replace(
        '"method of 2"', f'"{"y" * 400}"')))

    directive = plan_refs.cmd_plan_externalize(
        Namespace(plan=str(index), stage=2, field="method"))

    assert directive.ok, directive.detail
    assert directive.data["added_indices"] == []
    assert directive.data["added_depends_on"] == []


def _live_session(store, tmp_path: Path, source: bytes):
    from agentctl.state import GateRecord, Node, PlanPresentation, PlanReview, SessionState

    plan_dir = tmp_path / "plans-live"
    plan_dir.mkdir()
    plan_file = plan_dir / "demo.toml"
    plan_file.write_bytes(source)
    root = _sha(source)
    state = SessionState(session_id="live-a", task_id="live")
    state.node = Node.EXECUTING.value
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by="user")
    state.plan_path = str(plan_file)
    state.accepted_plan_digest = root
    state.plan_snapshot_path = str(plan_dir / "snapshot.toml")
    state.plan_snapshot_hash = root
    state.plan_review = PlanReview(
        plan_path=str(plan_file), verdict="pass", reviewer="thinker", plan_sha256=root)
    state.plan_presentations = [PlanPresentation(
        plan_path=str(plan_file), kind="essence", plan_sha256=root,
        rendering_sha256="0" * 64, rendering_text="the essence", presented_ts=1.0)]
    store.save(state)
    return plan_file


def _live(store):
    from agentctl import plan_convert

    return plan_convert.cmd_plan_convert_live(
        Namespace(session="live-a", by="agent", revert=False), store=store)


def _live_world(store, plan_file: Path) -> dict:
    state = store.load("live-a")
    return {
        "node": state.node,
        "accepted": state.accepted_plan_digest,
        "conversions": list(state.plan_conversions),
        "history": list(state.history),
        "files": _snapshot(plan_file.parent),
    }


LIVE_REFUSED = {
    "a-dependency-would-be-added": _flat(_body(1, None), _body(2, [1])),
    "an-empty-dependency-would-be-added": _flat(_body(1, None)),
    "the-run-order-would-change": _flat(_body(2, []), _body(1, [])),
}


@pytest.mark.parametrize("case", LIVE_REFUSED)
def test_stage_list_live_conversion_refuses_a_plan_that_would_run_differently(
        case, store, tmp_path):
    plan_file = _live_session(store, tmp_path, LIVE_REFUSED[case])
    before = _live_world(store, plan_file)

    result = _live(store)

    assert not result.ok
    assert result.data is None or result.data == {}
    assert _live_world(store, plan_file) == before


@pytest.mark.parametrize("case", ["a-dependency-would-be-added", "the-run-order-would-change"])
def test_stage_list_the_two_live_refusals_do_not_name_what_they_refuse_to_add(
        case, store, tmp_path):
    _live_session(store, tmp_path, LIVE_REFUSED[case])

    result = _live(store)

    assert not result.ok
    assert "depends_on" not in result.detail
    assert "replan" not in result.detail


LIVE_PROCEEDS = {
    "in-list-order": _flat(_body(1, []), _body(2, [1])),
    "out-of-list-order-with-the-same-run-order": _flat(_body(2, [1]), _body(1, [])),
}


@pytest.mark.parametrize("case", LIVE_PROCEEDS)
def test_stage_list_live_conversion_proceeds_for_a_plan_that_adds_nothing(
        case, store, tmp_path, monkeypatch):
    from agentctl import plan_store

    monkeypatch.setenv(plan_store.OBJECTS_DIR_ENV, str(tmp_path / "objects"))
    plan_file = _live_session(store, tmp_path, LIVE_PROCEEDS[case])

    result = _live(store)

    assert result.ok, result.detail
    assert (plan_file.parent / "demo.parts" / "stage-2.toml").is_file()
    raw = tomllib.loads(plan_file.read_text(encoding="utf-8"))
    assert [e["ref"] for e in raw["stage"]] == [
        "demo.parts/stage-1.toml", "demo.parts/stage-2.toml"]
