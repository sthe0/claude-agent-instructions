"""A multi-span Target parses intact and a backlog item is titled by its issue title."""
import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from difficulty_channel.adapters import github  # noqa: E402


def _issue(title: str, target_line: str) -> dict:
    body = "**Target:** " + target_line + "\n**Functional ground:** g\n"
    return {"title": title, "body": body, "labels": [], "number": 1}


def test_multi_span_target_parses_intact():
    raw = "`a.py` and `b.py`"
    assert github._parse_body_field("**Target:** " + raw, "Target") == raw


def test_single_span_target_unwrapped():
    assert github._parse_body_field("**Target:** `scripts/x.py`", "Target") == "scripts/x.py"


def test_worklist_title_is_issue_title():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan_title", SCRIPTS_DIR / "improvement-scan.py"
    )
    scan = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = scan
    spec.loader.exec_module(scan)
    record = github._issue_to_record(_issue("[core] Something broke", "`x.py`"))
    worklist = scan.build_worklist([("ref1", record)], [], [], [])
    assert worklist["items"][0]["title"] == "Something broke"
