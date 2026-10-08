"""Tests for the measured-loss ranking: `improvement_scan_loss.py` (the pure half) and
improvement-scan.py's `loss` subcommand, board fields, amendment protocol and report lanes.

Every transcript here is a synthetic JSONL file written under `tmp_path`; no real transcript is
read. The new modules are reached only inside test bodies so that, against a tree that lacks
them, each test fails on its own assertion rather than on collection.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import self_diagnose_store as sds  # noqa: E402


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan_loss_tests", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _loss():
    return importlib.import_module("improvement_scan_loss")


# --- synthetic transcripts ---------------------------------------------------

def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _use(tid: str, command: str, days_ago: float = 2) -> dict:
    return {
        "type": "assistant", "timestamp": _ts(days_ago),
        "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}
        ]},
    }


def _result(tid: str, text: str, days_ago: float = 2) -> dict:
    return {
        "type": "user", "timestamp": _ts(days_ago),
        "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]},
    }


def _hit(text: str, *, command: str = "pytest -q", tid: str = "t1", days_ago: float = 2) -> list:
    return [_use(tid, command, days_ago), _result(tid, text, days_ago)]


def _transcript(root: Path, sid: str, entries: list, *, subagent: "str | None" = None) -> Path:
    directory = root / "proj"
    name = sid
    if subagent:
        directory = directory / sid / "subagents"
        name = subagent
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


# --- board and CLI helpers -----------------------------------------------------

def _item(**over):
    base = dict(
        classification="narrow", score=None, rank=None, source_digest="d", title="an item",
        functional_ground="a ground", recommended_next_step="planner", severity="medium",
        severity_labeled=True, cost_to_resolve="small", old_score=1.0,
    )
    base.update(over)
    return scan.PriorBoardItem(**base)


class _Paths:
    def __init__(self, tmp_path: Path):
        self.board = tmp_path / "board.json"
        self.store = tmp_path / "findings.jsonl"
        self.cache = tmp_path / "hits.json"
        self.root = tmp_path / "projects"
        self.root.mkdir(exist_ok=True)

    def write_board(self, items: dict) -> None:
        scan.write_board(
            scan.PriorBoard(schema=scan.BOARD_SCHEMA, generated_at=NOW.isoformat(), items=items),
            self.board,
        )

    def board_items(self) -> dict:
        return scan.load_prior_board(self.board).items

    def loss_argv(self, *extra: str, days: int = 14) -> list:
        return [
            "loss", "--board-state", str(self.board), "--store", str(self.store),
            "--projects-root", str(self.root), "--hits-cache", str(self.cache),
            "--until", NOW.isoformat(), "--days", str(days), *extra,
        ]

    def run_loss(self, *extra: str, days: int = 14) -> int:
        return scan.main(self.loss_argv(*extra, days=days))

    def amend(self, tmp_path: Path, items: dict) -> int:
        classifications = tmp_path / "classifications.json"
        classifications.write_text(json.dumps({"items": items}), encoding="utf-8")
        return scan.main([
            "backlog", "--board-state", str(self.board), "--classifications", str(classifications),
            "--store", str(self.store),
        ])


def _sessions(paths: _Paths, signature: str, count: int, prefix: str) -> None:
    for i in range(count):
        _transcript(paths.root, f"{prefix}{i}", _hit(f"boom {signature} happened"))


# --- tests ---------------------------------------------------------------------

def test_inversion_frequent_unlabelled_outranks_universal_critical_zero(tmp_path):
    paths = _Paths(tmp_path)
    paths.write_board({
        "frequent": _item(
            title="frequent", severity_labeled=False, old_score=1.0, cost_to_resolve="large",
            signatures=("ERR-FREQ",), minutes_per_occurrence=5.0, minutes_basis="re-run by hand",
        ),
        "critical": _item(
            title="critical", severity="critical", classification="universal", old_score=10.0,
            cost_to_resolve="small",
            signatures=("ERR-CRIT",), minutes_per_occurrence=30.0, minutes_basis="outage",
        ),
    })
    _sessions(paths, "ERR-FREQ", 3, "f")

    assert paths.run_loss() == 0

    items = paths.board_items()
    assert items["frequent"].loss_measurement["sessions_hit"] == 3
    assert items["critical"].loss_measurement["sessions_hit"] == 0
    assert items["frequent"].rank == 1 and items["critical"].rank == 2
    assert items["critical"].old_score > items["frequent"].old_score
    lanes = _loss().build_lanes(items)
    assert [row["ref"] for row in lanes["loss"]] == ["frequent", "critical"]
    assert lanes["loss"][0]["min_per_week"] == 7.5
    assert lanes["loss"][1]["min_per_week"] == 0.0


def test_signatureless_item_lands_in_silent_lane():
    loss = _loss()
    measured = {"window_days": 14, "sessions": ["a", "b"]}
    items = {
        "measured": _item(signatures=("X",), minutes_per_occurrence=7.0, loss_measurement=measured),
        "quiet": _item(silent_estimate="~20 min/week re-explaining the same context"),
        "unclassified": _item(),
    }

    lanes = loss.build_lanes(items)

    assert [row["ref"] for row in lanes["loss"]] == ["measured"]
    assert [row["ref"] for row in lanes["silent"]] == ["quiet"]
    assert lanes["silent"][0]["silent_estimate"] == "~20 min/week re-explaining the same context"
    assert [row["ref"] for row in lanes["awaiting"]] == ["unclassified"]
    assert loss.lane_ranks(lanes) == {"measured": 1, "quiet": 2, "unclassified": 3}


def _family_row(tmp_path: Path, name: str, items: dict, transcripts: dict) -> dict:
    paths = _Paths(tmp_path / name)
    paths.write_board(items)
    for sid, text in transcripts.items():
        _transcript(paths.root, sid, _hit(text))
    assert paths.run_loss() == 0
    lanes = _loss().build_lanes(paths.board_items())
    assert len(lanes["loss"]) == 1
    return lanes["loss"][0]


def test_family_not_double_counted(tmp_path):
    (tmp_path / "same").mkdir()
    (tmp_path / "disjoint").mkdir()

    same = _family_row(
        tmp_path, "same",
        {
            "A": _item(family="fam", signatures=("SIG-A",), minutes_per_occurrence=4.0),
            "B": _item(family="fam", signatures=("SIG-B",), minutes_per_occurrence=4.0),
        },
        {"s1": "SIG-A and SIG-B", "s2": "SIG-A and SIG-B"},
    )
    assert same["sessions_hit"] == 2
    assert same["min_per_week"] == 4.0  # 2 sessions x 4 min / 2 weeks, as for either member alone

    disjoint = _family_row(
        tmp_path, "disjoint",
        {
            "A": _item(family="fam", signatures=("SIG-A",), minutes_per_occurrence=60.0),
            "B": _item(family="fam", signatures=("SIG-B",), minutes_per_occurrence=1.0),
        },
        {"a1": "SIG-A", **{f"b{i}": "SIG-B" for i in range(10)}},
    )
    loss_a, loss_b = 1 * 60.0 / 2, 10 * 1.0 / 2  # each member's separately computed min/week
    assert disjoint["sessions_hit"] == 11
    assert max(loss_a, loss_b) <= disjoint["min_per_week"] <= loss_a + loss_b
    assert disjoint["min_per_week"] != 11 * 60.0 / 2


def test_family_partially_overlapping_sessions_counted_once(tmp_path):
    paths = _Paths(tmp_path)
    paths.write_board({
        "A": _item(family="fam", signatures=("SIG-A",), minutes_per_occurrence=4.0),
        "B": _item(family="fam", signatures=("SIG-B",), minutes_per_occurrence=4.0),
    })
    _transcript(paths.root, "s1", _hit("SIG-A"))
    _transcript(paths.root, "s2", _hit("SIG-A and SIG-B"))
    _transcript(paths.root, "s3", _hit("SIG-A and SIG-B"))
    _transcript(paths.root, "s4", _hit("SIG-B"))

    assert paths.run_loss() == 0

    lanes = _loss().build_lanes(paths.board_items())
    assert len(lanes["loss"]) == 1
    row = lanes["loss"][0]
    assert row["members"] == ["A", "B"]
    assert row["sessions_hit"] == 4
    assert row["min_per_week"] == 8.0  # 4 sessions x 4 min / 2 weeks; summing members would give 12


def test_unlabelled_item_gets_a_finding(tmp_path):
    classified = {
        "ref-unlabelled": {
            "breadth": "narrow", "cost_to_resolve": "small", "in_flight": "none",
            "recommended_next_step": "planner", "severity": "low",
            "functional_ground": "a ground nobody else shares", "title": "unlabelled item",
            "signatures": ["ERR-UNLABELLED"], "minutes_per_occurrence": 3,
        },
    }
    board, findings, no_urgency = scan.classify_and_score(
        scan._empty_board(), classified, [], config_path=scan.CONFIG_PATH
    )

    assert no_urgency == ["ref-unlabelled"]
    assert board.items["ref-unlabelled"].signatures == ("ERR-UNLABELLED",)
    assert [f.source_ref for f in findings] == ["ref-unlabelled"]
    assert findings[0].proxy_score is not None


def test_discussion_and_filing_hits_excluded(tmp_path):
    paths = _Paths(tmp_path)
    ref = "sthe0/repo#305"
    paths.write_board({
        ref: _item(signatures=("SIG-X",), minutes_per_occurrence=2.0),
    })
    root = paths.root
    _transcript(root, "filing", _hit("SIG-X", command="gh issue view 305"))
    _transcript(root, "quoting-sig", _hit("SIG-X", command="grep SIG-X notes.md"))
    _transcript(root, "hash-ref", _hit("SIG-X", command="cat the-thread-for-#305.txt"))
    _transcript(root, "issues-url", _hit("SIG-X", command="curl .../issues/305"))
    _transcript(root, "assistant-text", [{
        "type": "assistant", "timestamp": _ts(2),
        "message": {"content": [{"type": "text", "text": "I see SIG-X in the log"}]},
    }])
    _transcript(root, "user-text", [{
        "type": "user", "timestamp": _ts(2), "message": {"content": "please look at SIG-X"},
    }])
    _transcript(root, "real-tool-result", _hit("crash: SIG-X"))
    _transcript(root, "system-entry", [{
        "type": "system", "timestamp": _ts(2), "content": "hook failed: SIG-X",
    }])
    _transcript(root, "longer-number", _hit("SIG-X", command="see #3055 for context"))

    assert paths.run_loss() == 0

    measurement = paths.board_items()[ref].loss_measurement
    assert measurement["sessions"] == ["longer-number", "real-tool-result", "system-entry"]


def test_only_hook_attachments_count(tmp_path):
    paths = _Paths(tmp_path)
    paths.write_board({"item": _item(signatures=("SIG-H",), minutes_per_occurrence=2.0)})
    _transcript(paths.root, "hook", [{
        "type": "attachment", "timestamp": _ts(2),
        "attachment": {"type": "hook_success", "content": "SIG-H raised by a hook"},
    }])
    _transcript(paths.root, "file-attachment", [{
        "type": "attachment", "timestamp": _ts(2),
        "attachment": {"type": "file", "content": "notes quoting SIG-H"},
    }])
    _transcript(paths.root, "memory-attachment", [{
        "type": "attachment", "timestamp": _ts(2),
        "attachment": {"type": "nested_memory", "content": "SIG-H in a memory leaf"},
    }])

    assert paths.run_loss() == 0

    assert paths.board_items()["item"].loss_measurement["sessions"] == ["hook"]


def test_precision_scales_and_goes_stale_on_signature_change(tmp_path):
    paths = _Paths(tmp_path)
    paths.write_board({
        "item": _item(signatures=("SIG-P",), minutes_per_occurrence=10.0, minutes_basis="measured"),
    })
    _sessions(paths, "SIG-P", 3, "p")

    assert paths.amend(tmp_path, {
        "item": {"precision": 0.5, "precision_sample": {"n": 10, "true": 5}},
    }) == 0
    assert paths.run_loss() == 0
    lanes = _loss().build_lanes(paths.board_items())
    assert lanes["loss"][0]["min_per_week"] == 7.5  # 3 sessions x 0.5 x 10 min / 2 weeks
    assert "precision 0.50 (5/10 sampled)" in lanes["loss"][0]["basis"]

    assert paths.amend(tmp_path, {"item": {"signatures": ["SIG-P", "SIG-OTHER"]}}) == 0
    assert paths.run_loss() == 0
    lanes = _loss().build_lanes(paths.board_items())
    assert lanes["loss"][0]["min_per_week"] == 15.0  # stale precision counts as 1.0
    assert "precision unjudged (upper bound)" in lanes["loss"][0]["basis"]


def test_emit_samples_is_deterministic(tmp_path):
    paths = _Paths(tmp_path)
    loss = _loss()
    judged_signatures = ("SIG-JUDGED",)
    paths.write_board({
        "needs": _item(signatures=("SIG-N",), minutes_per_occurrence=2.0),
        "judged": _item(
            signatures=judged_signatures, minutes_per_occurrence=2.0, precision=0.8,
            signatures_digest=loss.signatures_digest(judged_signatures),
        ),
    })
    _sessions(paths, "SIG-N", 6, "n")
    _sessions(paths, "SIG-JUDGED", 3, "j")
    first, second = tmp_path / "samples-1.json", tmp_path / "samples-2.json"

    assert paths.run_loss("--emit-samples", str(first), "--sample-size", "3") == 0
    assert paths.run_loss("--emit-samples", str(second), "--sample-size", "3") == 0

    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert list(payload["items"]) == ["needs"]
    samples = payload["items"]["needs"]["samples"]
    assert len(samples) == 3
    assert len({s["session"] for s in samples}) == 3
    assert all("SIG-N" in s["excerpt"] for s in samples)
    assert payload["items"]["needs"]["signatures_digest"] == loss.signatures_digest(("SIG-N",))


def test_hit_cache_rescans_only_changed_transcripts(tmp_path):
    loss = _loss()
    root = tmp_path / "projects"
    paths = [
        _transcript(root, f"c{i}", _hit("cached SIG-C")) for i in range(3)
    ]
    specs = {"item": (("SIG-C",), "")}
    since, until = loss.window_bounds(14, NOW)
    cache_file = tmp_path / "cache.json"

    cache = loss.load_hit_cache(cache_file)
    sessions, opened = loss.count_sessions(specs, paths, cache, since=since, until=until)
    assert opened == 3 and sessions["item"] == {"c0", "c1", "c2"}
    loss.save_hit_cache(cache, cache_file)

    cache = loss.load_hit_cache(cache_file)
    sessions, opened = loss.count_sessions(specs, paths, cache, since=since, until=until)
    assert opened == 0 and sessions["item"] == {"c0", "c1", "c2"}

    with paths[1].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_result("t9", "again SIG-C")) + "\n")
    sessions, opened = loss.count_sessions(specs, paths, cache, since=since, until=until)
    assert opened == 1 and sessions["item"] == {"c0", "c1", "c2"}


def test_old_board_loads_and_lists_awaiting_and_loss_unclassified(tmp_path, capsys):
    paths = _Paths(tmp_path)
    paths.board.write_text(json.dumps({
        "schema": 1, "generated_at": NOW.isoformat(),
        "items": {
            "old-1": {
                "classification": "narrow", "score": 2.0, "rank": 1, "source_digest": "d1",
                "title": "an item from the old board", "functional_ground": "old ground",
                "recommended_next_step": "planner",
            },
        },
    }), encoding="utf-8")

    prior = scan.load_prior_board(paths.board)
    item = prior.items["old-1"]
    assert item.signatures == () and item.loss_measurement is None and item.family == ""

    lanes = _loss().build_lanes(prior.items)
    assert lanes["loss"] == [] and lanes["silent"] == []
    assert [row["ref"] for row in lanes["awaiting"]] == ["old-1"]

    worklist = scan.build_worklist([], [], [], [], prior=prior)
    assert [w["item_ref"] for w in worklist["loss_unclassified"]] == ["old-1"]
    assert worklist["items"] == []

    assert scan.main(["report", "--store", str(paths.store), "--board", str(paths.board)]) == 0
    assert "## Awaiting loss classification" in capsys.readouterr().out


def test_loss_amendment_on_carried_item_and_bad_shape_exits_2(tmp_path, capsys):
    paths = _Paths(tmp_path)
    paths.write_board({"carried": _item(title="carried")})

    assert paths.amend(tmp_path, {"carried": {
        "signatures": ["SIG-A", "SIG-A", "SIG-B"], "minutes_per_occurrence": 4,
        "minutes_basis": "stopwatch", "family": "fam-1",
    }}) == 0
    amended = paths.board_items()["carried"]
    assert amended.signatures == ("SIG-A", "SIG-B")
    assert amended.minutes_per_occurrence == 4.0
    assert amended.minutes_basis == "stopwatch" and amended.family == "fam-1"
    assert amended.title == "carried"
    assert [f["path"] for f in sds.load_rows(paths.store)] == ["carried"]

    before = paths.board.read_bytes()
    for bad in (
        {"carried": {"minutes_per_occurrence": -1}},
        {"carried": {"precision": 1.5}},
        {"carried": {"signatures": "not-a-list"}},
        {"carried": {"precision_sample": {"n": 2, "true": 5}}},
        {"unknown-ref": {"signatures": ["SIG-Z"]}},
    ):
        assert paths.amend(tmp_path, bad) == 2
        assert paths.board.read_bytes() == before
    err = capsys.readouterr().err
    assert "carried" in err and "unknown-ref" in err


def test_loss_rejects_a_non_positive_window(tmp_path, capsys):
    paths = _Paths(tmp_path)
    paths.write_board({"carried": _item(title="carried")})
    before = paths.board.read_bytes()

    assert paths.run_loss(days=0) == 2
    assert "--days" in capsys.readouterr().err
    assert paths.board.read_bytes() == before


def test_tie_break_by_severity_then_fix_cost_then_old_score():
    loss = _loss()
    measured = {"window_days": 14, "sessions": ["s1", "s2"]}

    def entry(**over):
        return _item(signatures=("X",), minutes_per_occurrence=7.0, loss_measurement=measured, **over)

    items = {
        "a-high-large": entry(severity="high", cost_to_resolve="large", old_score=1.0),
        "b-critical-large": entry(severity="critical", cost_to_resolve="large", old_score=1.0),
        "c-high-small-low-score": entry(severity="high", cost_to_resolve="small", old_score=1.0),
        "d-high-small-high-score": entry(severity="high", cost_to_resolve="small", old_score=5.0),
        "e-unlabelled": entry(severity="critical", severity_labeled=False, cost_to_resolve="small"),
    }

    lanes = loss.build_lanes(items)

    assert [row["ref"] for row in lanes["loss"]] == [
        "b-critical-large", "d-high-small-high-score", "c-high-small-low-score",
        "a-high-large", "e-unlabelled",
    ]


def test_loss_reemits_findings_for_all_board_items(tmp_path):
    paths = _Paths(tmp_path)
    items = {
        "measured": _item(title="measured", signatures=("SIG-M",), minutes_per_occurrence=3.0),
        "quiet": _item(title="quiet", silent_estimate="~10 min/week"),
        "unclassified": _item(title="unclassified", severity_labeled=False),
    }
    paths.write_board(items)
    scan.store_findings(
        scan.emit_board_findings(items),
        kinds=frozenset([sds.KIND_BACKLOG_ITEM]), store_path=paths.store,
    )
    _sessions(paths, "SIG-M", 2, "m")

    assert paths.run_loss() == 0

    assert {r["path"] for r in sds.load_rows(paths.store)} == {"measured", "quiet", "unclassified"}


def test_report_md_and_json_show_loss_columns_and_lanes(tmp_path, capsys):
    paths = _Paths(tmp_path)
    paths.write_board({
        "measured": _item(
            title="measured item", signatures=("SIG-R",), minutes_per_occurrence=6.0,
            minutes_basis="stopwatch",
        ),
        "quiet": _item(title="quiet item", silent_estimate="~15 min/week of silent re-work"),
        "unclassified": _item(title="unclassified item"),
    })
    _sessions(paths, "SIG-R", 4, "r")
    assert paths.run_loss() == 0
    capsys.readouterr()
    report = ["report", "--store", str(paths.store), "--board", str(paths.board)]

    assert scan.main(report) == 0
    markdown = capsys.readouterr().out
    assert "## Measured loss — min/week" in markdown
    assert "| min/week |" in markdown and "**12.0**" in markdown  # 4 sessions x 6 min / 2 weeks
    assert "## Silent lane — no observable signature" in markdown
    assert "~15 min/week of silent re-work" in markdown
    assert "## Awaiting loss classification" in markdown
    assert markdown.index("## Measured loss") < markdown.index("## Silent lane")

    assert scan.main([*report, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["ref"] for row in payload["loss"]] == ["measured"]
    assert payload["loss"][0]["min_per_week"] == 12.0
    assert [row["ref"] for row in payload["silent"]] == ["quiet"]
    assert [row["ref"] for row in payload["awaiting"]] == ["unclassified"]
    assert isinstance(payload["findings"], list)


def test_subagent_transcript_attributed_to_parent_session(tmp_path):
    loss = _loss()
    root = tmp_path / "projects"
    transcripts = [
        _transcript(root, "PARENT", _hit("SIG-S")),
        _transcript(root, "PARENT", _hit("SIG-S"), subagent="agent-a"),
        _transcript(root, "OTHER", _hit("SIG-S"), subagent="agent-b"),
        _transcript(root, "CLEAN", _hit("nothing here")),
    ]
    since, until = loss.window_bounds(14, NOW)

    sessions, _opened = loss.count_sessions(
        {"item": (("SIG-S",), "")}, transcripts, {}, since=since, until=until
    )

    assert sessions["item"] == {"PARENT", "OTHER"}
    assert loss.session_id_of(transcripts[1]) == "PARENT"
    assert loss.enumerate_transcripts([root]) == sorted(transcripts)


def test_window_excludes_entries_outside_days(tmp_path):
    loss = _loss()
    root = tmp_path / "projects"
    transcripts = [
        _transcript(root, "recent", _hit("SIG-W", days_ago=3)),
        _transcript(root, "old", _hit("SIG-W", days_ago=30)),
        _transcript(root, "future", _hit("SIG-W", days_ago=-2)),
        _transcript(root, "mixed", _hit("SIG-W", days_ago=40) + _hit("SIG-W", tid="t2", days_ago=5)),
    ]
    specs = {"item": (("SIG-W",), "")}

    since, until = loss.window_bounds(14, NOW)
    narrow, _ = loss.count_sessions(specs, transcripts, {}, since=since, until=until)
    since, until = loss.window_bounds(60, NOW)
    wide, _ = loss.count_sessions(specs, transcripts, {}, since=since, until=until)

    assert narrow["item"] == {"recent", "mixed"}
    assert wide["item"] == {"recent", "old", "mixed"}
