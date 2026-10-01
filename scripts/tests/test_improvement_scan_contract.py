"""Phase B's classifications contract: judgment fields from the model, metadata from --worklist.

The model supplies only what judgment adds (breadth, cost_to_resolve, in_flight,
recommended_next_step, blocked_by); the item metadata comes from the phase-A worklist.
A gap is a named exit-2 error, never an empty title or ground.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan_contract", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()

_JUDGMENT = {
    "breadth": "narrow", "cost_to_resolve": "small", "in_flight": "none",
    "recommended_next_step": "planner",
}


def _worklist_item(ref="ref-1", **over):
    item = {
        "item_ref": ref, "bucket": "new", "title": "worklist title",
        "functional_ground": "worklist ground", "severity": "high",
        "severity_labeled": True, "reporter": "r", "evidence": "ev",
        "cost_estimate": "", "source_digest": "digest-1",
    }
    item.update(over)
    return item


def _run(tmp_path, classified, worklist=None, closed=()):
    cls = tmp_path / "classifications.json"
    cls.write_text(json.dumps({"items": classified, "closed_refs": list(closed)}), encoding="utf-8")
    wl_path = None
    if worklist is not None:
        wl_path = tmp_path / "worklist.json"
        wl_path.write_text(json.dumps({"items": worklist, "closed_refs": []}), encoding="utf-8")
    out = tmp_path / "board.json"
    args = argparse.Namespace(
        prior=None, classifications=str(cls), worklist=str(wl_path) if wl_path else None,
        out=str(out), store=str(tmp_path / "store.jsonl"),
    )
    return scan._run_backlog_phase_b(args), out


def test_phase_b_merges_worklist_metadata(tmp_path, capsys):
    rc, out = _run(tmp_path, {"ref-1": dict(_JUDGMENT)}, worklist=[_worklist_item()])
    assert rc == 0
    item = json.loads(out.read_text(encoding="utf-8"))["items"]["ref-1"]
    assert item["title"] == "worklist title"
    assert item["functional_ground"] == "worklist ground"
    assert item["source_digest"] == "digest-1"
    assert item["severity_labeled"] is True
    assert item["score"] is not None


def test_classification_overrides_worklist_field(tmp_path):
    rc, out = _run(
        tmp_path, {"ref-1": {**_JUDGMENT, "title": "retitled"}}, worklist=[_worklist_item()]
    )
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["items"]["ref-1"]["title"] == "retitled"


def test_unknown_ref_exits_2(tmp_path, capsys):
    rc, out = _run(tmp_path, {"ghost": dict(_JUDGMENT)}, worklist=[_worklist_item()])
    assert rc == 2
    assert "ghost" in capsys.readouterr().err
    assert not out.exists()


def test_missing_metadata_without_worklist_exits_2(tmp_path, capsys):
    rc, out = _run(tmp_path, {"ref-1": dict(_JUDGMENT)})
    assert rc == 2
    err = capsys.readouterr().err
    assert "ref-1" in err and "title" in err
    assert not out.exists()


def test_self_contained_classification_works_without_worklist(tmp_path):
    full = {
        **_JUDGMENT, "title": "t", "functional_ground": "g", "severity": "high",
        "source_digest": "d", "severity_labeled": True,
    }
    rc, out = _run(tmp_path, {"ref-1": full})
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["items"]["ref-1"]["score"] is not None


def test_string_false_severity_labeled_is_not_true(tmp_path):
    rc, out = _run(
        tmp_path, {"ref-1": dict(_JUDGMENT)},
        worklist=[_worklist_item(severity_labeled="false")],
    )
    assert rc == 0
    item = json.loads(out.read_text(encoding="utf-8"))["items"]["ref-1"]
    assert item["severity_labeled"] is False
    assert item["score"] is None


def test_worklist_lacking_severity_labeled_key_is_unlabeled(tmp_path):
    old = _worklist_item()
    del old["severity_labeled"]
    rc, out = _run(tmp_path, {"ref-1": dict(_JUDGMENT)}, worklist=[old])
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["items"]["ref-1"]["severity_labeled"] is False
