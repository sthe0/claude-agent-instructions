"""Stage identity in hypertext plans: a stage is its declared `index` (else its 1-based
position), so zero-based and out-of-order plans convert, resolve, and attribute their
externalized content to the right stage.

Tests whose names start with `test_hand_built_` build their plan by hand, with field
references only and no converter, so they exercise the resolver and the views alone.
"""
from __future__ import annotations

import hashlib
import re
import sys
import tomllib
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

LONG = 5000
SMALL_FIELD_MAX = 100
BIG_STAGE_MAX = 60000
ORDERS = {"zero-based": [0, 1, 2], "out-of-order": [1, 2, 14, 3]}
SHA_IN_OBJECTS = re.compile(r"objects/([0-9a-f]{64})")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _long(index: int) -> str:
    return f"distinct content of stage {index}: " + ("x" * LONG)


def _meta() -> str:
    return (
        "[meta]\n"
        'task_id = "identity"\n'
        'goal = "exercise stage identity"\n'
        'done_criterion = "all stages PASSED"\n'
        'criterion_type = "measurable"\n'
    )


def _stage(index: int, depends_on: list[int], *, method: str, with_index: bool = True) -> str:
    return (
        "\n[[stage]]\n"
        + (f"index = {index}\n" if with_index else "")
        + f'title = "Stage {index}"\n'
        'executor = "spawn:developer"\n'
        f'expected_result_image = "image {index}"\n'
        'criterion_type = "measurable"\n'
        f'done_criterion = "stage {index} done"\n'
        f"depends_on = {depends_on}\n"
        f'output_artifacts = ["s{index}.py"]\n'
        f"method = {method}\n"
    )


def _chain(order: list[int]) -> list[tuple[int, list[int]]]:
    return [(n, [order[i - 1]] if i else []) for i, n in enumerate(order)]


def _plan_source(order: list[int], *, long_stage: int | None = None, with_index: bool = True) -> bytes:
    text = _meta()
    for n, deps in _chain(order):
        body = _long(n) if n == long_stage else f"method of {n}"
        text += _stage(n, deps, method=f'"""\n{body}\n"""', with_index=with_index)
    return text.encode("utf-8")


def _convert(source: bytes, *, field: int = SMALL_FIELD_MAX, stage: int = BIG_STAGE_MAX):
    from agentctl import plan_convert

    return plan_convert.convert_with_report(
        source, "idp", field_max_chars=field, stage_max_chars=stage)


def _materialize(files: dict[str, bytes], root: Path) -> Path:
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root / "idp.toml"


def _hand_built(root: Path, order: list[int]) -> Path:
    """Inline stages whose `method` is a reference to a distinct 5000-char file."""
    parts = root / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    text = _meta()
    for n, deps in _chain(order):
        content = _long(n)
        (parts / f"m{n}.md").write_text(content, encoding="utf-8")
        ref = f'{{ ref = "parts/m{n}.md", sha256 = "{_sha(content)}" }}'
        text += _stage(n, deps, method=ref)
    index = root / "plan.toml"
    index.write_text(text, encoding="utf-8")
    return index


def _site(kind, index, views, **selector):
    from agentctl.render import build_site_prompt

    return build_site_prompt(kind, index, view_root=views, **selector)


def _printed(site) -> str:
    texts = [site.text]
    if site.view_dir is not None:
        texts += [(site.view_dir / name).read_text(encoding="utf-8")
                  for name in site.view_files if not name.startswith("objects/")]
    return "\n".join(texts)


@pytest.mark.parametrize("name", ORDERS)
def test_a_plan_converts_resolves_and_loads_like_its_source(name, tmp_path):
    from agentctl import plan

    order = ORDERS[name]
    source = _plan_source(order)

    files, report = _convert(source)
    index = _materialize(files, tmp_path)

    tree = plan.load_plan(index, root=tmp_path)
    original = plan.parse_plan(tomllib.loads(source.decode()))
    assert [s.index for s in tree.stages] == sorted(order)
    assert [(s.index, s.depends_on) for s in tree.stages] == sorted(
        (s.index, s.depends_on) for s in original.stages)
    assert {n: plan.stage_closure_digest(tree, n) for n in order} == {
        n: plan.stage_closure_digest(original, n) for n in order}
    assert plan.plan_content_digest(tree) == plan.plan_content_digest(original)
    assert report["added_indices"] == []
    assert report["parsed_equivalence"] == "ok"


@pytest.mark.parametrize("name", ORDERS)
def test_stage_files_are_named_by_their_declared_index(name):
    files, _ = _convert(_plan_source(ORDERS[name]))

    assert sorted(f for f in files if f.endswith(".toml") and f != "idp.toml") == sorted(
        f"idp.parts/stage-{n}.toml" for n in ORDERS[name])


