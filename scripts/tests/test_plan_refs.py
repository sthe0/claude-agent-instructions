"""Hash-pinned references in a plan (ADR-0011): the resolver, the Merkle root digest,
the stage closure digest and the verbs `plan-digest` / `plan-externalize` / `plan-seal`.

Every plan here is synthetic. Expected values come from the ADR's rules and from plain
sha256 of file bytes -- never from what the code under test returned -- and the names
under test are imported inside the test bodies, so a tree without the feature fails each
test by assertion or ImportError rather than at collection.
"""
from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path

import pytest

from conftest import SUBSTANTIVE_ORDER

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HEX_A = "a" * 64


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _ref(d: Path, rel: str, text: str, *, held_in: str = ".") -> str:
    """Write `text` at `d/rel` and return the inline reference table to it, spelled
    relative to the directory `d/held_in` of the file that will carry the reference."""
    _write(d / rel, text)
    return f'{{ ref = "{os.path.relpath(rel, held_in)}", sha256 = "{_sha(text)}" }}'


class _Array(str):
    """An inline `stage = [...]` array. TOML reads a top-level key written after a table
    header as a key of that table, so it has to come before `[meta]`."""


class _Text(str):
    def __add__(self, other):
        if isinstance(other, _Array):
            return _Text(str.__add__(other, "\n" + self))
        return _Text(str.__add__(self, other))


def _meta(**over) -> str:
    fields = {
        "task_id": '"demo-refs"',
        "goal": '"exercise references"',
        "done_criterion": '"all stages PASSED"',
        "criterion_type": '"measurable"',
    }
    fields.update(over)
    return _Text("[meta]\n" + "".join(f"{k} = {v}\n" for k, v in fields.items() if v is not None))


def _stage_fields(i: int, deps: str = "[]", **over) -> str:
    fields = {
        "index": str(i),
        "title": f'"Stage {i}"',
        "executor": '"spawn:developer"',
        "expected_result_image": f'"image {i}"',
        "criterion_type": '"measurable"',
        "done_criterion": f'"stage {i} done"',
        "depends_on": deps,
        "output_artifacts": f'["s{i}.py"]',
    }
    fields.update(over)
    return "".join(f"{k} = {v}\n" for k, v in fields.items() if v is not None)


def _stage(i: int, deps: str = "[]", **over) -> str:
    return "[[stage]]\n" + _stage_fields(i, deps, **over)


def _plan(d: Path, meta: str, *stages: str, tail: str = "", name: str = "plan.toml") -> Path:
    return _write(d / name, meta + "\n" + "\n".join(stages) + "\n" + tail)


def _stage_array(*refs: str) -> str:
    return _Array("stage = [\n" + ",\n".join(f"  {r}" for r in refs) + "\n]\n")


def _resolve(index: Path, **kw):
    from agentctl import plan_refs

    raw = tomllib.loads(index.read_text(encoding="utf-8"))
    root = kw.pop("root", index.parent)
    return plan_refs.resolve_refs(raw, index.parent, root, index_path=index, **kw)


def _refusal(index: Path, **kw):
    from agentctl.plan import PlanError

    with pytest.raises(PlanError) as exc:
        _resolve(index, **kw)
    return exc.value


