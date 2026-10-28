"""Plan views: what `render.build_site_prompt` and the spawners' prompt assembly pass to a
model, which nodes of the plan that is, and what the model is granted to read for the rest.

The fixture is `plan_site_fixture`: four stages (1 relies on nothing, 2 on 1, 3 on 2 with an
empty interface, 4 on 3) and a unique token at the start and end of every key outside
`render.PROJECTED_FIELDS`, so a token in a prompt names the node whose key reached it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent


class _LazyFixture:
    """`plan_site_fixture`, imported on first use: a tree without the module fails the
    tests that need it, not the collection of this file."""

    def __getattr__(self, name):
        import plan_site_fixture

        return getattr(plan_site_fixture, name)


fx = _LazyFixture()

SHA_IN_OBJECTS = re.compile(r"objects/([0-9a-f]{64})")

SITE_CASES = [
    ("dispatch-stage-brief", {"stage": 1}, {1}),
    ("dispatch-stage-brief", {"stage": 2}, {2}),
    ("dispatch-stage-brief", {"stage": 3}, {3}),
    ("dispatch-no-stage", {}, {"meta"}),
    ("whole-plan-review", {}, {"meta"}),
    ("topo-pair", {"pair": "2-1"}, {"meta", 1, 2}),
    ("topo-pair", {"pair": "3-2"}, {"meta", 3}),
    ("topo-pair", {"pair": "4-3"}, {"meta", 3, 4}),
    ("topo-unit", {"unit": "unit:2"}, {2}),
    ("topo-unit", {"unit": "unit:base"}, {"meta"}),
    ("enumerate-node", {"stage": 2}, {2}),
    ("enumerate-node", {}, {"meta"}),
]
WITHOUT_VIEW = ("topo-unit", "enumerate-node")


def _site_id(case) -> str:
    kind, selector, _ = case
    return "-".join([kind, *map(str, selector.values())])


def _service_stage(selector) -> set:
    """The stage a pair view holds the brief of beside the nodes it shows: the pair's
    service stage is readable even where its text is not part of the review."""
    return {int(selector["pair"].split("-")[1])} if "pair" in selector else set()


def _site(kind, plan, views, **selector):
    from agentctl.render import build_site_prompt

    return build_site_prompt(kind, plan, view_root=views, **selector)


def _view_texts(site) -> "dict[str, str]":
    if site.view_dir is None:
        return {}
    return {name: (site.view_dir / name).read_text(encoding="utf-8")
            for name in site.view_files if not name.startswith("objects/")}


def _everything_printed(site) -> str:
    return "\n".join([site.text, *_view_texts(site).values()])


@pytest.fixture
def plan(tmp_path):
    return fx.write_plan(tmp_path / "author")


@pytest.mark.parametrize("case", SITE_CASES, ids=_site_id)
def test_a_site_shows_the_nodes_its_kind_names_and_no_others(case, plan, tmp_path):
    kind, selector, expected = case

    site = _site(kind, plan, tmp_path / "views", **selector)

    readable = expected | _service_stage(selector)
    assert set(site.shown_nodes) == expected
    printed = _everything_printed(site)
    assert [t for t in fx.hidden_tokens(readable) if t in printed] == []
    assert [t for t in fx.shown_tokens(readable - {"meta"}) if t not in printed] == []


@pytest.mark.parametrize("case", [c for c in SITE_CASES if c[0] not in WITHOUT_VIEW],
                         ids=_site_id)
def test_every_path_a_site_offers_lies_in_its_view_and_exists(case, plan, tmp_path):
    kind, selector, _ = case
    site = _site(kind, plan, tmp_path / "views", **selector)

    assert site.add_dirs == (str(site.view_dir),)
    printed = _everything_printed(site)
    offered = re.findall(re.escape(str(site.view_dir)) + r"(?:/[\w./\-]*\w)?", printed)
    assert offered
    assert all(Path(path).exists() for path in offered)
    for pointer in SHA_IN_OBJECTS.findall(printed):
        assert (site.view_dir / "objects" / pointer).is_file()
    for name in site.view_files:
        assert (site.view_dir / name).is_file()


@pytest.mark.parametrize("case", [c for c in SITE_CASES if c[0] not in WITHOUT_VIEW],
                         ids=_site_id)
def test_an_object_file_is_named_by_the_sha256_of_its_bytes(case, plan, tmp_path):
    kind, selector, _ = case
    site = _site(kind, plan, tmp_path / "views", **selector)

    objects = list((site.view_dir / "objects").glob("*")) if (site.view_dir / "objects").exists() else []
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == p.name for p in objects)


def test_a_node_without_a_view_offers_no_directory_and_prints_every_field(plan, tmp_path):
    for kind, selector, expected in [c for c in SITE_CASES if c[0] in WITHOUT_VIEW]:
        site = _site(kind, plan, tmp_path / "views", **selector)

        assert site.add_dirs == ()
        assert site.view_dir is None
        assert "objects/" not in site.text
        assert [t for t in fx.shown_tokens(expected) if t not in site.text] == []
    assert not (tmp_path / "views").exists()


def test_a_field_over_the_inline_limit_reads_as_a_pointer_to_its_object(tmp_path):
    index = fx.write_hypertext_plan(tmp_path / "author", long_method=True)
    sha256 = hashlib.sha256(fx.LONG_FIELD.encode("utf-8")).hexdigest()

    site = _site("dispatch-stage-brief", index, tmp_path / "views", stage=2)

    printed = _everything_printed(site)
    assert f"see objects/{sha256}" in site.text
    assert fx.LONG_FIELD not in printed
    assert "y" * 200 not in printed
    assert (site.view_dir / "objects" / sha256).read_bytes() == fx.LONG_FIELD.encode("utf-8")
    assert f"see objects/{sha256}" in _view_texts(site)["stage-2.md"]


def test_a_node_without_a_view_prints_a_field_over_the_inline_limit_in_full(tmp_path):
    index = fx.write_hypertext_plan(tmp_path / "author", long_method=True)

    for kind, selector in [("topo-unit", {"unit": "unit:2"}), ("enumerate-node", {"stage": 2})]:
        site = _site(kind, index, tmp_path / "views", **selector)

        assert site.add_dirs == ()
        assert "objects/" not in site.text
        assert fx.LONG_FIELD in site.text


def test_a_reference_to_a_reference_puts_every_file_it_passed_through_in_the_view(tmp_path):
    index = fx.write_hypertext_plan(tmp_path / "author")
    parts = tmp_path / "author" / "parts"
    held = [hashlib.sha256((parts / name).read_bytes()).hexdigest()
            for name in ("stage-2-hop.toml", "stage-2.toml")]

    site = _site("dispatch-stage-brief", index, tmp_path / "views", stage=2)

    objects = {p.name for p in (site.view_dir / "objects").iterdir()}
    assert set(held) <= objects
    assert [t for t in fx.shown_tokens({2}) if t not in _everything_printed(site)] == []


def test_a_hypertext_plan_never_submitted_still_gets_its_objects(tmp_path):
    index = fx.write_hypertext_plan(tmp_path / "author")
    assert not (tmp_path / "plan-objects").exists()

    site = _site("topo-pair", index, tmp_path / "views", pair="2-1")

    stage_files = {hashlib.sha256((tmp_path / "author" / "parts" / name).read_bytes()).hexdigest()
                   for name in ("stage-1.toml", "stage-2-hop.toml", "stage-2.toml")}
    assert stage_files <= {p.name for p in (site.view_dir / "objects").iterdir()}


@pytest.mark.parametrize("case", [c for c in SITE_CASES if c[0] not in WITHOUT_VIEW],
                         ids=_site_id)
def test_no_prompt_or_view_file_names_the_authors_directory_or_the_plans_directory(
        case, tmp_path):
    from lib.config_root import plans_dir

    author = tmp_path / "qauthordirx9z"
    index = fx.write_hypertext_plan(author)
    kind, selector, _ = case

    site = _site(kind, index, tmp_path / "views", **selector)

    view_files = [p for p in site.view_dir.rglob("*") if p.is_file()]
    assert any(p.parent.name == "objects" for p in view_files)
    for printed in (site.text, *(p.read_text(encoding="utf-8") for p in view_files)):
        assert str(author) not in printed
        assert str(plans_dir()) not in printed


@pytest.mark.parametrize("kind", ("dispatch-no-stage", "whole-plan-review"))
def test_a_site_without_a_stage_holds_every_file_the_plan_was_assembled_from(kind, tmp_path):
    index = fx.write_hypertext_plan(tmp_path / "author")
    parts = tmp_path / "author" / "parts"
    assembled = {hashlib.sha256(p.read_bytes()).hexdigest() for p in parts.iterdir()}
    assert len(assembled) == 5

    site = _site(kind, index, tmp_path / "views")

    held = {p.name for p in (site.view_dir / "objects").iterdir()}
    assert held == assembled | {hashlib.sha256(index.read_bytes()).hexdigest()}
    assert set(site.shown_nodes) == {"meta"}
    assert "## Referenced files" in site.text
    assert all(f"objects/{sha256}" in site.text for sha256 in assembled)


@pytest.mark.parametrize("case", [c for c in SITE_CASES if c[0] not in WITHOUT_VIEW],
                         ids=_site_id)
def test_every_site_with_a_view_opens_with_the_view_header(case, plan, tmp_path):
    kind, selector, _ = case
    site = _site(kind, plan, tmp_path / "views", **selector)

    assert site.text.startswith(f"Plan view: `{site.view_dir}` — ")
    assert "`see objects/<sha256>`" in site.text.splitlines()[0]


def test_a_site_without_a_stage_lets_the_model_read_every_stage_of_an_inline_plan(plan, tmp_path):
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()

    site = _site("dispatch-no-stage", plan, tmp_path / "views")

    assert f"objects/{digest}" in site.text
    assert not any(t in site.text for t in fx.hidden_tokens({"meta"}))
    index_object = (site.view_dir / "objects" / digest).read_text(encoding="utf-8")
    assert [t for stage in fx.STAGES for t in fx.shown_tokens({stage}) if t not in index_object] == []


def test_the_stage_brief_final_verification_names_nothing_the_model_cannot_read(plan, tmp_path):
    site = _site("dispatch-stage-brief", plan, tmp_path / "views", stage=2)

    brief = (site.view_dir / "stage-2.md").read_text(encoding="utf-8")
    assert "## Final verification (labels only" in brief
    assert "the coordinator runs them" in brief
    assert "full plan file" not in site.text + brief


def test_a_site_for_a_caller_that_grants_no_directory_has_no_view_and_no_header(plan, tmp_path):
    from agentctl.render import build_site_prompt

    for kind, selector in (("dispatch-stage-brief", {"stage": 2}), ("dispatch-no-stage", {})):
        site = build_site_prompt(kind, plan, view_root=tmp_path / "views", with_view=False,
                                 **selector)

        assert site.add_dirs == () and site.view_dir is None and site.view_files == ()
        assert "Plan view:" not in site.text
        assert "objects/" not in site.text and "## Referenced files" not in site.text
        assert [t for t in fx.shown_tokens(set(selector.values()) or set())
                if t not in site.text] == []
    assert not (tmp_path / "views").exists()


@pytest.mark.parametrize("kind,selector", [
    ("dispatch-stage-brief", {"stage": 2}), ("dispatch-no-stage", {})])
def test_a_site_without_a_view_prints_a_field_over_the_default_limit_in_full(
        kind, selector, tmp_path):
    from agentctl.render import build_site_prompt

    index = fx.write_hypertext_plan(tmp_path / "author", long_method=True)

    site = build_site_prompt(kind, index, view_root=tmp_path / "views", with_view=False,
                             **selector)

    assert fx.LONG_FIELD in site.text
    assert "objects/" not in site.text
    assert not (tmp_path / "views").exists()


@pytest.mark.parametrize("kind,selector", [
    ("topo-pair", {"pair": "2-1"}), ("topo-unit", {"unit": "unit:2"}),
    ("whole-plan-review", {}), ("enumerate-node", {"stage": 2})])
def test_a_view_less_site_is_refused_for_any_kind_but_the_two_dispatch_kinds(
        kind, selector, plan, tmp_path):
    from agentctl.render import build_site_prompt

    with pytest.raises(ValueError, match="with_view=False"):
        build_site_prompt(kind, plan, view_root=tmp_path / "views", with_view=False, **selector)


def test_a_view_less_site_without_a_stage_inlines_the_plan_with_every_reference_resolved(
        tmp_path):
    from agentctl.render import build_site_prompt

    index = fx.write_hypertext_plan(tmp_path / "author")

    site = build_site_prompt("dispatch-no-stage", index, with_view=False)

    assert [t for t in fx.shown_tokens(set(fx.NODES)) if t not in site.text] == []
    assert "{ ref" not in site.text and "sha256 =" not in site.text
    assert site.shown_nodes == frozenset(fx.NODES)
    assert site.add_dirs == ()


def test_a_plan_without_references_has_no_referenced_files_section(plan, tmp_path):
    site = _site("dispatch-no-stage", plan, tmp_path / "views")

    assert "## Referenced files" not in site.text


def _without_file_access_scope(prompt: str) -> str:
    return re.sub(r"## File-access scope.*?(?=\n## |\Z)", "", prompt, flags=re.DOTALL)


SPAWN_CASES = [
    ("stage-brief", ["--stage-index", "2"], "developer", {2}),
    ("no-stage", [], "developer", {"meta"}),
    ("whole-plan-review", [], "thinker", {"meta"}),
]


def _spawner():
    return fx.load_script("spawn-specialist.py", "spawn_specialist_views")


@pytest.mark.parametrize("name,extra,kind,shown", SPAWN_CASES, ids=[c[0] for c in SPAWN_CASES])
def test_the_assembled_prompt_names_neither_the_authors_directory_nor_the_plans_directory(
        name, extra, kind, shown, tmp_path):
    from lib.config_root import plans_dir

    author = tmp_path / "qauthordirx9z"
    index = fx.write_plan(author)

    prompt = _spawner().assembled_prompt(fx.specialist_argv(index, *extra, kind=kind))

    outside_scope = _without_file_access_scope(prompt)
    assert str(author) not in outside_scope
    assert str(plans_dir()) not in outside_scope
    assert [t for t in fx.hidden_tokens(shown) if t in prompt] == []


@pytest.mark.parametrize("name,extra,kind,shown", SPAWN_CASES, ids=[c[0] for c in SPAWN_CASES])
def test_the_launched_prompt_is_the_assembled_prompt(
        name, extra, kind, shown, plan, tmp_path, monkeypatch):
    argv = fx.specialist_argv(plan, *extra, kind=kind)

    rc, launched, _ = fx.launch_specialist(monkeypatch, tmp_path, argv)

    assert rc == 0
    assert launched == _spawner().assembled_prompt(argv)


def test_a_whole_plan_review_names_the_plan_digest_object(plan, tmp_path, monkeypatch):
    from agentctl import gates

    digest = gates._file_sha256(str(plan))
    argv = fx.specialist_argv(plan, kind="thinker")

    prompt = _spawner().assembled_prompt(argv)

    assert f"objects/{digest}" in prompt
    views = list((tmp_path / "plan-views").glob("site-*-whole-plan-review-*"))
    assert len(views) == 1
    assert (views[0] / "objects" / digest).read_bytes() == plan.read_bytes()


def _topo():
    return fx.load_script("plan-review-topological.py", "topo_views")


def _history(tmp_path) -> Path:
    path = tmp_path / "history.json"
    path.write_text(json.dumps({
        "records": [{
            "reviewer_verdict": "revise", "effective_verdict": "revise", "record_seq": 1,
            "concerns": [],
        }],
        "changed_parts_since_last": ["stage 2"],
    }), encoding="utf-8")
    return path


TOPO_CASES = [("2-1", "topo-pair", "pair"), ("unit:2", "topo-unit", "unit"),
              ("unit:base", "topo-unit", "unit")]


@pytest.mark.parametrize("rereview", [False, True], ids=["first", "re-review"])
@pytest.mark.parametrize("unit_id,kind,selector", TOPO_CASES, ids=[c[0] for c in TOPO_CASES])
def test_the_prompt_the_driver_sends_is_the_reviewer_prompt_and_carries_the_site_text(
        unit_id, kind, selector, rereview, plan, tmp_path, monkeypatch):
    topo = _topo()
    views = tmp_path / "driver-views"
    history = _history(tmp_path) if rereview else None
    argv = topo.build_spawn_argv(
        argparse.Namespace(model=None, complexity=None), unit_id, str(plan), dry_run=False,
        history_path=str(history) if history else None, view_root=str(views))

    rc, launched, cmd = fx.launch_specialist(monkeypatch, tmp_path, argv)

    assert rc == 0
    reviewer = topo.reviewer_prompt(
        str(plan), unit_id, view_root=views, history_path=str(history) if history else None)
    assert launched == reviewer
    from agentctl.render import build_site_prompt

    site = build_site_prompt(
        kind, plan, view_root=views, **{selector: unit_id},
        history=json.loads(history.read_text(encoding="utf-8")) if history else None)
    assert site.text in launched
    granted = " ".join(cmd)
    assert all(directory in granted for directory in site.add_dirs)


def test_a_pair_reviewer_is_granted_its_own_view_and_no_other_plan_view(plan, tmp_path, monkeypatch):
    topo = _topo()
    views = tmp_path / "driver-views"
    other = _site("topo-pair", plan, views, pair="4-3")
    argv = topo.build_spawn_argv(
        argparse.Namespace(model=None, complexity=None), "2-1", str(plan), dry_run=False,
        view_root=str(views))

    _, _, cmd = fx.launch_specialist(monkeypatch, tmp_path, argv)

    own = _site("topo-pair", plan, views, pair="2-1")
    granted = " ".join(cmd)
    assert str(own.view_dir) in granted
    assert str(other.view_dir) not in granted


def test_a_prompt_over_the_ceiling_is_returned_in_full_and_refused_at_launch(
        plan, tmp_path, monkeypatch):
    spawner = _spawner()
    ceiling = spawner.dispatch_prompt_ceiling_chars("haiku")
    argv = ["--kind", "developer", "--plan", str(plan), "--done-criterion", "done",
            "--criterion-type", "measurable", "--model", "haiku", "--effort", "medium",
            "--stage-index", "2", "--constraints", "x" * (ceiling + 1000)]

    prompt = spawner.assembled_prompt(argv)
    rc, launched, cmd = fx.launch_specialist(monkeypatch, tmp_path, argv)

    assert len(prompt) > ceiling
    assert rc == 5
    assert launched is None and cmd is None


def test_a_reviewer_prompt_over_the_ceiling_is_returned_in_full_and_refused_at_launch(
        plan, tmp_path, monkeypatch):
    topo = _topo()
    views = tmp_path / "driver-views"

    def lower_ceiling(module):
        monkeypatch.setattr(module, "dispatch_prompt_ceiling_chars", lambda model: 100)
        monkeypatch.setattr(topo, "_spawner_module", lambda: module)

    argv = topo.build_spawn_argv(
        argparse.Namespace(model=None, complexity=None), "2-1", str(plan), dry_run=False,
        view_root=str(views))

    rc, launched, cmd = fx.launch_specialist(monkeypatch, tmp_path, argv, configure=lower_ceiling)
    prompt = topo.reviewer_prompt(str(plan), "2-1", view_root=views)

    assert len(prompt) > 100
    assert rc == 5
    assert launched is None and cmd is None


def test_view_root_decides_where_views_land_and_the_default_root_stays_empty(
        plan, tmp_path, monkeypatch):
    chosen = tmp_path / "chosen-views"
    default = Path(os.environ["AGENTCTL_TOPO_UNITS_DIR"])
    argv = fx.specialist_argv(plan, "--stage-index", "2", "--view-root", str(chosen))

    rc, _, cmd = fx.launch_specialist(monkeypatch, tmp_path, argv)
    _spawner().assembled_prompt(argv)

    assert rc == 0
    assert [p.parent for p in chosen.iterdir()] == [chosen]
    assert list(chosen.iterdir())
    assert not default.exists() or list(default.iterdir()) == []
    assert any(str(chosen) in part for part in cmd)


@pytest.mark.parametrize("extra,shown,kind", [
    (["--stage-index", "2"], {2}, "dispatch-stage-brief"),
    ([], {"meta", 1, 2, 3, 4}, "dispatch-no-stage"),
], ids=["with-stage-index", "without-stage-index"])
def test_the_cursor_prompt_is_the_site_text_and_none_of_the_unshown_stages(
        extra, shown, kind, plan, tmp_path, monkeypatch):
    rc, prompt = fx.launch_cursor(monkeypatch, tmp_path, fx.cursor_argv(plan, *extra))

    selector = {"stage": 2} if kind == "dispatch-stage-brief" else {}
    from agentctl.render import build_site_prompt

    site = build_site_prompt(kind, plan, with_view=False, inline_max_chars=sys.maxsize, **selector)
    assert prompt is not None
    assert site.text in prompt
    assert "Plan view:" not in prompt
    assert not any(Path(os.environ["AGENTCTL_TOPO_UNITS_DIR"]).glob("*"))
    assert [t for t in fx.hidden_tokens(shown) if t in prompt] == []


def test_the_cursor_prompt_prints_a_field_over_the_inline_limit_in_full(tmp_path, monkeypatch):
    index = fx.write_hypertext_plan(tmp_path / "author", long_method=True)

    _, prompt = fx.launch_cursor(
        monkeypatch, tmp_path, fx.cursor_argv(index, "--stage-index", "2"))

    assert prompt is not None
    assert fx.LONG_FIELD in prompt
    assert re.search(r"see objects/[0-9a-f]{64}", prompt) is None


def test_the_stage_brief_label_does_not_promise_the_other_stages(plan, tmp_path):
    prompt = _spawner().assembled_prompt(
        fx.specialist_argv(plan, "--stage-index", "2", "--view-root", str(tmp_path / "views")))

    assert "a field too long to print here is read from the view directory" in prompt
    assert "the rest of the plan" not in prompt


def test_a_cursor_spawn_without_a_stage_index_sees_a_referenced_stage_in_full(
        tmp_path, monkeypatch):
    index = fx.write_hypertext_plan(tmp_path / "author")

    _, prompt = fx.launch_cursor(monkeypatch, tmp_path, fx.cursor_argv(index))

    assert prompt is not None
    assert [t for t in fx.shown_tokens({2}) if t not in prompt] == []
    assert "{ ref" not in prompt and "ref = " not in prompt


def test_a_plain_brief_given_as_plan_reaches_both_spawners_as_its_own_text(tmp_path, monkeypatch):
    brief = tmp_path / "brief.txt"
    brief.write_text("Difficulty: BRIEFSENTINEL expected X, actual Y.\n", encoding="utf-8")

    rc, prompt, _ = fx.launch_specialist(monkeypatch, tmp_path, fx.specialist_argv(brief))
    _, cursor_prompt = fx.launch_cursor(monkeypatch, tmp_path, fx.cursor_argv(brief))

    assert rc == 0
    assert "BRIEFSENTINEL" in prompt
    assert "BRIEFSENTINEL" in cursor_prompt


def test_a_plain_brief_over_the_limit_is_refused_before_launch(tmp_path, monkeypatch):
    from agentctl import render

    brief = tmp_path / "brief.txt"
    brief.write_text("z" * (render.NON_PLAN_BRIEF_MAX_CHARS + 1), encoding="utf-8")

    rc, prompt, cmd = fx.launch_specialist(monkeypatch, tmp_path, fx.specialist_argv(brief))

    assert rc == 2
    assert prompt is None and cmd is None


def test_the_stage_brief_and_the_handoff_record_carry_the_closure_digest(plan, tmp_path):
    from agentctl import plan as plan_module

    doc = plan_module.load_plan(str(plan))
    digest = plan_module.stage_closure_digest(doc, 2)
    state = SimpleNamespace(
        plan_path=str(plan), plan_snapshot_path=None, parent_link=None,
        approved_grant_entries=None, approved_grants_sha256=None,
        stage=lambda index: next(s for s in doc.stages if s.index == index),
        resolve_check_venue=lambda venue: str(tmp_path),
    )
    store = SimpleNamespace(load=lambda session_id: state)

    record = _spawner().norm_handoff_record("session", 2, "developer", 1, None, store=store)

    site = _site("dispatch-stage-brief", plan, tmp_path / "views", stage=2)
    assert f"Stage closure digest:** {digest}" in site.text
    assert record["closure_digest"] == digest


def test_the_handoff_record_carries_the_git_surface_of_the_workdir_and_the_launch_directory(plan, tmp_path):
    import subprocess
    from agentctl import plan as plan_module

    doc = plan_module.load_plan(str(plan))
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    state = SimpleNamespace(
        plan_path=str(plan), plan_snapshot_path=None, parent_link=None,
        approved_grant_entries=None, approved_grants_sha256=None,
        stage=lambda index: next(s for s in doc.stages if s.index == index),
        resolve_check_venue=lambda venue: str(tmp_path),
    )
    store = SimpleNamespace(load=lambda session_id: state)

    record = _spawner().norm_handoff_record("session", 2, "developer", 1, None, store=store, workdir=str(repo))

    assert record["launch_dir"] == str(repo)
    assert list(record["git_surface"]["repos"]) == [str(repo.resolve() / ".git")]


def test_the_handoff_record_carries_runs_worktree_code_only_from_the_approved_snapshot(plan, tmp_path, monkeypatch):
    from agentctl import plan as plan_module
    from agentctl.grants import StageGrants

    spawner = _spawner()
    doc = plan_module.load_plan(str(plan))
    state = SimpleNamespace(
        plan_path=str(plan), plan_snapshot_path="snapshot", parent_link=None,
        approved_grant_entries=None, approved_grants_sha256=None,
        stage=lambda index: next(s for s in doc.stages if s.index == index),
        resolve_check_venue=lambda venue: str(tmp_path),
    )
    store = SimpleNamespace(load=lambda session_id: state)

    def record_with(declared):
        approved = plan_module.load_plan(str(plan))
        next(s for s in approved.stages if s.index == 2).grants = StageGrants(runs_worktree_code=declared)
        monkeypatch.setattr(spawner.plan_store, "load_baseline", lambda *a: approved)
        return spawner.norm_handoff_record("session", 2, "developer", 1, None, store=store)

    assert "runs_worktree_code" not in record_with(False)
    assert record_with(True)["runs_worktree_code"] is True
    state.plan_snapshot_path = None
    assert "runs_worktree_code" not in spawner.norm_handoff_record("session", 2, "developer", 1, None, store=store)


def test_a_redispatch_keeps_the_digest_of_every_repository_the_prior_record_knew():
    spawner = _spawner()
    record = {"git_surface": {"schema": 1, "repos": {"/a/.git": {"digest": "new"}, "/b/.git": {"digest": "fresh"}}}}
    spawner.carry_git_surface_forward(record, {"git_surface": {"schema": 1, "repos": {"/a/.git": {"digest": "old"}}}})
    assert record["git_surface"]["repos"] == {"/a/.git": {"digest": "old"}, "/b/.git": {"digest": "fresh"}}
    untouched = {"git_surface": {"schema": 1, "repos": {"/a/.git": {"digest": "new"}}}}
    spawner.carry_git_surface_forward(untouched, None)
    spawner.carry_git_surface_forward(untouched, {"git_surface": None})
    assert untouched["git_surface"]["repos"] == {"/a/.git": {"digest": "new"}}


# --- the former inline trigger -----------------------------------------------------------
# Until a spawn met four conditions together (a kind allowed to read the plans directory,
# `--plan-brief`, `--stage-index`, a plan inside the plans directory) it received the whole
# plan inline. These tests name no symbol of the site builder and use no shared fixture: they
# run the spawner as it is and look for stage text in the prompt it launches.

FORMER_FIELDS = ("method", "procedure", "knowledge", "invariants")


def _former_stage(n: int) -> str:
    lines = [
        "[[stage]]", f"index = {n}", f'title = "Stage {n} title"',
        'executor = "spawn:developer"', f'expected_result_image = "result image of stage {n}"',
        'criterion_type = "measurable"', f'done_criterion = "done criterion of stage {n}"',
        'verify_command = "pytest -q"', f'output_artifacts = ["out/stage{n}.txt"]',
        *(f'{field} = "FORMERSENTINEL-{n}-{field}"' for field in FORMER_FIELDS),
        'material = "m"', 'means = "m"', 'preconditions = "p"', 'conditions = "c"',
        'capability_required = "c"',
        "[stage.principle]", 'statement = "s"', 'source = "s"', 'derivation = "d"',
        'confidence = "high"', 'refutation = "r"',
    ]
    if n > 1:
        lines += ["[[stage.supplies]]", f"on = {n - 1}"]
    return "\n".join(lines) + "\n"


FORMER_PLAN = "\n".join([
    "[meta]", 'task_id = "former-trigger"', 'goal = "goal of the former-trigger plan"',
    'done_criterion = "dc"', 'criterion_type = "measurable"', 'weight_class = "substantive"',
    'external_research = "none applies"', "",
    *map(_former_stage, (1, 2, 3)),
    "[[final_check]]", 'command = "true"', 'label = "former check"', "",
])


class _FormerProc:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdin_input = None
        self.pid = 424242

    def communicate(self, input=None):
        self.stdin_input = input
        return ('{"result": "COMPLETED: ok", "cost_usd": 0}', "")


def _former_launch(monkeypatch, tmp_path, argv, plans):
    spec = importlib.util.spec_from_file_location("spawn_specialist_former", SCRIPTS / "spawn-specialist.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["spawn_specialist_former"] = mod
    spec.loader.exec_module(mod)
    captured: dict = {}

    def fake_launch(cmd, **kwargs):
        captured["proc"] = _FormerProc()
        return captured["proc"]

    monkeypatch.setattr(mod.proc_tree, "launch_supervised", fake_launch)
    monkeypatch.setattr(mod.proc_tree, "install_teardown", lambda p: None)
    monkeypatch.setattr(mod.proc_tree, "kill_tree", lambda p: None)
    monkeypatch.setattr(mod, "_snapshot_transcripts", lambda *a, **k: set())
    monkeypatch.setattr(mod, "_discover_transcript_path", lambda *a, **k: None)
    monkeypatch.setattr(mod, "permissions_digest", lambda *a, **k: "")
    monkeypatch.setattr(mod, "log_cost_entry", lambda entry: None)
    monkeypatch.setattr(mod, "log_refused", lambda *a, **k: None)
    monkeypatch.setattr(mod, "deregister_child_scope", lambda *a, **k: None)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/claude")
    sysprompt = tmp_path / "sysprompt.md"
    sysprompt.write_text("system prompt", encoding="utf-8")
    monkeypatch.setattr(mod, "composed_system_prompt_file", lambda skill: sysprompt)
    monkeypatch.setattr(mod, "plans_dir", lambda: plans)
    rc = mod.main(argv)
    proc = captured.get("proc")
    return rc, (proc.stdin_input if proc else None)


def _former_argv(plan: Path, *extra: str, kind: str = "developer") -> list[str]:
    return ["--kind", kind, "--plan", str(plan), "--done-criterion", "done",
            "--criterion-type", "measurable", "--complexity", "medium", "--effort", "medium",
            *extra]


def _former_sentinels(stages) -> list[str]:
    return [f"FORMERSENTINEL-{n}-{field}" for n in stages for field in FORMER_FIELDS]


def _former_run(monkeypatch, tmp_path, argv_for, *, in_plans_dir: bool):
    plans = tmp_path / "plans"
    directory = plans if in_plans_dir else tmp_path / "elsewhere"
    directory.mkdir(parents=True)
    plan = directory / "former-trigger.toml"
    plan.write_text(FORMER_PLAN, encoding="utf-8")
    rc, prompt = _former_launch(monkeypatch, tmp_path, argv_for(plan), plans)
    assert rc == 0
    assert "Stage 2 title" in prompt, "the plan loaded and reached the prompt as a plan view"
    return prompt


def test_former_inline_trigger_kind_outside_the_plans_read_kinds(monkeypatch, tmp_path):
    prompt = _former_run(
        monkeypatch, tmp_path,
        lambda plan: _former_argv(plan, "--stage-index", "2", "--plan-brief", kind="tech-writer"),
        in_plans_dir=True)

    assert [s for s in _former_sentinels((1, 3)) if s in prompt] == []


def test_former_inline_trigger_argv_without_a_stage_index(monkeypatch, tmp_path):
    prompt = _former_run(
        monkeypatch, tmp_path, lambda plan: _former_argv(plan, "--plan-brief"),
        in_plans_dir=True)

    assert [s for s in _former_sentinels((1, 2, 3)) if s in prompt] == []


def test_former_inline_trigger_stage_index_without_plan_brief(monkeypatch, tmp_path):
    prompt = _former_run(
        monkeypatch, tmp_path, lambda plan: _former_argv(plan, "--stage-index", "2"),
        in_plans_dir=True)

    assert [s for s in _former_sentinels((1, 3)) if s in prompt] == []


def test_former_inline_trigger_plan_outside_the_plans_directory(monkeypatch, tmp_path):
    prompt = _former_run(
        monkeypatch, tmp_path,
        lambda plan: _former_argv(plan, "--stage-index", "2", "--plan-brief"),
        in_plans_dir=False)

    assert [s for s in _former_sentinels((1, 3)) if s in prompt] == []
