"""The context-growth nudge, after it moved onto the shared band throttle.

Written when ``lib/band_throttle.py`` was extracted from this hook and the
burn-rate guard. The extraction is only safe if this hook's observable behaviour
did not move with it, and the part most easily moved by accident is where the
stamp lives: these tests pin the literal ``/tmp/cc-context-nudge-<id>`` path the
hook has always written, alongside the once-per-band contract that path serves.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "hook_context_growth_reminder", SCRIPTS_DIR / "hook-context-growth-reminder.py"
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _transcript(tmp_path, context_tokens):
    path = tmp_path / "transcript.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for tokens in context_tokens:
            fh.write(json.dumps({
                "message": {"usage": {
                    "input_tokens": tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }},
            }) + "\n")
    return path


def _run(monkeypatch, capsys, transcript, session_id, state_root):
    monkeypatch.setattr(mod, "STATE_ROOT", str(state_root))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "transcript_path": str(transcript),
        "session_id": session_id,
    })))
    rc = mod.main()
    return rc, capsys.readouterr().out


def test_a_crossed_band_nudges_once_and_then_stays_quiet(
        tmp_path, monkeypatch, capsys):
    transcript = _transcript(tmp_path, [130_000])
    session = str(uuid.uuid4())
    state = tmp_path / "state"

    rc, first = _run(monkeypatch, capsys, transcript, session, state)
    assert rc == 0
    assert "context-growth" in first

    rc, second = _run(monkeypatch, capsys, transcript, session, state)
    assert rc == 0
    assert second == ""


def test_a_higher_band_speaks_again_in_the_same_session(
        tmp_path, monkeypatch, capsys):
    session = str(uuid.uuid4())
    state = tmp_path / "state"

    _run(monkeypatch, capsys, _transcript(tmp_path, [130_000]), session, state)
    _, out = _run(
        monkeypatch, capsys, _transcript(tmp_path, [300_000]), session, state)
    assert "very large" in out


def test_the_stamp_keeps_its_historical_tmp_path(tmp_path, monkeypatch, capsys):
    """The refactor must not relocate a path other sessions already own."""
    assert mod.STATE_ROOT == "/tmp"
    assert mod.STATE_PREFIX == "cc-context-nudge-"

    session = str(uuid.uuid4())
    state = tmp_path / "state"
    _run(monkeypatch, capsys, _transcript(tmp_path, [130_000]), session, state)
    assert (state / f"cc-context-nudge-{session}").read_text().strip() == "1"


def test_a_short_session_says_nothing(tmp_path, monkeypatch, capsys):
    _, out = _run(
        monkeypatch, capsys, _transcript(tmp_path, [4_000]),
        str(uuid.uuid4()), tmp_path / "state")
    assert out == ""


def test_an_unwritable_state_root_still_nudges(tmp_path, monkeypatch, capsys):
    """Fail-open: losing the stamp costs a repeated nudge, never an error."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    rc, out = _run(
        monkeypatch, capsys, _transcript(tmp_path, [130_000]),
        str(uuid.uuid4()), blocked)
    assert rc == 0
    assert "context-growth" in out
