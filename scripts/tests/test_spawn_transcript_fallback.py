"""Regression test for the slow-child transcript-discovery race in
spawn-specialist.py: when _discover_transcript_path times out (the child's
transcript genuinely exists on disk but wasn't created within the 10s poll
window), the finally block must still recover it by child_session_id before
building the COST_LOG row — otherwise plan-review-topological.py's
select_cost_row/pair_telemetry can never resolve that pair's transcript.

Test 1 drives an ordinary (non-topo) spawn and checks the recovered
transcript_path lands in the COST_LOG row. Test 2 drives a real
--review-topo spawn against the two-stage fixture plan and checks the
recovered path resolves through select_cost_row's WINDOW-MATCH branch (not
exact-match, since the real slow-child flow's stderr still reads the
<not-found-within-10s> sentinel) and through pair_telemetry.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SPAWN_SCRIPT = SCRIPTS_DIR / "spawn-specialist.py"
REVIEW_SCRIPT = SCRIPTS_DIR / "plan-review-topological.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SPAWN_MOD = _load("spawn_specialist_transcript_fallback", SPAWN_SCRIPT)
REVIEW_MOD = _load("plan_review_topological_transcript_fallback", REVIEW_SCRIPT)

CHILD_SESSION_ID = "fake-child-session-id-0001"
SENTINEL = "<not-found-within-10s>"


class _FakeProc:
    def __init__(self):
        self.returncode = 0
        self.pid = 999999

    def communicate(self, input=None):  # noqa: A002 - matches subprocess.Popen signature
        payload = {
            "session_id": CHILD_SESSION_ID,
            "result": "COMPLETED: ok",
            "cost_usd": 0.0,
        }
        return json.dumps(payload), ""


@pytest.fixture()
def cost_log(tmp_path, monkeypatch):
    path = tmp_path / "spawn-costs.jsonl"
    monkeypatch.setattr(SPAWN_MOD, "COST_LOG", path)
    return path


@pytest.fixture()
def workdir(tmp_path):
    d = tmp_path / "workdir"
    d.mkdir()
    return d


def _plan_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stub_slow_child(monkeypatch, workdir: Path, transcripts_root: Path) -> Path:
    """Stub the child-launch machinery so main() drives a spawn whose
    transcript genuinely exists on disk under transcripts_root but was not
    discovered within the (stubbed-out) 10s poll -- the exact slow-child
    race this patch fixes. Returns the pre-created transcript file path."""
    monkeypatch.setattr(
        SPAWN_MOD.proc_tree, "launch_supervised", lambda *a, **k: _FakeProc()
    )
    monkeypatch.setattr(SPAWN_MOD.proc_tree, "install_teardown", lambda *a, **k: None)
    monkeypatch.setattr(SPAWN_MOD.proc_tree, "kill_tree", lambda *a, **k: None)
    monkeypatch.setattr(SPAWN_MOD, "permissions_digest", lambda *a, **k: "")
    monkeypatch.setattr(SPAWN_MOD, "deregister_child_scope", lambda *a, **k: None)
    monkeypatch.setattr(shutil, "which", lambda *a, **k: "/usr/bin/claude")
    fake_sysprompt = workdir.parent / "fake-system-prompt.txt"
    fake_sysprompt.write_text("fake system prompt\n")
    monkeypatch.setattr(
        SPAWN_MOD, "composed_system_prompt_file", lambda *a, **k: fake_sysprompt
    )
    # The 10s poll genuinely times out -- this IS the race being tested.
    monkeypatch.setattr(SPAWN_MOD, "_snapshot_transcripts", lambda *a, **k: set())
    monkeypatch.setattr(SPAWN_MOD, "_discover_transcript_path", lambda *a, **k: None)

    # But the file genuinely exists on disk, just not where/when the poll
    # looked -- projects_roots() is where _iter_workdir_transcripts searches.
    monkeypatch.setattr(SPAWN_MOD, "projects_roots", lambda: [transcripts_root])
    project_dir = transcripts_root / SPAWN_MOD._project_dir_name(str(workdir))
    project_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = project_dir / f"{CHILD_SESSION_ID}.jsonl"
    transcript_file.write_text("")
    return transcript_file


def test_finally_block_recovers_transcript_by_child_session_id_stem(
    monkeypatch, tmp_path, workdir, cost_log
):
    transcripts_root = tmp_path / "projects-root"
    transcript_file = _stub_slow_child(monkeypatch, workdir, transcripts_root)

    argv = [
        "--kind", "developer",
        "--plan", str(SCRIPTS_DIR / "tests" / "fixtures" / "plan_two_stage.toml"),
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
        "--complexity", "medium",
        "--effort", "medium",
        "--workdir", str(workdir),
    ]
    rc = SPAWN_MOD.main(argv)
    assert rc == 0

    rows = [json.loads(line) for line in cost_log.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["child_session_id"] == CHILD_SESSION_ID
    assert rows[0]["transcript_path"] == str(transcript_file)


def test_recovered_transcript_resolves_through_select_cost_row_window_match_and_pair_telemetry(
    monkeypatch, tmp_path, workdir, cost_log
):
    transcripts_root = tmp_path / "projects-root"
    transcript_file = _stub_slow_child(monkeypatch, workdir, transcripts_root)

    two_stage_plan = SCRIPTS_DIR / "tests" / "fixtures" / "plan_two_stage.toml"
    sha = _plan_sha(two_stage_plan)

    topo_units_dir = tmp_path / "topo-units"
    monkeypatch.setenv("AGENTCTL_TOPO_UNITS_DIR", str(topo_units_dir))

    argv = [
        "--kind", "thinker",
        "--plan", str(two_stage_plan),
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
        "--complexity", "medium",
        "--effort", "medium",
        "--workdir", str(workdir),
        "--review-topo", "plan-2",
    ]
    rc = SPAWN_MOD.main(argv)
    assert rc == 0

    rows = [json.loads(line) for line in cost_log.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["review_pair"] == "plan-2"
    assert row["plan_sha256"] == sha
    assert row["transcript_path"] == str(transcript_file)

    monkeypatch.setattr(REVIEW_MOD, "COST_LOG", cost_log)
    resolved = REVIEW_MOD.select_cost_row("plan-2", sha, SENTINEL, 0)
    assert resolved is not None
    assert resolved["transcript_path"] == str(transcript_file)

    launched = REVIEW_MOD.Launched(
        rc=0,
        stdout=json.dumps({"result": "REVIEW:\n\n**REVIEW: pass**\n"}),
        stderr=f"spawn-specialist: transcript={SENTINEL}\n"
        "spawn-specialist: kind=thinker budget=medium depth=1 duration_ms=1 "
        "cost_usd=0.0 marker=REVIEW\n",
        log_offset=0,
        plan_sha=sha,
    )
    cost, duration, pulls, transcript = REVIEW_MOD.pair_telemetry(launched, "plan-2")
    assert transcript == str(transcript_file)
