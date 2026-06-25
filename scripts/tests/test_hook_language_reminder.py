"""Stage 5: language-reminder advisory hook — fires on a Cyrillic prompt,
silent on English, tolerant of stray transliteration / ticket keys."""
import importlib.util
import io
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "hook_language_reminder",
    Path(__file__).resolve().parents[1] / "hook-language-reminder.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _run(monkeypatch, capsys, prompt):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": prompt})))
    rc = mod.main()
    return rc, capsys.readouterr().out


# --- detector unit ------------------------------------------------------------

def test_detect_russian_prompt():
    assert mod.is_cyrillic_prompt("Давай починим баг в движке")


def test_english_prompt_not_detected():
    assert not mod.is_cyrillic_prompt("Please fix the bug in the engine")


def test_mostly_english_with_one_cyrillic_word_not_detected():
    # a single transliterated word amid English stays under the ratio
    assert not mod.is_cyrillic_prompt(
        "Run the DEEPAGENT-430 pipeline and report the resulting metrics now да")


def test_ticket_key_alone_not_detected():
    assert not mod.is_cyrillic_prompt("look at ABC-123 and PROJ-9")


# --- hook behaviour -----------------------------------------------------------

def test_fires_and_is_advisory(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, "Сделай ревью этого кода, пожалуйста")
    assert rc == 0
    assert "language-reminder" in out
    assert "Russian" in out


def test_silent_on_english(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, "Review this code please")
    assert rc == 0
    assert out == ""


def test_silent_on_empty(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, "   ")
    assert rc == 0
    assert out == ""
