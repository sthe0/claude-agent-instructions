"""Stage 4: the resolution-confirmation quote must be the user's actual words.

A frontmatter field is trivial to fabricate; the session transcript is not.
The `--hook` mode of verify-experience-leaf cross-checks the
`resolution_confirmed_by_user` quote against the transcript JSONL:
  - quote present in a user message -> allow (exit 0)
  - transcript resolvable but quote absent -> block (exit 2)
  - transcript unresolvable -> graceful degrade, advisory only (exit 0)
"""
import importlib.util
import json
from pathlib import Path

# verify-experience-leaf.py is a hyphenated script, not an importable module.
_SPEC = importlib.util.spec_from_file_location(
    "verify_experience_leaf",
    Path(__file__).resolve().parents[1] / "verify-experience-leaf.py",
)
vel = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vel)


LEAF = """---
schema: leaf/v1
resolution_confirmed_by_user: "yes ship it"
---
body
"""


def _write_transcript(path: Path, user_texts):
    """Write a minimal session JSONL with the given user messages."""
    lines = []
    for txt in user_texts:
        lines.append(json.dumps({"type": "user", "message": {"content": txt}}))
    # a non-user record and a tool_result-style user record that must NOT count
    lines.append(json.dumps({"type": "assistant", "message": {"content": "ok"}}))
    lines.append(json.dumps({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "yes ship it"}]},
    }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- pure helper: transcript_confirms -----------------------------------------

def test_quote_found_returns_true(tmp_path):
    tp = tmp_path / "t.jsonl"
    _write_transcript(tp, ["please proceed", "Yes  ship   it now"])  # whitespace reflow
    status, _ = vel.transcript_confirms("yes ship it", str(tp))
    assert status is True


def test_quote_absent_returns_false(tmp_path):
    tp = tmp_path / "t.jsonl"
    _write_transcript(tp, ["looks wrong, revert it"])
    status, detail = vel.transcript_confirms("yes ship it", str(tp))
    assert status is False
    assert "not present" in detail


def test_tool_result_block_does_not_count(tmp_path):
    # the planted tool_result block literally contains "yes ship it" but is not
    # user-authored prose, so it must not satisfy the check.
    tp = tmp_path / "t.jsonl"
    _write_transcript(tp, ["hello"])
    status, _ = vel.transcript_confirms("yes ship it", str(tp))
    assert status is False


def test_missing_transcript_degrades_to_none(tmp_path):
    status, _ = vel.transcript_confirms("yes ship it", str(tmp_path / "nope.jsonl"))
    assert status is None


def test_no_path_degrades_to_none():
    status, _ = vel.transcript_confirms("yes ship it", None)
    assert status is None


# --- hook integration ---------------------------------------------------------

def _hook(monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))
    rc = vel.cmd_hook()
    return rc, capsys.readouterr().err


def test_hook_allows_when_quote_in_transcript(tmp_path, monkeypatch, capsys):
    tp = tmp_path / "t.jsonl"
    _write_transcript(tp, ["yes ship it"])
    leaf = tmp_path / "experience" / "x.md"
    rc, _ = _hook(monkeypatch, capsys, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(leaf), "content": LEAF},
        "transcript_path": str(tp),
    })
    assert rc == 0


def test_hook_blocks_when_quote_fabricated(tmp_path, monkeypatch, capsys):
    tp = tmp_path / "t.jsonl"
    _write_transcript(tp, ["this is wrong"])
    leaf = tmp_path / "experience" / "x.md"
    rc, err = _hook(monkeypatch, capsys, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(leaf), "content": LEAF},
        "transcript_path": str(tp),
    })
    assert rc == 2
    assert "BLOCK" in err


def test_hook_advisory_when_no_transcript(tmp_path, monkeypatch, capsys):
    leaf = tmp_path / "experience" / "x.md"
    rc, err = _hook(monkeypatch, capsys, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(leaf), "content": LEAF},
        # no transcript_path
    })
    assert rc == 0
    assert "ADVISORY" in err


def test_hook_missing_frontmatter_still_blocks_before_transcript(tmp_path, monkeypatch, capsys):
    # the existing presence check fires first, regardless of transcript.
    leaf = tmp_path / "experience" / "x.md"
    rc, err = _hook(monkeypatch, capsys, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(leaf), "content": "no frontmatter here"},
        "transcript_path": "/does/not/matter.jsonl",
    })
    assert rc == 2
    assert "frontmatter" in err.lower()


def test_hook_ignores_non_experience_writes(tmp_path, monkeypatch, capsys):
    rc, _ = _hook(monkeypatch, capsys, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "notes.md"), "content": "x"},
        "transcript_path": "/whatever.jsonl",
    })
    assert rc == 0
