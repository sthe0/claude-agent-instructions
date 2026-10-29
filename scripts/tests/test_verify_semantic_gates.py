"""Tests for scripts/verify-semantic-gates.py.

The gate computes from the live tree: a hard-sink site (deny / block / exit 2)
with no judge_* call in its scope or its file and no allowlist entry fails the
run. All fixtures live under tmp_path; no stored file is read or written.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load_module(name: str, filename: str):
    path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


inv = _load_module("crutch_inventory_vsg_test", "crutch-inventory.py")
vsg = _load_module("verify_semantic_gates_test", "verify-semantic-gates.py")


_HARD_SINK_FIXTURE = '''
import re


def decide(text):
    if re.search(r"correction|feedback", text):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "matched",
            }
        }
    return None
'''

_HARD_SINK_WITH_SAME_SCOPE_JUDGE_FIXTURE = '''
import re


def decide(text, runner):
    if not judge_meaning(text, runner):
        return None
    if re.search(r"correction|feedback", text):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "matched",
            }
        }
    return None


def judge_meaning(text, runner):
    return False
'''

_HARD_SINK_WITH_FILE_LEVEL_JUDGE_FIXTURE = '''
import re


def judge_meaning(text, runner):
    return False


def prefilter(text):
    return bool(re.search(r"correction|feedback", text))


def check(text, runner):
    return prefilter(text) and judge_meaning(text, runner)


def decide(text, runner):
    if check(text, runner):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "matched",
            }
        }
    return None
'''


def test_unguarded_hard_sink_is_reported_and_named(tmp_path, capsys):
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_FIXTURE, encoding="utf-8")

    unguarded, _ = vsg.find_unguarded_hard_sinks(inv, tmp_path)

    decide_site = next(s for s in inv.enumerate_code_sites(tmp_path) if s.scope == "decide")
    assert [s.id for s in unguarded] == [decide_site.id]
    assert vsg.run_check(inv, tmp_path) == 1
    out = capsys.readouterr().out
    assert f"SEMANTIC-UNGUARDED {decide_site.id}" in out
    assert "hard_site.py" in out


def test_same_scope_judge_is_not_reported(tmp_path):
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_WITH_SAME_SCOPE_JUDGE_FIXTURE, encoding="utf-8")
    decide_site = next(s for s in inv.enumerate_code_sites(tmp_path) if s.scope == "decide")
    assert decide_site.judge_guarded is True

    assert vsg.find_unguarded_hard_sinks(inv, tmp_path)[0] == []
    assert vsg.run_check(inv, tmp_path) == 0


def test_file_rollup_judge_corroborates_a_scope_without_one(tmp_path):
    """The judge_* call lives in a different scope of the same file: the
    scope-local flag is False but the file-rollup corroborates a real guard."""
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_WITH_FILE_LEVEL_JUDGE_FIXTURE, encoding="utf-8")
    decide_site = next(s for s in inv.enumerate_code_sites(tmp_path) if s.scope == "decide")
    assert decide_site.judge_guarded is False
    assert inv.enumerate_code_file_rollups(tmp_path)[0].judge_guarded is True

    assert vsg.find_unguarded_hard_sinks(inv, tmp_path)[0] == []
    assert vsg.run_check(inv, tmp_path) == 0


def test_allowlisted_id_is_not_reported(tmp_path, monkeypatch):
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_FIXTURE, encoding="utf-8")
    decide_site = next(s for s in inv.enumerate_code_sites(tmp_path) if s.scope == "decide")
    monkeypatch.setattr(vsg, "KNOWN_UNGUARDED_HARD_SINKS", {decide_site.id: "fixture ground"})

    assert vsg.find_unguarded_hard_sinks(inv, tmp_path)[0] == []


def test_clean_tree_with_no_registry_file_exits_zero(tmp_path):
    (tmp_path / "plain.py").write_text("x = 1\n", encoding="utf-8")
    assert not list(tmp_path.glob("*.toml"))
    assert vsg.main(["--root", str(tmp_path), "--staged"]) == 0


def test_poisoned_registry_file_does_not_change_the_verdict(tmp_path):
    """A stored registry beside the tree is never read: garbage in it must not
    flip either a clean or a failing verdict."""
    clean = tmp_path / "clean"
    bad = tmp_path / "bad"
    clean.mkdir()
    bad.mkdir()
    (clean / "hard_site.py").write_text(_HARD_SINK_WITH_SAME_SCOPE_JUDGE_FIXTURE, encoding="utf-8")
    (bad / "hard_site.py").write_text(_HARD_SINK_FIXTURE, encoding="utf-8")
    for root in (clean, bad):
        (root / "crutch_registry.toml").write_text("not [valid toml\n", encoding="utf-8")

    assert vsg.main(["--root", str(clean)]) == 0
    assert vsg.main(["--root", str(bad)]) == 1


def test_fail_output_says_how_to_fix(tmp_path, capsys):
    (tmp_path / "hard_site.py").write_text(_HARD_SINK_FIXTURE, encoding="utf-8")
    assert vsg.run_check(inv, tmp_path) == 1
    assert "KNOWN_UNGUARDED_HARD_SINKS" in capsys.readouterr().out

    (tmp_path / "hard_site.py").write_text(_HARD_SINK_WITH_SAME_SCOPE_JUDGE_FIXTURE, encoding="utf-8")
    assert vsg.run_check(inv, tmp_path) == 0
    assert "KNOWN_UNGUARDED_HARD_SINKS" not in capsys.readouterr().out