@pytest.mark.parametrize("name", ORDERS)
def test_an_oversize_stage_is_reported_and_externalized_under_its_declared_index(name):
    order = ORDERS[name]
    target = order[-1]
    stage_max = 3000

    files, report = _convert(_plan_source(order, long_stage=target), stage=stage_max)

    assert [o["node"] for o in report["oversize_stages"]] == [target]
    assert [e["node"] for e in report["externalized"]] == [target]
    assert report["externalized"][0]["path"] == f"idp.parts/stage-{target}-method.md"
    assert report["externalized"][0]["path"] in files


def test_a_source_stage_without_an_index_gets_its_position_and_is_listed():
    from agentctl import plan

    source = _plan_source([1, 2, 3], with_index=False)

    files, report = _convert(source)

    assert report["added_indices"] == [1, 2, 3]
    assert "idp.parts/stage-1.toml" in files
    stage_file = tomllib.loads(files["idp.parts/stage-2.toml"].decode())
    assert stage_file["index"] == 2
    assert [s.index for s in plan.parse_plan(tomllib.loads(source.decode())).stages] == [1, 2, 3]


def test_a_zero_based_source_names_its_first_stage_file_stage_0():
    files, _ = _convert(_plan_source([0, 1, 2]))

    assert "idp.parts/stage-0.toml" in files
    assert "idp.parts/stage-3.toml" not in files


def test_a_referenced_stage_file_without_an_index_is_refused_as_index(tmp_path):
    from agentctl import plan_refs
    from agentctl.plan import PlanError

    body = _stage(1, [], method='"m"', with_index=False).replace("\n[[stage]]\n", "")
    (tmp_path / "s.toml").write_text(body, encoding="utf-8")
    index = tmp_path / "plan.toml"
    index.write_text(
        f'stage = [{{ ref = "s.toml", sha256 = "{_sha(body)}" }}]\n' + _meta(), encoding="utf-8")
    raw = tomllib.loads(index.read_text())

    with pytest.raises(PlanError) as exc:
        plan_refs.resolve_refs(raw, tmp_path, tmp_path, index_path=index)
    assert exc.value.ref_kind == "index"


def test_a_converted_1_based_plan_keeps_the_bytes_the_position_keyed_converter_made():
    files, report = _convert(_plan_source([1, 2], long_stage=2), field=20)

    assert {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()} == EXPECTED_ONE_BASED
    assert report["added_indices"] == []


EXPECTED_ONE_BASED: dict[str, str] = {
    "idp.toml": "ed1014b71a80f27771ec4ddc55a7f79ac148a501acbadf59329deb5b0e2f6f5e",
    "idp.parts/meta-goal.md": "43175de88991f99ca65cfd3d1208fd5cda553fb40a272e25da086cb4e43c0e22",
    "idp.parts/stage-1.toml": "7adf7d0a4672890827457460f414952bb92b23b57d4d92ac6a398055f9392ab0",
    "idp.parts/stage-2-method.md": "7998582a7aaac4b6169c14ed7531c837f8522abe5bb845d2279475298239f796",
    "idp.parts/stage-2.toml": "2a4e71a02431f887d1789905623800f4c1d1c00db53ccdef138e0cce91f85056",
}


@pytest.mark.parametrize("name", ORDERS)
def test_hand_built_the_visited_list_labels_references_by_declared_index(name, tmp_path):
    from agentctl import plan_store

    order = ORDERS[name]
    index = _hand_built(tmp_path, order)

    heads = [position[0] for position, _p, _s in plan_store.visited_refs(index)]

    assert heads == order


@pytest.mark.parametrize("name", ORDERS)
def test_hand_built_a_referenced_field_is_not_reported_as_inline_in_its_declared_stage(
        name, tmp_path):
    from agentctl import plan_store, submission
    from agentctl.plan import load_plan

    index = _hand_built(tmp_path, ORDERS[name])
    doc = load_plan(index, root=tmp_path)

    warnings = submission.plan_size_warnings(doc, plan_store.visited_positions(index))

    assert warnings == []


@pytest.mark.parametrize("name", ORDERS)
def test_hand_built_every_dispatch_view_resolves_the_pointers_it_prints(name, tmp_path):
    order = ORDERS[name]
    index = _hand_built(tmp_path / "author", order)

    for n in order:
        site = _site("dispatch-stage-brief", index, tmp_path / "views", stage=n)
        printed = _printed(site)
        wanted = _sha(_long(n))
        assert f"see objects/{wanted}" in printed, n
        for sha in SHA_IN_OBJECTS.findall(printed):
            assert (site.view_dir / "objects" / sha).is_file(), (n, sha)


