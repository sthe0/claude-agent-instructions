"""Phase B CLI edges: --worklist merge through main(), and a malformed classifications shape."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan_review_nits", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()

CLASSIFICATION = {
    "breadth": "narrow", "cost_to_resolve": "small", "in_flight": "none",
    "recommended_next_step": "planner",
}
WORKLIST_ITEM = {
    "item_ref": "ref-1", "title": "t", "functional_ground": "g", "severity": "high",
    "severity_labeled": True, "source_digest": "d1",
}


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def _phase_b(tmp_path, monkeypatch, classifications, worklist=None):
    monkeypatch.setenv("IMPROVEMENT_SCAN_BOARD_STATE", str(tmp_path / "board.json"))
    argv = [
        "backlog", "--classifications", _write(tmp_path / "cls.json", classifications),
        "--store", str(tmp_path / "store.jsonl"),
    ]
    if worklist is not None:
        argv += ["--worklist", _write(tmp_path / "wl.json", worklist)]
    return scan.main(argv)


def test_cli_phase_b_merges_worklist_metadata(tmp_path, monkeypatch):
    rc = _phase_b(
        tmp_path, monkeypatch,
        {"items": {"ref-1": CLASSIFICATION}},
        worklist={"items": [WORKLIST_ITEM], "closed_refs": []},
    )
    assert rc == 0
    board = json.loads((tmp_path / "board.json").read_text(encoding="utf-8"))
    assert board["items"]["ref-1"]["title"] == "t"


def test_list_shaped_items_exits_2_with_message(tmp_path, monkeypatch, capsys):
    rc = _phase_b(tmp_path, monkeypatch, {"items": [CLASSIFICATION]})
    assert rc == 2
    assert "'items' must be an object" in capsys.readouterr().err


def test_search_timeout_constant_is_named_for_its_call():
    import improvement_scan_shell

    assert improvement_scan_shell._SEARCH_TIMEOUT_S == 60
    assert not hasattr(improvement_scan_shell, "_TIMEOUT_S")
