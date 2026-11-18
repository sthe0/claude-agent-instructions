"""A re-review history longer than `review-history-inline-max-chars` reaches the reviewer
as an index built by code, with the diff and the settled concerns in view files it opens on
demand; a shorter one is printed exactly as before."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

import plan_site_fixture as fx
from agentctl import config, render
from agentctl.plan import load_plan
from agentctl.render import (
    PRIOR_REVIEWS_FILE, build_site_prompt, diff_segments, row_diff, topo_unit_refusal,
    verify_site_view,
)

PAIR = "2-1"
OPEN_TEXT = "QOPENx the scaffold breaks the interface"
SETTLED_TEXT = "QSETTLEDx the wording could be tighter"
OLD_IMAGE = "QOLDIMAGEx result image of stage 1 before the edit"
OLD_METHOD = "QOLDMETHODx method of stage 2 before the edit"
INLINE_LIMIT = 200


def _concern(cid: str, text: str, *, unresolved: bool) -> dict:
    return {"id": cid, "severity": "blocking", "effective_severity": "blocking",
            "parts": ["stage:1"], "text": text, "unresolved": unresolved}


def _write(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plan.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _prior_text(*, old_image: str = OLD_IMAGE) -> str:
    return (fx.plan_text()
            .replace("result image of stage 1", old_image)
            .replace(fx._value(2, "method"), OLD_METHOD))


def _history(live: Path, prior: Path, review_id: str = PAIR) -> dict:
    diff = row_diff(load_plan(str(prior)), load_plan(str(live)), review_id)
    assert diff
    return {
        "pair": review_id,
        "records": [{
            "record_seq": 7, "reviewer_verdict": "revise", "effective_verdict": "revise",
            "concerns": [_concern(f"{review_id}#7.c1", OPEN_TEXT, unresolved=True),
                         _concern(f"{review_id}#7.c2", SETTLED_TEXT, unresolved=False)],
        }],
        "changed_parts_since_last": ["stage:1", "stage:2"],
        "prior_diff": {"available": True, "from_sha": "0" * 64, "text": diff},
    }


@pytest.fixture
def plans(tmp_path):
    live = fx.write_plan(tmp_path / "live")
    prior = _write(tmp_path / "prior", _prior_text())
    return live, prior


def _limit(monkeypatch, value):
    monkeypatch.setattr(render, "review_history_inline_limit", lambda: value)


def _view_bytes(site) -> dict[str, bytes]:
    return {name: (site.view_dir / name).read_bytes() for name in site.view_files}


def test_the_limit_is_the_config_row():
    assert config.review_history_inline_limit() == 20000


@pytest.mark.parametrize("review_id,kind,selector", [
    (PAIR, "topo-pair", "pair"), ("unit:2", "topo-unit", "unit")])
def test_a_history_within_the_limit_is_printed_byte_for_byte_as_before(
        review_id, kind, selector, plans, tmp_path, monkeypatch):
    live, prior = plans
    history = _history(live, prior, review_id)

    within = build_site_prompt(kind, live, view_root=tmp_path / "v", history=history,
                               **{selector: review_id})
    _limit(monkeypatch, None)
    unlimited = build_site_prompt(kind, live, view_root=tmp_path / "v", history=history,
                                  **{selector: review_id})

    assert within.text == unlimited.text
    assert within.view_dir == unlimited.view_dir and within.view_files == unlimited.view_files
    assert "```diff" in within.text and SETTLED_TEXT in within.text
    assert not any(name.startswith("history/") for name in within.view_files)


def test_a_history_over_the_limit_is_an_index_of_verified_view_files(plans, tmp_path, monkeypatch):
    live, prior = plans
    history = _history(live, prior)
    _limit(monkeypatch, INLINE_LIMIT)

    site = build_site_prompt("topo-pair", live, view_root=tmp_path / "v", pair=PAIR, history=history)

    assert "```diff" not in site.text
    assert OLD_IMAGE not in site.text and OLD_METHOD not in site.text
    verify_site_view(site.view_dir, _view_bytes(site))
    diff_files = sorted(n for n in site.view_files if re.fullmatch(r"history/diff-\d{3}-.*\.diff", n))
    assert "".join((site.view_dir / n).read_text(encoding="utf-8") for n in diff_files) == \
        history["prior_diff"]["text"] + "\n"
    rows = re.findall(r"^\| (.+?) \| (.+?) \| (\d+) \| (\d+) \| `(.+?)` \|$", site.text, re.M)
    assert [Path(row[4]) for row in rows] == [site.view_dir / n for n in diff_files]
    assert [row[:4] for row in rows] == [("stage 2", "Method", "1", "1"),
                                         ("stage 1", "Expected result image", "1", "1")]

    assert OPEN_TEXT in site.text and SETTLED_TEXT not in site.text
    assert "1 concern(s) inline, 1 in the file" in site.text
    assert str(site.view_dir / PRIOR_REVIEWS_FILE) in site.text
    assert SETTLED_TEXT in (site.view_dir / PRIOR_REVIEWS_FILE).read_text(encoding="utf-8")
    assert "open the ones your question needs" in site.text


def test_a_unit_whose_history_is_indexed_is_granted_a_view_of_the_history_files(
        plans, tmp_path, monkeypatch):
    live, prior = plans
    _limit(monkeypatch, INLINE_LIMIT)

    site = build_site_prompt("topo-unit", live, view_root=tmp_path / "v", unit="unit:2",
                             history=_history(live, prior, "unit:2"))

    assert site.add_dirs == (str(site.view_dir),)
    assert site.view_files and all(n.startswith("history/") or n == "MANIFEST.json"
                                   for n in site.view_files)
    verify_site_view(site.view_dir, _view_bytes(site))
    assert "```diff" not in site.text and str(site.view_dir) in site.text


def test_the_diff_segments_join_back_into_the_diff_and_none_for_a_changed_plan(plans, tmp_path):
    live, prior = plans
    diff = _history(live, prior)["prior_diff"]["text"]

    segments = diff_segments(diff, load_plan(str(live)), PAIR)

    assert "\n".join(line for _, run in segments for line in run) == diff
    assert diff_segments(diff, load_plan(str(prior)), PAIR) is None


def _topo():
    return fx.load_script("plan-review-topological.py", "topo_history_index")


def test_a_pair_over_the_ceiling_inline_but_under_it_as_an_index_is_not_refused(
        tmp_path, monkeypatch, capsys):
    live = fx.write_plan(tmp_path / "live")
    prior = _write(tmp_path / "prior", _prior_text(old_image="Q" * 240_000))
    history_path = tmp_path / "history.json"
    history_path.write_text(json.dumps(_history(live, prior)), encoding="utf-8")
    views = tmp_path / "views"
    topo = _topo()
    doc = load_plan(str(live))

    with monkeypatch.context() as unlimited:
        _limit(unlimited, None)
        inline = topo.reviewer_prompt(str(live), PAIR, view_root=views, history_path=str(history_path))
    assert topo_unit_refusal(doc, ("meta", 2, 1), inline) is not None

    driver = topo.Driver(argparse.Namespace(session="s", view_root=str(views), pairs=None,
                                            model=None, complexity=None), str(live), {})
    assert driver.size_refusal(PAIR, str(history_path)) is None
    indexed = topo.reviewer_prompt(str(live), PAIR, view_root=views, history_path=str(history_path))
    assert len(indexed) < len(inline)

    spawner = fx.load_script("spawn-specialist.py", "spawn_history_index")
    monkeypatch.setattr(spawner, "COST_LOG", tmp_path / "costs.jsonl")
    argv = topo.build_spawn_argv(argparse.Namespace(model=None, complexity=None), PAIR, str(live),
                                 dry_run=True, history_path=str(history_path), view_root=str(views))
    assert spawner.main(argv) == 0
    stdin = re.search(r"# stdin: <prompt (\d+) chars>", capsys.readouterr().out)
    assert stdin is not None and int(stdin.group(1)) == len(indexed)


def test_a_read_of_a_history_diff_file_counts_as_a_pull(plans, tmp_path, monkeypatch):
    live, prior = plans
    history_path = tmp_path / "history.json"
    history = _history(live, prior, "unit:2")
    history_path.write_text(json.dumps(history), encoding="utf-8")
    _limit(monkeypatch, INLINE_LIMIT)
    topo = _topo()
    view = topo.view_dir_for(str(live), "unit:2", str(tmp_path / "v"), history_path=str(history_path))
    site = build_site_prompt("topo-unit", live, view_root=tmp_path / "v", unit="unit:2",
                             history=history)
    assert view == str(site.view_dir)
    diff_file = site.view_dir / next(n for n in site.view_files if n.endswith(".diff"))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": str(diff_file)}}]}})
        + "\n", encoding="utf-8")

    assert topo.count_pulls(str(transcript), view) == 1