@pytest.mark.parametrize("name", ORDERS)
def test_hand_built_every_pair_view_resolves_the_pointers_it_prints(name, tmp_path):
    order = ORDERS[name]
    index = _hand_built(tmp_path / "author", order)

    for later, earlier in zip(order[1:], order):
        site = _site("topo-pair", index, tmp_path / "views", pair=f"{later}-{earlier}")
        printed = _printed(site)
        for n in (later, earlier):
            assert f"see objects/{_sha(_long(n))}" in printed, (later, earlier, n)
        for sha in SHA_IN_OBJECTS.findall(printed):
            assert (site.view_dir / "objects" / sha).is_file(), (later, earlier, sha)


def _converted_tree(root: Path, order: list[int], **kw) -> Path:
    files, _ = _convert(_plan_source(order, **kw))
    return _materialize(files, root)


def _stage_file_shas(order: list[int]) -> dict[int, str]:
    files, _ = _convert(_plan_source(order))
    return {n: hashlib.sha256(files[f"idp.parts/stage-{n}.toml"]).hexdigest()
            for n in order}


@pytest.mark.parametrize("name", ORDERS)
def test_converted_visited_heads_are_the_declared_indices(name, tmp_path):
    from agentctl import plan_store

    order = ORDERS[name]
    index = _converted_tree(tmp_path, order, long_stage=order[-1])

    visited = plan_store.visited_refs(index)

    assert [p[0] for p, _path, _sha in visited if len(p) == 1] == sorted(order)
    assert [p for p, _path, _sha in visited if len(p) == 2] == [(order[-1], "method")]


@pytest.mark.parametrize("name", ORDERS)
def test_converted_every_dispatch_view_holds_its_stage_file_object(name, tmp_path):
    order = ORDERS[name]
    index = _converted_tree(tmp_path / "tree", order)
    shas = _stage_file_shas(order)

    for n in order:
        site = _site("dispatch-stage-brief", index, tmp_path / "views", stage=n)
        printed = _printed(site)
        assert (site.view_dir / "objects" / shas[n]).is_file(), n
        for sha in SHA_IN_OBJECTS.findall(printed):
            assert (site.view_dir / "objects" / sha).is_file(), (n, sha)


@pytest.mark.parametrize("name", ORDERS)
def test_converted_every_pair_view_holds_its_stage_file_objects(name, tmp_path):
    order = ORDERS[name]
    index = _converted_tree(tmp_path / "tree", order)
    shas = _stage_file_shas(order)

    for later, earlier in zip(order[1:], order):
        site = _site("topo-pair", index, tmp_path / "views", pair=f"{later}-{earlier}")
        printed = _printed(site)
        for n in (later, earlier):
            assert (site.view_dir / "objects" / shas[n]).is_file(), (later, earlier, n)
        for sha in SHA_IN_OBJECTS.findall(printed):
            assert (site.view_dir / "objects" / sha).is_file(), (later, earlier, sha)


def test_converted_size_warning_lands_under_the_declared_stage_only(tmp_path):
    import plan_site_fixture as fx
    from agentctl import plan_store, submission
    from agentctl.config import plan_field_inline_limit
    from agentctl.plan import load_plan

    limit = plan_field_inline_limit()
    text = _meta()
    for n, deps in _chain([1, 2, 14, 3]):
        body = {14: "e" * (limit + 500), 3: "i" * (limit + 10)}.get(n, f"method of {n}")
        text += _stage(n, deps, method=f'"""\n{body}\n"""')
    files, _ = _convert(text.encode(), field=limit + 100)
    index = _materialize(files, tmp_path)
    assert "idp.parts/stage-14-method.md" in files
    doc = load_plan(index, root=tmp_path)

    positions = plan_store.visited_positions(index)
    warned = [(w["node"], w["field"]) for w in submission.plan_size_warnings(doc, positions)]
    verdicts = fx.load_script("plan-fit-report.py", "plan_fit_report_for_identity").lint_by_node(
        doc, positions)

    assert warned == [(3, "method")]
    assert {node: v for node, v in verdicts.items() if v != "ok"} == {"3": "warn"}


def test_a_duplicate_stage_index_is_refused_by_the_converter():
    from agentctl.plan import PlanError

    source = (_meta() + _stage(1, [], method='"a"') + _stage(1, [], method='"b"')).encode()

    with pytest.raises(PlanError, match="stage index 1 is declared twice"):
        _convert(source)
