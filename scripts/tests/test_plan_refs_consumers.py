"""Consumers of plan digests and of old plan versions, for hypertext plans (ADR-0012).

(a) part digests come from the resolved plan, (b) the plan-review `--plan-digest` check goes
through the resolver, (c) the plan object store keeps older versions readable, (d) plan-render
shows resolved text and where it lives, (e) the raw plan reader inventory is complete, (f) old
sessions and single-file plans keep loading.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
INVENTORY = REPO / "docs" / "operations" / "plan-readers.md"
DISPOSITIONS = {"resolves", "index-only", "not a plan"}
POSITIVE_CONTROLS = {
    "scripts/agentctl/plan.py:load_plan_with_digest",
    "scripts/agentctl/cli_common.py:_plan_file_sha256",
    "scripts/plan-review-topological.py:Driver.launch",
}


@pytest.fixture(autouse=True)
def _object_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_OBJECTS_DIR", str(tmp_path / "plan-objects"))


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha(path: Path) -> str:
    return _sha_bytes(Path(path).read_bytes())


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture
def env(tmp_path, store, monkeypatch):
    from test_norm_staleness_pair_currency import Env, _fan_in

    monkeypatch.setenv("AGENTCTL_ESCALATION_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    return Env(tmp_path, store, _fan_in())


def _inline_and_external(tmp_path, *externalize):
    """The `_fan_in` plan twice, one directory each: everything inline, and with the given
    `(kind, field)` parts moved into files (`kind` is "meta" or a stage number)."""
    from agentctl import plan_refs
    from test_norm_staleness_pair_currency import _fan_in, _write

    (tmp_path / "inline").mkdir()
    (tmp_path / "ext").mkdir()
    inline = _write(tmp_path / "inline" / "plan.toml", _fan_in())
    ext = _write(tmp_path / "ext" / "plan.toml", _fan_in())
    parts = {}
    for kind, field in externalize:
        if kind == "meta":
            done = plan_refs.plan_externalize(ext, meta_field=field)
        else:
            done = plan_refs.plan_externalize(ext, stage=kind, field=field)
        parts[(kind, field)] = Path(done["field_file"])
    return inline, ext, parts


def _digests(doc) -> dict[str, str]:
    from agentctl.plan import (
        pair_currency_hash, plan_content_digest, plan_meta_digest, plan_stage_digests,
        review_ids, stage_interface_digest, unit_currency_hash,
    )

    out = {"content": plan_content_digest(doc), "meta": plan_meta_digest(doc)}
    out.update({f"stage:{n}": d for n, d in plan_stage_digests(doc).items()})
    out.update({f"interface:{s.index}": stage_interface_digest(doc, s) for s in doc.stages})
    for rid in review_ids(doc):
        out[rid] = (unit_currency_hash(doc, rid) if rid.startswith("unit:")
                    else pair_currency_hash(doc, rid))
    return out


# --- (a) part digests are computed from the resolved plan ---------------------------------

def test_every_part_digest_of_an_externalized_plan_equals_the_inline_plans(tmp_path):
    from agentctl.plan import load_plan

    inline, ext, _ = _inline_and_external(
        tmp_path, (1, "method"), (3, "done_criterion"), ("meta", "goal"))

    assert _sha(inline) != _sha(ext)
    assert _digests(load_plan(ext)) == _digests(load_plan(inline))


def test_editing_an_externalized_part_moves_only_the_digests_its_stage_feeds(tmp_path):
    from agentctl import plan_refs
    from agentctl.plan import load_plan

    inline, ext, parts = _inline_and_external(tmp_path, (1, "method"))
    before = _digests(load_plan(ext))

    parts[(1, "method")].write_text("a different method", encoding="utf-8")
    plan_refs.seal(ext)
    after = _digests(load_plan(ext))

    moved = {key for key in before if before[key] != after[key]}
    assert {"stage:1", "unit:1", "3-1"} <= moved
    assert not moved & {"stage:2", "unit:2", "3-2", "meta", "unit:base"}


def test_an_edit_to_an_externalized_part_is_digested_like_the_same_edit_inline(tmp_path):
    from agentctl import plan_refs
    from agentctl.plan import load_plan
    from test_norm_staleness_pair_currency import _fan_in, _write

    inline, ext, parts = _inline_and_external(tmp_path, (1, "method"))
    parts[(1, "method")].write_text("a different method", encoding="utf-8")
    plan_refs.seal(ext)
    data = _fan_in()
    data["stage"][0]["method"] = "a different method"
    (tmp_path / "edited").mkdir()
    edited_inline = _write(tmp_path / "edited" / "plan.toml", data)

    assert _digests(load_plan(ext)) == _digests(load_plan(edited_inline))


# --- (a') review currency survives externalization; the whole-plan record is index bytes --

def test_every_recorded_pair_and_unit_review_stays_current_after_externalization(env):
    from agentctl import plan_refs

    env.record_every_id()
    ids = env.walk_ids()
    assert ids and all(env.status(rid) == "current" for rid in ids)

    plan_refs.plan_externalize(env.plan, stage=1, field="method")
    plan_refs.plan_externalize(env.plan, meta_field="goal")

    assert {rid: env.status(rid) for rid in ids} == {rid: "current" for rid in ids}


def test_editing_an_externalized_part_stales_only_the_reviews_its_stage_feeds(env):
    from agentctl import plan_refs

    env.record_every_id()
    done = plan_refs.plan_externalize(env.plan, stage=1, field="method")

    Path(done["field_file"]).write_text("a different method", encoding="utf-8")
    plan_refs.seal(env.plan)

    assert env.status("unit:1") == "stale:unit"
    assert env.status("3-1").startswith("stale:")
    assert env.status("unit:2") == "current"
    assert env.status("3-2") == "current"


def _whole_plan_review(env, digest: str):
    return cli.cmd_plan_review(
        ns(session="currency-s", target=None, scope=None, verdict="pass", reviewer="thinker",
           concerns=None, note="", plan_digest=digest, regression_command=None),
        store=env.store)


def test_the_whole_plan_review_is_bound_to_the_index_bytes(env):
    from agentctl import plan_refs

    plan_refs.plan_externalize(env.plan, stage=1, field="method")
    d = _whole_plan_review(env, _sha(env.plan))
    assert d.ok, d.detail

    review = env.state().plan_review
    assert review.plan_sha256 == _sha(env.plan)
    assert gates._plan_review_content_stale(review, str(env.plan)) is None


def test_the_whole_plan_review_goes_stale_when_externalizing_or_editing_a_part(env):
    from agentctl import plan_refs

    d = _whole_plan_review(env, _sha(env.plan))
    assert d.ok, d.detail
    review = env.state().plan_review
    assert gates._plan_review_content_stale(review, str(env.plan)) is None

    done = plan_refs.plan_externalize(env.plan, stage=1, field="method")
    assert "stale" in gates._plan_review_content_stale(review, str(env.plan))

    d = _whole_plan_review(env, _sha(env.plan))
    assert d.ok, d.detail
    review = env.state().plan_review
    Path(done["field_file"]).write_text("a different method", encoding="utf-8")
    plan_refs.seal(env.plan)
    assert "stale" in gates._plan_review_content_stale(review, str(env.plan))


def test_an_edited_part_that_was_not_resealed_is_refused_by_the_loader(env):
    from agentctl import plan_refs
    from agentctl.plan import PlanError, load_plan

    done = plan_refs.plan_externalize(env.plan, stage=1, field="method")
    Path(done["field_file"]).write_text("edited without sealing", encoding="utf-8")

    with pytest.raises(PlanError) as exc:
        load_plan(env.plan)
    assert done["sha256"] in str(exc.value)


# --- (b) the plan-review --plan-digest cross-check goes through the resolver --------------

def test_an_intact_externalized_plan_is_attested_by_the_sha256_of_its_index_bytes(env):
    from agentctl import plan_refs

    plan_refs.plan_externalize(env.plan, stage=1, field="method")

    d = _whole_plan_review(env, _sha(env.plan))

    assert d.ok, d.detail
    assert env.state().plan_review.plan_sha256 == _sha(env.plan)


def test_a_digest_of_the_resolved_content_is_not_the_attested_digest(env):
    from agentctl import plan_refs

    done = plan_refs.plan_externalize(env.plan, stage=1, field="method")

    d = _whole_plan_review(env, done["sha256"])

    assert not d.ok
    assert "does not match" in d.detail
    assert env.state().plan_review is None


def test_a_missing_referenced_file_refuses_the_attestation_and_names_the_reference(env):
    from agentctl import plan_refs

    done = plan_refs.plan_externalize(env.plan, stage=1, field="method")
    part = Path(done["field_file"])
    part.unlink()

    d = _whole_plan_review(env, _sha(env.plan))

    assert not d.ok
    assert part.name in d.detail
    assert env.state().plan_review is None


def test_a_tampered_referenced_file_refuses_the_attestation_and_names_the_reference(env):
    from agentctl import plan_refs

    done = plan_refs.plan_externalize(env.plan, stage=1, field="method")
    part = Path(done["field_file"])
    part.write_text("tampered", encoding="utf-8")

    d = _whole_plan_review(env, _sha(env.plan))

    assert not d.ok
    assert part.name in d.detail
    assert done["sha256"] in d.detail


def test_a_missing_referenced_file_refuses_a_pair_scoped_record_naming_the_reference(env):
    from agentctl import plan_refs

    done = plan_refs.plan_externalize(env.plan, stage=1, field="method")
    part = Path(done["field_file"])
    part.unlink()

    d = env.record("3-1")

    assert not d.ok
    assert part.name in d.detail
    assert env.state().plan_pair_reviews == {}


def test_a_plan_without_references_is_attested_as_before(env):
    d = _whole_plan_review(env, _sha(env.plan))

    assert d.ok, d.detail
    assert env.state().plan_review.plan_sha256 == _sha(env.plan)


# --- (c) the plan object store ------------------------------------------------------------

def test_put_stores_bytes_under_their_sha256_idempotently_and_get_returns_them(tmp_path):
    from agentctl import plan_store

    data = b"plan bytes \xc3\xa9\n"
    digest = plan_store.put(data)

    assert digest == _sha_bytes(data)
    assert plan_store.put(data) == digest
    assert plan_store.get(digest) == data
    assert (tmp_path / "plan-objects" / digest[:2] / digest).read_bytes() == data


def test_the_store_directory_comes_from_the_environment_else_the_plans_directory(
        tmp_path, monkeypatch):
    from agentctl import plan_store
    from lib import config_root

    assert plan_store.objects_dir() == tmp_path / "plan-objects"
    monkeypatch.delenv("AGENTCTL_PLAN_OBJECTS_DIR")
    assert plan_store.objects_dir() == config_root.plans_dir() / "objects"


def test_get_of_a_missing_object_is_a_plan_error_naming_the_hash():
    from agentctl import plan_store
    from agentctl.plan import PlanError

    missing = "ab" * 32
    with pytest.raises(PlanError) as exc:
        plan_store.get(missing)
    assert missing in str(exc.value)


def test_get_refuses_an_object_whose_bytes_do_not_hash_to_its_name(tmp_path):
    from agentctl import plan_store
    from agentctl.plan import PlanError

    digest = plan_store.put(b"original")
    (tmp_path / "plan-objects" / digest[:2] / digest).write_bytes(b"corrupted")

    with pytest.raises(PlanError) as exc:
        plan_store.get(digest)
    assert digest in str(exc.value)
    assert "corrupt" in str(exc.value)


def test_the_reader_serves_the_stored_bytes_whatever_the_path_names_now(tmp_path):
    from agentctl import plan_store

    digest = plan_store.put(b"stored text")
    on_disk = tmp_path / "part.md"
    on_disk.write_bytes(b"different text on disk")

    assert plan_store.reader()(on_disk, digest) == b"stored text"


def test_the_reader_does_not_fall_back_to_the_disk_unless_asked(tmp_path):
    from agentctl import plan, plan_refs, plan_store

    on_disk = tmp_path / "part.md"
    on_disk.write_bytes(b"only on disk")
    digest = _sha_bytes(b"only on disk")

    with pytest.raises(plan.PlanError) as exc:
        plan_store.reader()(on_disk, digest)
    assert digest in str(exc.value)
    assert plan_store.reader(fall_back_to_disk=True)(on_disk, digest) == b"only on disk"
    with pytest.raises(plan_refs._Unreadable):
        plan_store.reader(fall_back_to_disk=True)(tmp_path / "gone.md", "cd" * 32)


def test_store_plan_keeps_the_index_and_every_referenced_file(env):
    from agentctl import plan_refs, plan_store

    done = plan_refs.plan_externalize(env.plan, stage=1, field="method")
    plan_refs.plan_externalize(env.plan, meta_field="goal")
    index = env.plan.read_bytes()

    digests = plan_store.store_plan(index, env.plan)

    assert _sha_bytes(index) in digests
    assert done["sha256"] in digests
    assert len(digests) == 3
    assert all(plan_store.get(d) for d in digests)


def _hypertext_session(store, venued_fixtures_dir, tmp_path):
    """A session approved and executing on a plan whose stage-1 `done_criterion` is held in
    a referenced file. Returns `(session id, plan path, part file, original text)`."""
    from agentctl import plan_refs
    from test_replan import _to_executing_stage1

    plan = tmp_path / "plans" / "plan.toml"
    plan.parent.mkdir()
    plan.write_bytes((venued_fixtures_dir / "plan_two_stage.toml").read_bytes())
    done = plan_refs.plan_externalize(plan, stage=1, field="done_criterion")
    part = Path(done["field_file"])
    original = part.read_text(encoding="utf-8")
    _to_executing_stage1(store, "hyper", str(plan))
    return "hyper", plan, part, original


def test_approve_and_submit_put_the_index_and_the_part_into_the_store(
        store, venued_fixtures_dir, tmp_path):
    from agentctl import plan_store

    sid, plan, part, _ = _hypertext_session(store, venued_fixtures_dir, tmp_path)

    state = store.load(sid)
    assert state.plan_snapshot_hash == _sha(plan)
    assert plan_store.get(_sha(plan)) == plan.read_bytes()
    assert plan_store.get(_sha(part)) == part.read_bytes()


def test_the_approved_snapshot_keeps_its_text_after_the_part_is_edited_and_resealed(
        store, venued_fixtures_dir, tmp_path):
    from agentctl import plan_refs

    sid, plan, part, original = _hypertext_session(store, venued_fixtures_dir, tmp_path)
    part.write_text("an edited done criterion", encoding="utf-8")
    plan_refs.seal(plan)

    snapshot = cli._approved_snapshot_doc(store.load(sid))

    assert snapshot is not None
    assert snapshot.stages[0].criterion.done_criterion == original


def test_the_approved_snapshot_keeps_its_text_after_the_part_is_deleted(
        store, venued_fixtures_dir, tmp_path):
    sid, plan, part, original = _hypertext_session(store, venued_fixtures_dir, tmp_path)
    part.unlink()

    snapshot = cli._approved_snapshot_doc(store.load(sid))

    assert snapshot is not None
    assert snapshot.stages[0].criterion.done_criterion == original


def test_a_replan_after_the_part_changed_diffs_against_the_approved_text(
        store, venued_fixtures_dir, tmp_path):
    from agentctl import plan_refs

    sid, plan, part, original = _hypertext_session(store, venued_fixtures_dir, tmp_path)
    part.write_text("an edited done criterion", encoding="utf-8")
    plan_refs.seal(plan)

    d = cli.cmd_replan(ns(session=sid, plan=str(plan)), store=store)

    assert "does not resolve" not in d.detail
    assert "is not in the store" not in d.detail
    assert d.data.get("kind", d.data.get("replan_kind")) != "no_change"


def test_an_object_missing_from_the_store_is_refused_naming_its_hash_with_no_disk_fallback(
        store, venued_fixtures_dir, tmp_path):
    from agentctl import plan_store
    from agentctl.plan import PlanError

    sid, plan, part, _ = _hypertext_session(store, venued_fixtures_dir, tmp_path)
    state = store.load(sid)
    shutil.rmtree(tmp_path / "plan-objects")

    assert part.exists()
    with pytest.raises(PlanError) as exc:
        plan_store.load_baseline(state.plan_snapshot_path, state.plan_path)
    assert _sha(part) in str(exc.value)
    assert cli._approved_snapshot_doc(state) is None


def test_the_registered_plan_itself_is_loaded_from_the_disk(
        store, venued_fixtures_dir, tmp_path):
    from agentctl import plan_store

    sid, plan, part, _ = _hypertext_session(store, venued_fixtures_dir, tmp_path)
    shutil.rmtree(tmp_path / "plan-objects")

    doc = plan_store.load_baseline(plan, str(plan))

    assert doc.stages[0].criterion.done_criterion == part.read_text(encoding="utf-8")


def test_put_rewrites_an_object_whose_bytes_do_not_hash_to_its_name(tmp_path):
    from agentctl import plan_store

    digest = plan_store.put(b"original")
    target = tmp_path / "plan-objects" / digest[:2] / digest
    target.write_bytes(b"torn")

    assert plan_store.put(b"original") == digest
    assert plan_store.get(digest) == b"original"
    assert [p.name for p in target.parent.iterdir()] == [digest]


def test_each_put_writes_through_its_own_partial_and_leaves_none_behind(tmp_path, monkeypatch):
    from agentctl import plan_store

    sources = []
    real_replace = plan_store.os.replace

    def spy(src, dst):
        sources.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(plan_store.os, "replace", spy)
    first = plan_store.put(b"first")
    plan_store.put(b"second")
    target = tmp_path / "plan-objects" / first[:2] / first
    target.write_bytes(b"torn")
    plan_store.put(b"first")

    assert len(sources) == len(set(sources)) == 3
    assert all(name.endswith(".partial") for name in sources)
    assert not list((tmp_path / "plan-objects").rglob("*.partial"))


def test_a_failed_put_removes_its_partial(tmp_path, monkeypatch):
    from agentctl import plan_store

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(plan_store.os, "replace", fail)
    with pytest.raises(OSError):
        plan_store.put(b"never stored")

    assert not list((tmp_path / "plan-objects").rglob("*.partial"))


def test_get_refuses_a_name_that_is_not_a_sha256_hex_digest():
    from agentctl import plan_store
    from agentctl.plan import PlanError

    for name in ("../" + "a" * 61, "AB" * 32, "ab" * 31):
        with pytest.raises(PlanError) as exc:
            plan_store.get(name)
        assert "not a sha256 hex digest" in str(exc.value)


def test_the_spawned_brief_falls_back_to_the_snapshot_with_its_references_from_the_store(
        store, venued_fixtures_dir, tmp_path):
    import importlib.util

    script = REPO / "scripts" / "spawn-specialist.py"
    spec = importlib.util.spec_from_file_location("spawn_specialist_plan_refs", script)
    spawn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(spawn)
    sid, plan, part, original = _hypertext_session(store, venued_fixtures_dir, tmp_path)
    part.unlink()

    doc = spawn._session_plan(store.load(sid))

    assert doc is not None
    assert doc.stages[0].criterion.done_criterion == original


# --- (d) plan-render ------------------------------------------------------------------------

def _externalized(tmp_path):
    return _inline_and_external(tmp_path, (1, "method"), ("meta", "goal"))


def test_plan_render_prints_resolved_text_and_marks_each_externalized_field(tmp_path):
    from agentctl import render

    inline, ext, parts = _externalized(tmp_path)

    d = render.cmd_plan_render(ns(plan=str(ext), stage=None))

    assert d.ok
    assert "method-1" in d.detail
    assert "## Externalized fields" in d.detail
    listed = {e["field"]: e for e in d.data["externalized"]}
    assert set(listed) == {"meta.goal", "1.method"}
    assert listed["1.method"]["sha256"] == _sha(parts[(1, "method")])
    assert listed["1.method"]["path"] == parts[(1, "method")].relative_to(ext.parent).as_posix()
    assert f"`{listed['1.method']['path']}` (sha256 `{listed['1.method']['sha256']}`)" in d.detail
    assert d.data["markdown"] == d.detail


def test_plan_render_stage_filter_lists_only_that_stages_references(tmp_path):
    from agentctl import render

    inline, ext, parts = _externalized(tmp_path)

    only_two = render.cmd_plan_render(ns(plan=str(ext), stage="2"))
    only_one = render.cmd_plan_render(ns(plan=str(ext), stage="1"))

    assert "externalized" not in only_two.data
    assert "Externalized fields" not in only_two.detail
    assert [e["field"] for e in only_one.data["externalized"]] == ["1.method"]


def test_plan_render_of_a_plan_without_references_is_unchanged(tmp_path):
    from agentctl import render
    from agentctl.plan import load_plan

    inline, ext, parts = _externalized(tmp_path)

    d = render.cmd_plan_render(ns(plan=str(inline), stage=None))

    assert d.detail == render.render_plan_md(load_plan(inline))
    assert "externalized" not in d.data


def test_plan_render_text_of_an_externalized_plan_is_the_inline_render_plus_the_section(tmp_path):
    from agentctl import render

    inline, ext, parts = _externalized(tmp_path)

    plain = render.cmd_plan_render(ns(plan=str(inline), stage=None)).detail
    hyper = render.cmd_plan_render(ns(plan=str(ext), stage=None)).detail

    assert hyper.startswith(plain)
    assert hyper[len(plain):].lstrip().startswith("## Externalized fields")


# --- (e) the raw plan reader inventory --------------------------------------------------------

_PLAN = re.compile("plan", re.IGNORECASE)


def _read_arguments(call: ast.Call):
    func = call.func
    if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id == "tomllib" and func.attr in ("load", "loads")):
        return list(call.args) + [k.value for k in call.keywords]
    if isinstance(func, ast.Attribute) and func.attr in ("read_bytes", "read_text"):
        return [func.value] + list(call.args)
    if isinstance(func, ast.Name) and func.id == "open":
        return list(call.args)
    return None


def _mentions_plan(nodes) -> bool:
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and _PLAN.search(sub.id):
                return True
            if isinstance(sub, ast.Attribute) and _PLAN.search(sub.attr):
                return True
            if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    and _PLAN.search(sub.value)):
                return True
    return False


class _Readers(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.scope: list[ast.AST] = []
        self.found: set[str] = set()

    def _enter(self, node):
        self.scope.append(node)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _enter

    def visit_Call(self, node):
        arguments = _read_arguments(node)
        if arguments is not None:
            qualname = ".".join(n.name for n in self.scope) or "<module>"
            hit = _mentions_plan(arguments) or bool(_PLAN.search(qualname))
            for enclosing in reversed(self.scope):
                if isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    params = enclosing.args.args + enclosing.args.kwonlyargs
                    hit = hit or any(_PLAN.search(a.arg) for a in params)
                    break
            if hit:
                self.found.add(f"{self.rel}:{qualname}")
        self.generic_visit(node)


def enumerate_raw_plan_readers(root: Path) -> set[str]:
    """Keys `<repo-relative file>:<qualname or <module>>` of every function under
    `root/scripts` (tests excluded) that reads a file by `tomllib.load(s)`, `read_bytes`,
    `read_text` or `open` where the read's arguments, the enclosing function's qualified
    name, or one of its parameter names mentions "plan"."""
    found: set[str] = set()
    for path in sorted((root / "scripts").glob("**/*.py")):
        rel = path.relative_to(root)
        if rel.parts[:2] == ("scripts", "tests"):
            continue
        visitor = _Readers(rel.as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found |= visitor.found
    return found


def _inventory_section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("#")), len(lines))
    return lines[start:end]


def _inventory_rows() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in _inventory_section(INVENTORY.read_text(encoding="utf-8"), "## Raw plan readers"):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if set(cells[0]) <= set("-: ") or cells[0].lower() == "reader":
            continue
        rows[cells[0].replace("`", "")] = cells[1]
    return rows


def test_the_enumerator_finds_a_planted_reader_and_ignores_a_reader_of_something_else(tmp_path):
    scripts = tmp_path / "scripts"
    (scripts / "tests").mkdir(parents=True)
    (scripts / "tool.py").write_text(
        "import tomllib\nfrom pathlib import Path\n\n"
        "class Loader:\n"
        "    def load(self, plan_path):\n"
        "        return tomllib.loads(Path(plan_path).read_text())\n\n"
        "def settings(cfg):\n"
        "    return Path(cfg).read_text()\n\n"
        "def digest(target):\n"
        "    return Path(target).read_bytes()\n\n"
        "def plan_bytes(target):\n"
        "    return Path(target).read_bytes()\n\n"
        "def module_level():\n"
        "    return 1\n"
        "PLAN = open('plan.toml').read()\n",
        encoding="utf-8")
    (scripts / "tests" / "test_x.py").write_text(
        "def read_plan(plan_path):\n    return open(plan_path).read()\n", encoding="utf-8")

    assert enumerate_raw_plan_readers(tmp_path) == {
        "scripts/tool.py:Loader.load", "scripts/tool.py:plan_bytes", "scripts/tool.py:<module>",
    }


def test_the_inventory_rows_are_exactly_the_enumerated_raw_plan_readers():
    rows = set(_inventory_rows())
    enumerated = enumerate_raw_plan_readers(REPO)

    assert enumerated - rows == set(), "readers missing from docs/operations/plan-readers.md"
    assert rows - enumerated == set(), "inventory rows that no longer name a reader"


def test_the_enumerator_still_finds_the_pinned_positive_controls():
    assert POSITIVE_CONTROLS <= enumerate_raw_plan_readers(REPO)
    assert POSITIVE_CONTROLS <= set(_inventory_rows())


def test_every_inventory_row_uses_a_disposition_from_the_closed_list():
    rows = _inventory_rows()

    assert rows
    assert {key: d for key, d in rows.items() if d not in DISPOSITIONS} == {}


def test_the_inventory_names_the_known_indirect_readers():
    section = "\n".join(_inventory_section(
        INVENTORY.read_text(encoding="utf-8"), "### Known indirect readers"))

    for reader in ("gates._file_sha256", "cli._stage_grant_entries", "plan_refs.read_referenced_file",
                   "plan_refs._parse", "plan_store.get", "plan_store._intact",
                   "plan_store.reader"):
        assert f"`{reader}`" in section, reader


# --- (f) old sessions and single-file plans keep loading ---------------------------------------

@pytest.mark.parametrize("name", [
    "legacy_schema11_plan_review.json",
    "legacy_schema12_plan_review.json",
])
def test_persisted_session_fixtures_still_load(name):
    from agentctl.state import SessionState

    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    state = SessionState.from_dict(raw)

    assert state.session_id


def test_a_single_file_snapshot_loads_with_an_empty_store_and_any_registered_path(tmp_path):
    from agentctl import plan_store
    from agentctl.plan import load_plan

    snapshot = FIXTURES / "plan_snapshot_smd-act-defects-8.toml"

    assert not (tmp_path / "plan-objects").exists()
    assert plan_store.load_baseline(snapshot, tmp_path / "elsewhere" / "plan.toml") == load_plan(
        snapshot)
    assert plan_store.load_baseline(snapshot, None) == load_plan(snapshot)


def test_a_session_approved_before_the_store_existed_still_snapshots_and_replans(
        store, venued_fixtures_dir, tmp_path):
    from test_replan import _to_executing_stage1

    plan = tmp_path / "plan.toml"
    plan.write_bytes((venued_fixtures_dir / "plan_two_stage.toml").read_bytes())
    _to_executing_stage1(store, "old", str(plan))
    shutil.rmtree(tmp_path / "plan-objects")
    state = store.load("old")

    snapshot = cli._approved_snapshot_doc(state)
    plan.write_bytes((venued_fixtures_dir / "plan_two_stage_substantive.toml").read_bytes())
    d = cli.cmd_replan(ns(session="old", plan=str(plan)), store=store)

    assert snapshot is not None
    assert [s.index for s in snapshot.stages] == [1, 2]
    assert d.marker == "PLAN-READY"
    assert [s.index for s in store.load("old").stages] == [1, 2, 3]