def _tree(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def _two_stage(d: Path) -> Path:
    return _plan(d, _meta(), _stage(1), _stage(2, "[1]"))


# --- resolution -------------------------------------------------------------------

def test_a_string_reference_is_replaced_by_the_files_exact_text(tmp_path):
    text = "  line one  \n\n\tindented é\n"
    ref = _ref(tmp_path, "parts/image.md", text)
    index = _plan(tmp_path, _meta(), _stage(1, expected_result_image=ref))

    resolved, visited = _resolve(index)

    assert resolved["stage"][0]["expected_result_image"] == text
    assert [(p, h) for p, _, h in visited] == [((1, "expected_result_image"), _sha(text))]


def test_references_resolve_in_meta_order_and_stage_positions(tmp_path):
    goal = _ref(tmp_path, "parts/goal.md", "the goal\n")
    ctx = _ref(tmp_path, "parts/ctx.md", "some context\n")
    place = _ref(tmp_path, "parts/place.md", "the place\n")
    stage_body = _stage_fields(
        1, method=_ref(tmp_path, "parts/method.md", "do it\n", held_in="parts"))
    _write(tmp_path / "parts" / "stage-1.toml", stage_body)
    index = _write(
        tmp_path / "plan.toml",
        _meta(goal=goal, context=ctx)
        + '\n[meta.order]\ncustomer_id = "user"\n'
        + f"functional_place = {place}\n\n"
        + f'[[stage]]\nref = "parts/stage-1.toml"\nsha256 = "{_sha(stage_body)}"\n',
    )

    resolved, visited = _resolve(index)

    assert resolved["meta"]["goal"] == "the goal\n"
    assert resolved["meta"]["context"] == "some context\n"
    assert resolved["meta"]["order"]["functional_place"] == "the place\n"
    assert resolved["stage"][0]["method"] == "do it\n"
    assert resolved["stage"][0]["title"] == "Stage 1"
    assert [p for p, _, _ in visited] == [
        ("meta", "goal"), ("meta", "context"), ("meta.order", "functional_place"),
        (1,), (1, "method"),
    ]


def test_visited_nests_and_names_the_confined_resolved_path_and_declared_hash(tmp_path):
    from agentctl import plan_refs

    method = "stage three method\n"
    stage_body = _stage_fields(
        3, method=_ref(tmp_path, "plan.parts/m.md", method, held_in="plan.parts"))
    _write(tmp_path / "plan.parts" / "stage-3.toml", stage_body)
    raw = tomllib.loads(_meta() + "\n" + _stage(1) + "\n" + _stage(2))
    raw["stage"].append({"ref": "plan.parts/stage-3.toml", "sha256": _sha(stage_body)})

    resolved, visited = plan_refs.resolve_refs(raw, tmp_path, tmp_path)

    assert resolved["stage"][2]["method"] == method
    assert visited == [
        ((3,), (tmp_path / "plan.parts" / "stage-3.toml").resolve(), _sha(stage_body)),
        ((3, "method"), (tmp_path / "plan.parts" / "m.md").resolve(), _sha(method)),
    ]


def test_a_referenced_stage_files_own_references_resolve_against_its_directory(tmp_path):
    body = _stage_fields(1, method=_ref(tmp_path, "sub/dir/m.md", "nested\n", held_in="sub/dir"))
    ref = _ref(tmp_path, "sub/dir/stage-1.toml", body)
    index = _write(tmp_path / "plan.toml", _meta() + "\n" + _stage_array(ref))

    resolved, _ = _resolve(index)

    assert resolved["stage"][0]["method"] == "nested\n"


def test_a_stage_reference_resolving_to_a_reference_is_followed_in_the_same_position(tmp_path):
    real = _stage_fields(1)
    _write(tmp_path / "c2.toml", real)
    c1 = f'ref = "c2.toml"\nsha256 = "{_sha(real)}"\n'
    _write(tmp_path / "c1.toml", c1)
    index = _write(
        tmp_path / "plan.toml",
        _meta() + "\n" + _stage_array(f'{{ ref = "c1.toml", sha256 = "{_sha(c1)}" }}'),
    )

    resolved, visited = _resolve(index)

    assert resolved["stage"][0]["title"] == "Stage 1"
    assert [(p, h) for p, _, h in visited] == [((1,), _sha(c1)), ((1,), _sha(real))]


def _chain(d: Path, links: int) -> Path:
    """An index whose single stage sits at the end of `links` nested references."""
    text = _stage_fields(1)
    _write(d / f"c{links}.toml", text)
    for k in range(links - 1, 0, -1):
        nxt = f'ref = "c{k + 1}.toml"\nsha256 = "{_sha(text)}"\n'
        _write(d / f"c{k}.toml", nxt)
        text = nxt
    return _write(
        d / "plan.toml",
        _meta() + "\n" + _stage_array(f'{{ ref = "c1.toml", sha256 = "{_sha(text)}" }}'),
    )


def test_a_chain_of_eight_resolves_and_nine_is_refused_as_depth(tmp_path):
    ok = _chain(tmp_path / "ok", 8)
    resolved, visited = _resolve(ok)
    assert resolved["stage"][0]["index"] == 1
    assert len(visited) == 8

    err = _refusal(_chain(tmp_path / "deep", 9))
    assert err.ref_kind == "depth"
    assert "c9.toml" in str(err)


def test_chain_links_count_toward_the_depth_of_a_reference_inside_the_stage(tmp_path):
    d = tmp_path / "x"
    text = _stage_fields(1, method=_ref(d, "m.md", "late\n"))
    _write(d / "c8.toml", text)
    for k in range(7, 0, -1):
        text = f'ref = "c{k + 1}.toml"\nsha256 = "{_sha(text)}"\n'
        _write(d / f"c{k}.toml", text)
    index = _write(
        d / "plan.toml",
        _meta() + "\n" + _stage_array(f'{{ ref = "c1.toml", sha256 = "{_sha(text)}" }}'),
    )

    err = _refusal(index)

    assert err.ref_kind == "depth"
    assert "m.md" in str(err)


def test_a_cycle_inside_the_depth_bound_is_reported_as_a_cycle(tmp_path):
    b = f'ref = "a.toml"\nsha256 = "{HEX_A}"\n'
    _write(tmp_path / "b.toml", b)
    a = f'ref = "b.toml"\nsha256 = "{_sha(b)}"\n'
    _write(tmp_path / "a.toml", a)
    index = _write(
        tmp_path / "plan.toml",
        _meta() + "\n" + _stage_array(f'{{ ref = "a.toml", sha256 = "{_sha(a)}" }}'),
    )

    err = _refusal(index)

    assert err.ref_kind == "cycle"
    assert "a.toml" in str(err)


def test_a_reference_back_to_the_index_is_a_cycle(tmp_path):
    body = _stage_fields(1, method=f'{{ ref = "plan.toml", sha256 = "{HEX_A}" }}')
    ref = _ref(tmp_path, "stage-1.toml", body)
    index = _write(tmp_path / "plan.toml", _meta() + "\n" + _stage_array(ref))

    err = _refusal(index)

    assert err.ref_kind == "cycle"
    assert "plan.toml" in str(err)


def test_a_missing_file_is_refused_as_missing_and_names_the_path(tmp_path):
    ref = f'{{ ref = "parts/gone.md", sha256 = "{HEX_A}" }}'
    index = _plan(tmp_path, _meta(), _stage(1, method=ref))

    err = _refusal(index)

    assert err.ref_kind == "missing"
    assert "parts/gone.md" in str(err)
    assert str(tmp_path / "parts" / "gone.md") in str(err)


def test_a_hash_mismatch_names_both_hashes(tmp_path):
    ref = _ref(tmp_path, "m.md", "what is on disk\n")
    index = _plan(tmp_path, _meta(), _stage(1, method=ref.replace(_sha("what is on disk\n"), HEX_A)))

    err = _refusal(index)

    assert err.ref_kind == "mismatch"
    assert HEX_A in str(err)
    assert _sha("what is on disk\n") in str(err)
    assert "m.md" in str(err)


@pytest.mark.parametrize("ref_literal", [
    '{ ref = "m.md" }',
    f'{{ sha256 = "{HEX_A}" }}',
    '{ ref = "m.md", sha256 = "ABC" }',
    f'{{ ref = "m.md", sha256 = "{"A" * 64}" }}',
    f'{{ ref = "m.md", sha256 = "{"a" * 63}" }}',
    f'{{ ref = "", sha256 = "{HEX_A}" }}',
    f'{{ ref = 3, sha256 = "{HEX_A}" }}',
    '{ ref = "m.md", sha256 = 7 }',
])
def test_a_malformed_reference_is_refused_as_malformed(tmp_path, ref_literal):
    index = _plan(tmp_path, _meta(), _stage(1, method=ref_literal))

    assert _refusal(index).ref_kind == "malformed"


@pytest.mark.parametrize("ref_literal", [
    '{ ref = "m.md" }',
    f'{{ sha256 = "{HEX_A}" }}',
    '{ ref = "m.md", sha256 = "ABC" }',
    f'{{ ref = "m.md", sha256 = "{"A" * 64}" }}',
    f'{{ ref = "m.md", sha256 = "{"a" * 63}" }}',
    f'{{ ref = "", sha256 = "{HEX_A}" }}',
    f'{{ ref = 3, sha256 = "{HEX_A}" }}',
    '{ ref = "m.md", sha256 = 7 }',
])
def test_a_malformed_whole_stage_reference_is_refused_as_malformed(tmp_path, ref_literal):
    _write(tmp_path / "m.md", "a file that exists\n")
    index = _write(tmp_path / "plan.toml", _meta() + "\n" + _stage_array(ref_literal))

    assert _refusal(index).ref_kind == "malformed"


def test_a_malformed_whole_stage_reference_is_refused_by_the_sealer_too(tmp_path):
    from agentctl import plan_refs
    from agentctl.plan import PlanError

    _write(tmp_path / "m.md", "a file that exists\n")
    index = _write(tmp_path / "plan.toml", _meta() + "\n" + _stage_array('{ ref = "m.md" }'))

    with pytest.raises(PlanError) as exc:
        plan_refs.seal(index)

    assert exc.value.ref_kind == "malformed"


def test_a_parsed_stage_file_that_is_a_malformed_reference_is_refused_as_malformed(tmp_path):
    link = _ref(tmp_path, "s.toml", 'ref = "m.md"\n')
    index = _write(tmp_path / "plan.toml", _meta() + "\n" + _stage_array(link))

    assert _refusal(index).ref_kind == "malformed"


def test_an_absolute_reference_path_escapes_even_inside_the_root(tmp_path):
    target = _write(tmp_path / "m.md", "x\n")
    ref = f'{{ ref = "{target}", sha256 = "{_sha("x" + chr(10))}" }}'
    index = _plan(tmp_path, _meta(), _stage(1, method=ref))

    err = _refusal(index)

    assert err.ref_kind == "escape"
    assert str(target) in str(err)


def test_a_parent_traversal_out_of_the_root_escapes(tmp_path):
    root = tmp_path / "root"
    _write(tmp_path / "outside.md", "x\n")
    ref = f'{{ ref = "../outside.md", sha256 = "{_sha("x" + chr(10))}" }}'
    index = _plan(root, _meta(), _stage(1, method=ref))

    err = _refusal(index)

    assert err.ref_kind == "escape"
    assert "outside.md" in str(err)


def test_a_symlink_pointing_out_of_the_root_escapes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = _write(tmp_path / "outside" / "x.md", "secret\n")
    (root / "link.md").symlink_to(outside)
    ref = f'{{ ref = "link.md", sha256 = "{_sha("secret" + chr(10))}" }}'
    index = _plan(root, _meta(), _stage(1, method=ref))

    err = _refusal(index)

    assert err.ref_kind == "escape"
    assert "link.md" in str(err)


def test_the_root_may_be_wider_than_the_index_directory(tmp_path):
    _write(tmp_path / "shared" / "m.md", "shared text\n")
    ref = f'{{ ref = "../shared/m.md", sha256 = "{_sha("shared text" + chr(10))}" }}'
    index = _plan(tmp_path / "plans", _meta(), _stage(1, method=ref))

    resolved, _ = _resolve(index, root=tmp_path)

    assert resolved["stage"][0]["method"] == "shared text\n"


@pytest.mark.parametrize("where", [
    "index", "depends_on", "output_artifacts", "grants", "supplies", "guard_exempt_paths",
])
def test_a_reference_in_a_non_string_stage_field_is_refused_as_position(tmp_path, where):
    ref = _ref(tmp_path, "m.md", "1\n")
    literal = {
        "index": ref,
        "depends_on": f"[{ref}]",
        "output_artifacts": f"[{ref}]",
        "grants": f"{{ allow = [{ref}] }}",
        "supplies": f"[{{ on = {ref} }}]",
        "guard_exempt_paths": f"[{ref}]",
    }[where]
    index = _plan(tmp_path, _meta(), _stage(1, **{where: literal}))

    err = _refusal(index)

    assert err.ref_kind == "position"
    assert "m.md" in str(err)


def test_a_reference_in_a_plan_level_table_other_than_meta_or_stage_is_position(tmp_path):
    ref = _ref(tmp_path, "m.md", "true\n")
    index = _plan(
        tmp_path, _meta(), _stage(1),
        tail=f"[[final_check]]\ncommand = {ref}\nexpected_exit = 0\n",
    )

    assert _refusal(index).ref_kind == "position"


def test_a_reference_nested_in_an_ordinary_table_is_position_not_data(tmp_path):
    ref = _ref(tmp_path, "m.md", "1\n")
    index = _plan(tmp_path, _meta(), _stage(1, method=f'{{ note = "x", inner = {ref} }}'))

    assert _refusal(index).ref_kind == "position"


def test_a_reference_in_the_orders_non_string_part_is_position(tmp_path):
    ref = _ref(tmp_path, "m.md", "1\n")
    index = _plan(
        tmp_path, _meta(), _stage(1),
        tail='[meta.order]\ncustomer_id = "user"\n\n[meta.order.coverage]\n'
             f"R1 = [{ref}]\n",
    )

    assert _refusal(index).ref_kind == "position"


def test_ordinary_tables_are_data_and_left_alone(tmp_path):
    from agentctl import plan_refs

    raw = {
        "meta": {"goal": "g"},
        "stage": [{"index": 1, "grants": {}, "extra": {"ref": "a", "sha256": HEX_A, "more": 1}}],
    }

    resolved, visited = plan_refs.resolve_refs(raw, tmp_path, tmp_path)

    assert resolved == raw
    assert visited == []


def test_a_referenced_stage_whose_index_disagrees_with_its_position_is_refused(tmp_path):
    ref = _ref(tmp_path, "stage-1.toml", _stage_fields(5))
    index = _write(tmp_path / "plan.toml", _meta() + "\n" + _stage_array(ref))

    err = _refusal(index)

    assert err.ref_kind == "index"
    assert "stage-1.toml" in str(err)


def test_a_referenced_stage_without_an_index_key_loads(tmp_path):
    from agentctl.plan import load_plan

    ref = _ref(tmp_path, "stage-1.toml", _stage_fields(1, index=None))
    index = _write(tmp_path / "plan.toml", _meta() + "\n" + _stage_array(ref))

    doc = load_plan(index)

    assert [s.index for s in doc.stages] == [1]


def test_a_stage_reference_may_be_written_as_an_array_of_tables_beside_inline_stages(tmp_path):
    from agentctl.plan import load_plan

    body = _stage_fields(2, "[1]")
    _write(tmp_path / "stage-2.toml", body)
    index = _write(
        tmp_path / "plan.toml",
        _meta() + "\n" + _stage(1)
        + f'\n[[stage]]\nref = "stage-2.toml"\nsha256 = "{_sha(body)}"\n',
    )

    doc = load_plan(index)

    assert [(s.index, s.title) for s in doc.stages] == [(1, "Stage 1"), (2, "Stage 2")]


# --- the reader hook --------------------------------------------------------------

def test_a_custom_reader_serves_bytes_for_a_path_that_is_not_on_disk(tmp_path):
    text = "served from the store\n"
    ref = _ref(tmp_path, "parts/m.md", text)
    index = _plan(tmp_path, _meta(), _stage(1, method=ref))
    (tmp_path / "parts" / "m.md").unlink()
    calls = []

    def read(path, sha256):
        calls.append((path, sha256))
        return text.encode("utf-8")

    resolved, _ = _resolve(index, read=read)

    assert resolved["stage"][0]["method"] == text
    assert calls == [((tmp_path / "parts" / "m.md").resolve(), _sha(text))]


def test_the_hash_check_applies_to_what_the_reader_returns(tmp_path):
    ref = _ref(tmp_path, "m.md", "right\n")
    index = _plan(tmp_path, _meta(), _stage(1, method=ref))

    err = _refusal(index, read=lambda path, sha256: b"wrong\n")

    assert err.ref_kind == "mismatch"


def test_a_plan_error_raised_by_the_reader_propagates_unchanged(tmp_path):
    from agentctl.plan import PlanError

    ref = _ref(tmp_path, "m.md", "x\n")
    index = _plan(tmp_path, _meta(), _stage(1, method=ref))
    boom = PlanError("the store has no such object")

    def read(path, sha256):
        raise boom

    assert _refusal(index, read=read) is boom


# --- loading ----------------------------------------------------------------------

def test_every_fixture_plan_resolves_to_itself_with_nothing_visited(tmp_path):
    from agentctl import plan_refs

    seen = 0
    for path in sorted(FIXTURES.glob("*.toml")):
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            continue
        resolved, visited = plan_refs.resolve_refs(raw, path.parent, path.parent)
        assert resolved == raw, path.name
        assert visited == [], path.name
        seen += 1
    assert seen


def test_a_single_file_plan_loads_and_digests_as_before(tmp_path):
    from agentctl.plan import load_plan, load_plan_with_digest, parse_plan

    index = _plan(
        tmp_path, _meta(), _stage(1, grants="{}"), _stage(2, "[1]"),
        tail='[[stage.effects]]\npath = "e.txt"\nsha256 = "' + HEX_A + '"\nresolver = "file"\n',
    )
    data = index.read_bytes()

    doc = load_plan(index)
    again, got_bytes, digest = load_plan_with_digest(index)

    assert doc == parse_plan(tomllib.loads(data.decode("utf-8")))
    assert again == doc
    assert got_bytes == data
    assert digest == hashlib.sha256(data).hexdigest()


def test_the_root_digest_is_the_sha256_of_the_index_bytes(tmp_path):
    from agentctl.plan import load_plan_with_digest

    index = _plan(tmp_path, _meta(), _stage(1, method=_ref(tmp_path, "m.md", "m\n")))

    _, data, digest = load_plan_with_digest(index)

    assert digest == hashlib.sha256(index.read_bytes()).hexdigest()
    assert data == index.read_bytes()


def test_load_plan_accepts_a_custom_reader_and_an_explicit_root(tmp_path):
    from agentctl.plan import load_plan

    text = "from a store\n"
    ref = _ref(tmp_path / "plans", "m.md", text)
    index = _plan(tmp_path / "plans", _meta(), _stage(1, method=ref))
    (tmp_path / "plans" / "m.md").unlink()

    doc = load_plan(index, root=tmp_path, base_dir=tmp_path / "plans",
                    read=lambda path, sha256: text.encode("utf-8"))

    assert doc.stages[0].means.method == text


def test_editing_a_referenced_file_without_resealing_is_refused_until_sealed(tmp_path):
    from agentctl import plan_refs
    from agentctl.plan import PlanError, load_plan, load_plan_with_digest

    ref = _ref(tmp_path, "parts/m.md", "before\n")
    index = _plan(tmp_path, _meta(), _stage(1, method=ref))
    _, _, root_before = load_plan_with_digest(index)
    _write(tmp_path / "parts" / "m.md", "after\n")

    with pytest.raises(PlanError) as exc:
        load_plan(index)
    assert exc.value.ref_kind == "mismatch"

    plan_refs.seal(index)

    assert load_plan(index).stages[0].means.method == "after\n"
    assert load_plan_with_digest(index)[2] != root_before
    assert f'sha256 = "{_sha("after" + chr(10))}"' in index.read_text()


def test_sealing_rewrites_only_the_hash_values(tmp_path):
    from agentctl import plan_refs

    ref = _ref(tmp_path, "parts/m.md", "before\n")
    index = _plan(tmp_path, _meta(), _stage(1, method=ref))
    old = index.read_text()
    _write(tmp_path / "parts" / "m.md", "after\n")

    plan_refs.seal(index)

    assert index.read_text() == old.replace(_sha("before\n"), _sha("after\n"))


def test_sealing_a_two_level_chain_changes_the_root_when_the_leaf_changes(tmp_path):
    from agentctl import plan_refs
    from agentctl.plan import load_plan, load_plan_with_digest

    inner = _ref(tmp_path, "plan.parts/m.md", "leaf v1\n", held_in="plan.parts")
    body = _stage_fields(1, method=inner)
    outer = _ref(tmp_path, "plan.parts/stage-1.toml", body)
    index = _write(tmp_path / "plan.toml", _meta() + "\n" + _stage_array(outer))
    root_before = load_plan_with_digest(index)[2]
    stage_file_before = (tmp_path / "plan.parts" / "stage-1.toml").read_bytes()
    _write(tmp_path / "plan.parts" / "m.md", "leaf v2\n")

    plan_refs.seal(index)

    assert (tmp_path / "plan.parts" / "stage-1.toml").read_bytes() != stage_file_before
    assert load_plan(index).stages[0].means.method == "leaf v2\n"
    assert load_plan_with_digest(index)[2] != root_before


def test_a_stage_chain_loads_through_load_plan(tmp_path):
    from agentctl.plan import load_plan

    assert [s.index for s in load_plan(_chain(tmp_path, 3)).stages] == [1]


# --- naming and tables ------------------------------------------------------------

def test_path_helpers_follow_the_documented_layout():
    from agentctl import plan_refs

    assert plan_refs.field_file_relpath("plan", 3, "method") == "plan.parts/stage-3-method.md"
    assert plan_refs.field_file_relpath("plan", "meta", "goal") == "plan.parts/meta-goal.md"
    assert (plan_refs.field_file_relpath("plan", "meta.order", "customer")
            == "plan.parts/meta-order-customer.md")
    assert plan_refs.stage_file_relpath("plan", 3) == "plan.parts/stage-3.toml"


def test_write_field_file_writes_the_exact_bytes_and_returns_their_hash(tmp_path):
    from agentctl import plan_refs

    text = "  keep é \n\n"
    digest = plan_refs.write_field_file(tmp_path / "deep" / "f.md", text)

    assert (tmp_path / "deep" / "f.md").read_bytes() == text.encode("utf-8")
    assert digest == _sha(text)


def test_field_tables_separate_string_fields_from_structured_ones():
    from agentctl import plan_refs

    for kind in ("stage", "meta", "order"):
        assert plan_refs.EXTERNALIZABLE_FIELDS[kind]
        assert not (plan_refs.EXTERNALIZABLE_FIELDS[kind] & plan_refs.NON_STRING_FIELDS[kind])
    assert {"method", "expected_result_image", "done_criterion"} <= plan_refs.EXTERNALIZABLE_FIELDS["stage"]
    assert {"index", "depends_on", "supplies", "grants", "output_artifacts"} <= plan_refs.NON_STRING_FIELDS["stage"]
    assert {"goal", "done_criterion"} <= plan_refs.EXTERNALIZABLE_FIELDS["meta"]
    assert "order" in plan_refs.NON_STRING_FIELDS["meta"]
    assert {"functional_place", "customer"} <= plan_refs.EXTERNALIZABLE_FIELDS["order"]
    assert "requirements" in plan_refs.NON_STRING_FIELDS["order"]


def _keys_read_by_parse_plan(variable: str) -> set[str]:
    import ast

    source = (Path(__file__).resolve().parents[1] / "agentctl" / "plan.py").read_text("utf-8")
    keys = set()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == variable and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == variable and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            keys.add(node.slice.value)
    return keys


def test_the_field_tables_cover_exactly_the_keys_the_plan_schema_reads():
    import dataclasses

    from agentctl import plan_refs
    from agentctl.state import Order

    # `s.get("derived")` in plan.py reads a grant-entry table, not a [[stage]] key.
    stage_keys = _keys_read_by_parse_plan("s") - {"derived"}
    meta_keys = _keys_read_by_parse_plan("m")
    order_keys = {f.name for f in dataclasses.fields(Order)} - {"malformed", "requirements_dropped"}
    for kind, schema in (("stage", stage_keys), ("meta", meta_keys), ("order", order_keys)):
        classified = plan_refs.EXTERNALIZABLE_FIELDS[kind] | plan_refs.NON_STRING_FIELDS[kind]
        assert classified == schema, (kind, sorted(classified ^ schema))


def test_a_reference_standing_directly_at_a_non_string_key_is_refused_as_position(tmp_path):
    from agentctl import plan_refs

    cases = [(kind, key) for kind in ("stage", "meta", "order")
             for key in sorted(plan_refs.NON_STRING_FIELDS[kind])]
    for n, (kind, key) in enumerate(cases):
        d = tmp_path / f"case{n}"
        ref = _ref(d, "m.md", "1\n")
        if kind == "stage":
            index = _plan(d, _meta(), _stage(1, **{key: ref}))
        elif kind == "meta":
            index = _plan(d, _meta(**{key: ref}), _stage(1))
        else:
            index = _plan(d, _meta(), _stage(1), tail=f"[meta.order]\n{key} = {ref}\n")

        err = _refusal(index)

        assert err.ref_kind == "position", (kind, key)
        assert "m.md" in str(err), (kind, key)


# --- equivalence of an externalized plan -----------------------------------------

def _keys(doc):
    from agentctl.plan import stage_element_keys

    return {s.index: stage_element_keys(s) for s in doc.stages}


def _inline_and_external(tmp_path):
    """The same plan twice: everything inline, and with a stage field, a meta field, an
    order field and a whole stage held in referenced files."""
    inline = tmp_path / "inline"
    ext = tmp_path / "ext"
    meta_inline = _meta(goal='"the goal text"') + SUBSTANTIVE_ORDER
    inline_plan = _plan(inline, meta_inline, _stage(1, method='"do stage one"'),
                        _stage(2, "[1]", done_criterion='"second done"'))

    goal = _ref(ext, "plan.parts/meta-goal.md", "the goal text")
    order_meta = SUBSTANTIVE_ORDER.replace(
        'functional_place = "the norm governing an act of activity, in a test"',
        "functional_place = "
        + _ref(ext, "plan.parts/meta-order-functional_place.md",
               "the norm governing an act of activity, in a test"))
    method = _ref(ext, "plan.parts/stage-1-method.md", "do stage one", held_in="plan.parts")
    stage1 = _ref(ext, "plan.parts/stage-1.toml", _stage_fields(1, method=method))
    stage2 = _ref(
        ext, "plan.parts/stage-2.toml", _stage_fields(2, "[1]", done_criterion='"second done"'))
    ext_plan = _write(
        ext / "plan.toml",
        _meta(goal=goal) + order_meta + "\n" + _stage_array(stage1, stage2),
    )
    return inline_plan, ext_plan


def test_an_externalized_plan_gives_the_same_plandoc_and_element_keys(tmp_path):
    from agentctl.plan import load_plan

    inline_plan, ext_plan = _inline_and_external(tmp_path)

    doc_inline, doc_ext = load_plan(inline_plan), load_plan(ext_plan)

    assert doc_ext == doc_inline
    assert _keys(doc_ext) == _keys(doc_inline)


def test_the_content_digest_is_unchanged_by_externalization(tmp_path):
    from agentctl.plan import load_plan, plan_content_digest

    inline_plan, ext_plan = _inline_and_external(tmp_path)

    assert plan_content_digest(load_plan(ext_plan)) == plan_content_digest(load_plan(inline_plan))


# --- the stage closure digest -----------------------------------------------------

def _closure_plan(d: Path, *, done: dict[int, str] | None = None) -> Path:
    done = done or {}
    return _plan(
        d, _meta(),
        _stage(1, done_criterion=f'"{done.get(1, "stage 1 done")}"'),
        _stage(2, "[1]", done_criterion=f'"{done.get(2, "stage 2 done")}"'),
        _stage(3, done_criterion=f'"{done.get(3, "stage 3 done")}"')
        + "\n[[stage.supplies]]\non = 2\n",
        _stage(4, done_criterion=f'"{done.get(4, "stage 4 done")}"'),
    )


def _closures(path: Path) -> dict[int, str]:
    from agentctl.plan import load_plan, stage_closure_digest

    doc = load_plan(path)
    return {s.index: stage_closure_digest(doc, s.index) for s in doc.stages}


def test_the_closure_digest_moves_for_the_stage_and_everything_relying_on_it(tmp_path):
    base = _closures(_closure_plan(tmp_path / "a"))

    leaf = _closures(_closure_plan(tmp_path / "b", done={1: "changed"}))
    assert {i for i in base if base[i] != leaf[i]} == {1, 2, 3}

    mid = _closures(_closure_plan(tmp_path / "c", done={2: "changed"}))
    assert {i for i in base if base[i] != mid[i]} == {2, 3}

    solo = _closures(_closure_plan(tmp_path / "d", done={4: "changed"}))
    assert {i for i in base if base[i] != solo[i]} == {4}


def test_the_closure_digest_is_unchanged_by_externalization(tmp_path):
    from agentctl.plan import load_plan, stage_closure_digest

    inline_plan, ext_plan = _inline_and_external(tmp_path)
    a, b = load_plan(inline_plan), load_plan(ext_plan)

    assert [stage_closure_digest(a, n) for n in (1, 2)] == [stage_closure_digest(b, n) for n in (1, 2)]


def test_a_supplies_only_cycle_is_the_closures_own_plan_error(tmp_path):
    from agentctl.plan import PlanError, load_plan, stage_closure_digest
    from agentctl.state import Supply

    doc = load_plan(_two_stage(tmp_path))
    doc.stages[0].supplies = [Supply(on=2)]
    doc.stages[1].supplies = [Supply(on=1)]

    with pytest.raises(PlanError, match="reliance cycle detected"):
        stage_closure_digest(doc, 1)


# --- the verbs --------------------------------------------------------------------

def _run(tmp_path, capsys, *argv):
    from agentctl import cli

    rc = cli.main(["--state-root", str(tmp_path / "state"), *argv])
    out = json.loads(capsys.readouterr().out)
    return rc, out


def _no_session_state(tmp_path):
    state = tmp_path / "state"
    return not state.exists() or not any(state.iterdir())


def test_the_new_verbs_are_registered_and_listed_in_help(capsys):
    from agentctl import cli

    for verb in ("plan-digest", "plan-externalize", "plan-seal"):
        assert verb in cli.COMMANDS
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    listed = capsys.readouterr().out
    for verb in ("plan-digest", "plan-externalize", "plan-seal"):
        assert verb in listed


def test_plan_digest_reports_the_root_every_reference_and_each_closure_and_writes_nothing(
        tmp_path, capsys):
    from agentctl.plan import load_plan, stage_closure_digest

    method = "stage two method\n"
    s1 = _ref(tmp_path, "plan.parts/stage-1.toml", _stage_fields(1))
    s2_body = _stage_fields(
        2, "[1]",
        method=_ref(tmp_path, "plan.parts/stage-2-method.md", method, held_in="plan.parts"))
    s2 = _ref(tmp_path, "plan.parts/stage-2.toml", s2_body)
    index = _write(tmp_path / "plan.toml", _meta() + "\n" + _stage_array(s1, s2))
    before = _tree(tmp_path)

    rc, out = _run(tmp_path, capsys, "plan-digest", "--plan", str(index), "--format", "json")

    assert rc == 0 and out["ok"]
    data = out["data"]
    assert data["root_digest"] == hashlib.sha256(index.read_bytes()).hexdigest()
    assert sorted(r["sha256"] for r in data["refs"]) == sorted(
        [_sha(_stage_fields(1)), _sha(s2_body), _sha(method)])
    doc = load_plan(index)
    assert data["closure_digests"] == {
        str(n): stage_closure_digest(doc, n) for n in (1, 2)}
    assert _tree(tmp_path) == before
    assert _no_session_state(tmp_path)

    rc, out = _run(tmp_path, capsys, "plan-digest", "--plan", str(index), "--stage", "2",
                   "--format", "json")
    assert rc == 0
    assert set(out["data"]["closure_digests"]) == {"2"}
    assert _tree(tmp_path) == before


def test_plan_digest_refuses_a_tampered_reference(tmp_path, capsys):
    ref = _ref(tmp_path, "m.md", "before\n")
    index = _plan(tmp_path, _meta(), _stage(1, method=ref))
    _write(tmp_path / "m.md", "after\n")

    rc, out = _run(tmp_path, capsys, "plan-digest", "--plan", str(index))

    assert rc == 1 and not out["ok"]
    assert "m.md" in out["detail"]


def _external_line_diff(before: str, after: str) -> tuple[list[str], list[str]]:
    old, new = before.splitlines(), after.splitlines()
    return [l for l in old if l not in new], [l for l in new if l not in old]


def test_externalizing_a_stage_field_replaces_only_that_fields_lines(tmp_path, capsys):
    from agentctl.plan import load_plan

    index = _plan(tmp_path, _meta(), _stage(1, method='"do the work"'), _stage(2, "[1]"))
    before = index.read_text()
    doc_before = load_plan(index)

    rc, out = _run(tmp_path, capsys, "plan-externalize", "--plan", str(index),
                   "--stage", "1", "--field", "method")

    assert rc == 0, out
    field_file = tmp_path / "plan.parts" / "stage-1-method.md"
    assert field_file.read_bytes() == b"do the work"
    removed, added = _external_line_diff(before, index.read_text())
    assert removed == ['method = "do the work"']
    assert added == [f'method = {{ ref = "plan.parts/stage-1-method.md", sha256 = "{_sha("do the work")}" }}']
    assert load_plan(index) == doc_before
    assert _no_session_state(tmp_path)


def test_externalizing_a_multi_line_field_replaces_all_of_its_lines(tmp_path, capsys):
    from agentctl.plan import load_plan

    multi = '"""\nfirst line\nsecond line\n"""'
    index = _plan(tmp_path, _meta(), _stage(1, method=multi, title='"Stage 1"'))
    before = index.read_text()
    doc_before = load_plan(index)

    rc, out = _run(tmp_path, capsys, "plan-externalize", "--plan", str(index),
                   "--stage", "1", "--field", "method")

    assert rc == 0, out
    removed, added = _external_line_diff(before, index.read_text())
    assert removed == ['method = """', "first line", "second line", '"""']
    assert len(added) == 1
    assert load_plan(index) == doc_before
    assert (tmp_path / "plan.parts" / "stage-1-method.md").read_text() == "first line\nsecond line\n"


def test_externalizing_a_meta_field_and_an_order_field(tmp_path, capsys):
    from agentctl.plan import load_plan

    index = _plan(tmp_path, _meta(), _stage(1), tail=SUBSTANTIVE_ORDER)
    doc_before = load_plan(index)

    rc, _ = _run(tmp_path, capsys, "plan-externalize", "--plan", str(index), "--meta-field", "goal")
    assert rc == 0
    rc, _ = _run(tmp_path, capsys, "plan-externalize", "--plan", str(index),
                 "--order-field", "functional_place")
    assert rc == 0

    assert (tmp_path / "plan.parts" / "meta-goal.md").read_text() == "exercise references"
    assert (tmp_path / "plan.parts" / "meta-order-functional_place.md").is_file()
    assert load_plan(index) == doc_before


def test_externalizing_a_field_of_a_referenced_stage_edits_that_file_and_reseals_the_chain(
        tmp_path, capsys):
    from agentctl.plan import load_plan, load_plan_with_digest

    body = _stage_fields(1, method='"held in the stage file"')
    ref = _ref(tmp_path, "plan.parts/stage-1.toml", body)
    index = _write(tmp_path / "plan.toml", _meta() + "\n" + _stage_array(ref))
    doc_before = load_plan(index)
    root_before = load_plan_with_digest(index)[2]

    rc, out = _run(tmp_path, capsys, "plan-externalize", "--plan", str(index),
                   "--stage", "1", "--field", "method")

    assert rc == 0, out
    stage_file = (tmp_path / "plan.parts" / "stage-1.toml").read_text()
    leaf = "held in the stage file"
    assert (f'method = {{ ref = "stage-1-method.md", sha256 = "{_sha(leaf)}" }}') in stage_file
    assert (tmp_path / "plan.parts" / "stage-1-method.md").read_text() == leaf
    assert f'sha256 = "{_sha(stage_file)}"' in index.read_text()
    assert load_plan(index) == doc_before
    assert load_plan_with_digest(index)[2] != root_before


@pytest.mark.parametrize("field", ["depends_on", "index", "grants", "no_such_field"])
def test_externalizing_a_field_outside_the_allowed_set_is_refused_by_name(
        tmp_path, capsys, field):
    index = _two_stage(tmp_path)
    before = _tree(tmp_path)

    rc, out = _run(tmp_path, capsys, "plan-externalize", "--plan", str(index),
                   "--stage", "2", "--field", field)

    assert rc == 1 and not out["ok"]
    assert field in out["detail"]
    assert _tree(tmp_path) == before


def test_sealing_reseals_every_link_of_a_stage_chain_after_the_leaf_changes(tmp_path):
    from agentctl import plan_refs
    from agentctl.plan import load_plan

    index = _chain(tmp_path, 3)
    leaf_text = (tmp_path / "c3.toml").read_text(encoding="utf-8")
    _write(tmp_path / "c3.toml", leaf_text.replace("Stage 1", "Stage one"))

    root = plan_refs.seal(index)

    assert load_plan(index).stages[0].title == "Stage one"
    assert root == _sha(index.read_text(encoding="utf-8"))
    for link, target in (("c2.toml", "c3.toml"), ("c1.toml", "c2.toml")):
        body = tomllib.loads((tmp_path / link).read_text(encoding="utf-8"))
        assert body == {"ref": target, "sha256": _sha((tmp_path / target).read_text("utf-8"))}


def test_plan_seal_through_the_cli_reseals_after_an_edit(tmp_path, capsys):
    from agentctl.plan import load_plan

    ref = _ref(tmp_path, "m.md", "before\n")
    index = _plan(tmp_path, _meta(), _stage(1, method=ref))
    _write(tmp_path / "m.md", "after\n")

    rc, out = _run(tmp_path, capsys, "plan-seal", "--plan", str(index))

    assert rc == 0, out
    assert load_plan(index).stages[0].means.method == "after\n"
    assert _no_session_state(tmp_path)


def _snapshot(d: Path) -> dict[str, bytes | None]:
    """Every file with its bytes and every directory (None), so a leftover empty
    directory counts as a change."""
    return {str(p.relative_to(d)): p.read_bytes() if p.is_file() else None
            for p in sorted(d.rglob("*"))}


def _held_in_a_stage_file(tmp_path: Path) -> Path:
    body = _stage_fields(1, method='"held in the stage file"')
    ref = _ref(tmp_path, "stages/stage-1.toml", body)
    return _write(tmp_path / "plan.toml", _meta() + "\n" + _stage_array(ref))


@pytest.mark.parametrize("failing_step", ["seal", "reload-equality"])
def test_a_failed_externalize_restores_every_touched_file_and_leaves_no_new_directory(
        tmp_path, capsys, monkeypatch, failing_step):
    from agentctl import plan_refs
    from agentctl.plan import PlanError

    index = _held_in_a_stage_file(tmp_path)
    before = _snapshot(tmp_path)
    if failing_step == "seal":
        def boom(*args, **kwargs):
            raise PlanError("seal failed")

        monkeypatch.setattr(plan_refs, "seal", boom)
    else:
        real_load = plan_refs.load_plan
        calls = []

        def second_load_differs(*args, **kwargs):
            calls.append(1)
            return real_load(*args, **kwargs) if len(calls) == 1 else None

        monkeypatch.setattr(plan_refs, "load_plan", second_load_differs)

    rc, out = _run(tmp_path, capsys, "plan-externalize", "--plan", str(index),
                   "--stage", "1", "--field", "method")

    assert rc == 1 and not out["ok"]
    assert _snapshot(tmp_path) == before


def test_externalizing_a_field_that_is_already_a_reference_is_refused(tmp_path, capsys):
    index = _plan(tmp_path, _meta(), _stage(1, method='"do the work"'))
    args = ("plan-externalize", "--plan", str(index), "--stage", "1", "--field", "method")
    rc, _ = _run(tmp_path, capsys, *args)
    assert rc == 0
    before = _snapshot(tmp_path)

    rc, out = _run(tmp_path, capsys, *args)

    assert rc == 1 and not out["ok"]
    assert "already a reference" in out["detail"]
    assert _snapshot(tmp_path) == before


def test_externalizing_onto_an_existing_file_with_different_content_is_refused(
        tmp_path, capsys):
    index = _plan(tmp_path, _meta(), _stage(1, method='"do the work"'))
    _write(tmp_path / "plan.parts" / "stage-1-method.md", "something else")
    before = _snapshot(tmp_path)

    rc, out = _run(tmp_path, capsys, "plan-externalize", "--plan", str(index),
                   "--stage", "1", "--field", "method")

    assert rc == 1 and not out["ok"]
    assert "stage-1-method.md" in out["detail"]
    assert _snapshot(tmp_path) == before


def test_sealing_refuses_when_the_reference_text_also_occurs_elsewhere_in_the_file(
        tmp_path, capsys):
    ref = _ref(tmp_path, "m.md", "before\n")
    copy = f"title = 'see ref = \"m.md\", sha256 = \"{_sha('before' + chr(10))}\"'\n"
    index = _plan(tmp_path, _meta(), _stage(1, method=ref, title=None) + copy)
    _write(tmp_path / "m.md", "after\n")
    before = _snapshot(tmp_path)

    rc, out = _run(tmp_path, capsys, "plan-seal", "--plan", str(index))

    assert rc == 1 and not out["ok"]
    assert "m.md" in out["detail"]
    assert _snapshot(tmp_path) == before


def test_sealing_refuses_a_rewrite_that_changes_more_than_hashes(
        tmp_path, capsys, monkeypatch):
    from agentctl import plan_refs

    ref = _ref(tmp_path, "m.md", "before\n")
    index = _plan(tmp_path, _meta(), _stage(1, method=ref))
    _write(tmp_path / "m.md", "after\n")
    real = plan_refs._rewrite_hash
    monkeypatch.setattr(
        plan_refs, "_rewrite_hash",
        lambda *a, **k: real(*a, **k).replace('goal = "exercise references"', 'goal = "x"'))
    before = _snapshot(tmp_path)

    rc, out = _run(tmp_path, capsys, "plan-seal", "--plan", str(index))

    assert rc == 1 and not out["ok"]
    assert "sha256" in out["detail"]
    assert _snapshot(tmp_path) == before


def test_plan_digest_for_a_stage_the_plan_does_not_have_is_a_clean_error(tmp_path, capsys):
    index = _two_stage(tmp_path)

    rc, out = _run(tmp_path, capsys, "plan-digest", "--plan", str(index), "--stage", "99")

    assert rc == 1 and not out["ok"]
    assert "99" in out["detail"]
