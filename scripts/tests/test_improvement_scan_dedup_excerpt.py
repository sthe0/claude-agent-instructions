"""The dedup judge is shown the candidate leaf's Difficulty section, not only its
one-line description (the text the search scored on)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan_excerpt", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()
SENTENCE = "distinctive-difficulty-sentence zebra quartz"


class _Capture:
    def __init__(self):
        self.stdin = []

    def __call__(self, argv, **kwargs):
        from agentctl.dispatch import RunResult

        self.stdin.append(kwargs.get("stdin", ""))
        return RunResult(0, stdout="NO", stderr="")


def _run(monkeypatch, leaf_path):
    monkeypatch.delenv("AGENTCTL_ADVISOR", raising=False)
    out = (
        "analogous experience leafs:\n  [ 90] leaf.md\n        short desc\n"
        f"        extend --leaf {leaf_path}"
    )
    monkeypatch.setattr(
        scan.shell, "search_experience", lambda kw, scope="global": (True, True, out)
    )
    cap = _Capture()
    scan.build_findings_from_grounds(
        [{"detector": "d", "functional_ground": "some ground", "title": "t"}],
        judge_runner=cap,
    )
    return cap.stdin


def _leaf(tmp_path, body):
    p = tmp_path / "leaf.md"
    p.write_text(
        f"---\ndescription: short desc\n---\n# T\n\n## Difficulty\n{body}\n\n## Contexts\nx\n"
    )
    return p


def test_judge_prompt_carries_difficulty_section(tmp_path, monkeypatch):
    stdin = _run(monkeypatch, _leaf(tmp_path, SENTENCE))
    assert len(stdin) == 1 and SENTENCE in stdin[0]


def test_unreadable_leaf_falls_back_to_description(tmp_path, monkeypatch):
    stdin = _run(monkeypatch, tmp_path / "missing.md")
    assert len(stdin) == 1 and "short desc" in stdin[0]


def test_excerpt_is_capped(tmp_path, monkeypatch):
    stdin = _run(monkeypatch, _leaf(tmp_path, "x" * 5000))
    assert len(stdin) == 1 and "x" * 2001 not in stdin[0]
