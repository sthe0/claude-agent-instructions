"""spawn-specialist.py child-noanswer-recovery (fix A): a child stopped by the account usage limit
is routed to CHILD_INFRA_FAILURE's retry, and a child that answered without a return marker is
resumed once and asked only for the marker. Each action writes one `fix-firing/v1` row.

A fake process stands in for both `claude -p` launches (as in
test_spawn_specialist_child_return.py); the resume is told apart by `--resume` in its command.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from agentctl.dispatch import parse_marker
from lib import marker_extract

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"
CHILD_SID = "11111111-2222-3333-4444-555555555555"
USAGE_LIMIT_TEXT = "You've hit your 5-hour limit · resets 4am (Europe/Moscow)"
FLAG_ENV = "AGENTCTL_CHILD_NOANSWER_RECOVERY"
FIRINGS_LOG_ENV = "AGENTCTL_FIX_FIRINGS_LOG"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    def __init__(self, payload: dict, returncode: int = 0) -> None:
        self.returncode = returncode
        self.pid = 424242
        self._payload = payload

    def communicate(self, input=None, timeout=None):
        return (json.dumps(self._payload), "")


def _firings(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run(monkeypatch, tmp_path, capsys, child_result: str, resume_result: "str | None" = None,
         child_is_error: bool = False):
    mod = _load()
    captured: dict = {"ledger": [], "launches": []}

    def fake_launch(cmd, **kwargs):
        captured["launches"].append(list(cmd))
        if "--resume" in cmd:
            return _FakeProc({"result": resume_result or "", "total_cost_usd": 0.12, "session_id": CHILD_SID})
        return _FakeProc({"result": child_result, "cost_usd": 0.5, "session_id": CHILD_SID,
                          "is_error": child_is_error}, returncode=1 if child_is_error else 0)

    monkeypatch.setattr(mod.proc_tree, "launch_supervised", fake_launch)
    monkeypatch.setattr(mod.proc_tree, "install_teardown", lambda p: None)
    monkeypatch.setattr(mod.proc_tree, "kill_tree", lambda p: None)
    monkeypatch.setattr(mod, "_snapshot_transcripts", lambda *a, **k: set())
    monkeypatch.setattr(mod, "_discover_transcript_path", lambda *a, **k: None)
    monkeypatch.setattr(mod, "permissions_digest", lambda *a, **k: "")
    monkeypatch.setattr(mod, "log_cost_entry", captured["ledger"].append)
    monkeypatch.setattr(mod, "deregister_child_scope", lambda *a, **k: None)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/claude")
    sysprompt = tmp_path / "sysprompt.md"
    sysprompt.write_text("system prompt", encoding="utf-8")
    monkeypatch.setattr(mod, "composed_system_prompt_file", lambda skill: sysprompt)
    monkeypatch.setenv(marker_extract.ENV_KILL_SWITCH, "0")
    firings_log = tmp_path / "fix-firings.jsonl"
    firings_log.unlink(missing_ok=True)
    monkeypatch.setenv(FIRINGS_LOG_ENV, str(firings_log))

    plan = tmp_path / "plan.md"
    plan.write_text("a plan", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "spawn-specialist.py", "--kind", "developer", "--plan", str(plan),
        "--done-criterion", "done", "--criterion-type", "measurable",
        "--model", "haiku", "--effort", "medium",
    ])
    mod.main()
    out = capsys.readouterr()
    captured["stdout"], captured["stderr"] = out.out, out.err
    captured["row"] = next(e for e in captured["ledger"] if e.get("event") == "spawn")
    captured["resumed"] = any("--resume" in c for c in captured["launches"])
    captured["firings"] = _firings(firings_log)
    return captured


def test_usage_limit_routes_to_infra_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(FLAG_ENV, "1")
    captured = _run(monkeypatch, tmp_path, capsys, USAGE_LIMIT_TEXT, child_is_error=True)

    routed, _ = parse_marker(captured["stdout"])
    assert routed == "CHILD_INFRA_FAILURE"
    assert not captured["resumed"]
    assert [(f["fix"], f["event"]) for f in captured["firings"]] == [("A", "usage_limit")]
    assert captured["firings"][0]["observables"]["matched"] == "hit your 5-hour limit"
    assert captured["firings"][0]["schema"] == "fix-firing/v1"


def test_resume_recovers_marker(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(FLAG_ENV, "1")
    captured = _run(monkeypatch, tmp_path, capsys, "I finished the work.", resume_result="COMPLETED: done.\n")

    routed, _ = parse_marker(captured["stdout"])
    assert routed == "COMPLETED"
    assert captured["row"]["return_marker_source"] == "resume"
    assert captured["row"]["resume"]["marker"] == "COMPLETED"
    resume_cmd = next(c for c in captured["launches"] if "--resume" in c)
    assert resume_cmd[resume_cmd.index("--resume") + 1] == CHILD_SID
    assert len(captured["firings"]) == 1
    firing = captured["firings"][0]
    assert (firing["fix"], firing["event"], firing["cost_usd"]) == ("A", "resume", 0.12)
    assert firing["observables"]["resume_marker"] == "COMPLETED"


def test_resume_without_marker_stays_malformed(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(FLAG_ENV, "1")
    captured = _run(monkeypatch, tmp_path, capsys, "I finished the work.", resume_result="Still no marker.")

    routed, _ = parse_marker(captured["stdout"])
    assert routed == "MALFORMED"
    assert captured["row"]["return_marker_source"] == "envelope"
    assert len(captured["firings"]) == 1
    assert captured["firings"][0]["observables"]["resume_marker"] is None


def test_marker_present_no_resume(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(FLAG_ENV, "1")
    captured = _run(monkeypatch, tmp_path, capsys, "COMPLETED: done.\n")

    assert not captured["resumed"]
    assert captured["firings"] == []
    assert "resume" not in captured["row"]


def test_flag_off_keeps_base_path(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(FLAG_ENV, "0")
    markerless = _run(monkeypatch, tmp_path, capsys, "I finished the work.", resume_result="COMPLETED: done.\n")
    limited = _run(monkeypatch, tmp_path, capsys, USAGE_LIMIT_TEXT, child_is_error=True)

    assert not markerless["resumed"] and not limited["resumed"]
    assert parse_marker(markerless["stdout"])[0] == "MALFORMED"
    assert parse_marker(limited["stdout"])[0] == "MALFORMED"
    assert markerless["firings"] == [] and limited["firings"] == []
    assert markerless["stderr"].rstrip().endswith("MALFORMED")


def test_config_ships_recovery_on():
    from agentctl.config import parse_config_md

    assert parse_config_md().get("child-noanswer-recovery") == "on"
