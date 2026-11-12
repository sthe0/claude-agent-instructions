"""Tests for the read-only holder query: ``liveness.path_holders`` and ``hook-reaper.py --holders``.

Hermetic: scope records, engine states and the process sweep are planted or injected under
tmp_path; the one test that uses the real /proc starts its own child process.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import config_root  # noqa: E402
from reaper import liveness, runner  # noqa: E402
from reaper.contract import ReapContext  # noqa: E402
from session_scope.registry import ScopeRecord  # noqa: E402

NOW = 2_000_000_000.0
DAY = 24 * 3600.0


def make_ctx(tmp: Path, records=(), error=None) -> ReapContext:
    return ReapContext(
        now=NOW, dry_run=False, project_dir=tmp, deadletter_dir=tmp / "deadletter",
        scope_records=list(records), scope_registry_error=error,
    )


def write_state(state_dir: Path, session: str, node: str, **fields) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{session}.json"
    path.write_text(json.dumps({"node": node, "task_id": fields.pop("task_id", "t-" + session), **fields}), encoding="utf-8")
    return path


@pytest.fixture
def planted(tmp_path):
    work = tmp_path / "work"
    (work / "inner").mkdir(parents=True)
    sibling = tmp_path / "work-old"
    sibling.mkdir()
    return work, sibling


def ask(tmp_path, paths, *, records=(), error=None, sweep=None, state_dir=None):
    return liveness.path_holders(
        [str(p) for p in paths], make_ctx(tmp_path, records, error),
        sweep=liveness.HolderSweep() if sweep is None else sweep, state_dir=state_dir or tmp_path / "no-engine-store",
    )


# ── each source alone ────────────────────────────────────────────────────

def test_nothing_holding_a_path_answers_free(tmp_path, planted):
    work, _ = planted
    answer = ask(tmp_path, [work])[str(work)]
    assert answer.status == liveness.FREE_STATUS and answer.report() == ""


def test_a_live_scope_record_inside_the_path_holds_it_and_a_sibling_stays_free(tmp_path, planted):
    work, sibling = planted
    record = ScopeRecord(session_id="s1", heartbeat_ts=NOW, cwd=str(work / "inner"))
    answers = ask(tmp_path, [work, sibling], records=[record])
    assert answers[str(work)].status == liveness.HELD_STATUS
    assert answers[str(work)].kind == liveness.KIND_SCOPE and "s1" in answers[str(work)].detail
    assert answers[str(sibling)].status == liveness.FREE_STATUS


def test_a_scope_record_with_a_repo_root_at_the_path_holds_it(tmp_path, planted):
    work, _ = planted
    record = ScopeRecord(session_id="s1", heartbeat_ts=NOW, repo_root=str(work))
    assert ask(tmp_path, [work], records=[record])[str(work)].kind == liveness.KIND_SCOPE


def test_a_scope_record_of_a_dead_stale_session_holds_nothing(tmp_path, planted):
    work, _ = planted
    record = ScopeRecord(session_id="s1", heartbeat_ts=NOW - 2 * DAY, cwd=str(work), pid=None)
    assert ask(tmp_path, [work], records=[record])[str(work)].status == liveness.FREE_STATUS


@pytest.mark.parametrize("field", ["repo_root", "delivery_worktree"])
def test_an_unresolved_engine_task_holds_the_path_and_names_session_and_task(tmp_path, planted, field):
    work, sibling = planted
    store = tmp_path / "state"
    write_state(store, "sess-a", "EXECUTING", task_id="the-task", **{field: str(work / "inner")})
    answers = ask(tmp_path, [work, sibling], state_dir=store)
    held = answers[str(work)]
    assert held.status == liveness.HELD_STATUS and held.kind == liveness.KIND_ENGINE
    assert "sess-a" in held.detail and "the-task" in held.detail
    assert answers[str(sibling)].status == liveness.FREE_STATUS


def test_a_resolved_engine_task_holds_nothing(tmp_path, planted):
    work, _ = planted
    store = tmp_path / "state"
    write_state(store, "sess-a", "RESOLVED", delivery_worktree=str(work))
    assert ask(tmp_path, [work], state_dir=store)[str(work)].status == liveness.FREE_STATUS


def test_a_process_with_its_cwd_inside_the_path_holds_it(tmp_path, planted):
    work, sibling = planted
    child = subprocess.Popen(["sleep", "60"], cwd=work / "inner")
    try:
        answers = liveness.path_holders(
            [str(work), str(sibling)], make_ctx(tmp_path), state_dir=tmp_path / "no-engine-store",
        )
    finally:
        child.kill()
        child.wait()
    assert answers[str(work)].status == liveness.HELD_STATUS
    assert answers[str(work)].kind == liveness.KIND_PROCESS and "cwd" in answers[str(work)].detail
    assert answers[str(sibling)].status != liveness.HELD_STATUS


def test_a_held_path_is_held_even_when_the_sweep_was_incomplete(tmp_path, planted):
    work, _ = planted
    sweep = liveness.HolderSweep(incomplete="pid=7 unreadable")
    sweep.held["cwd"] = [str(work / "inner")]
    assert ask(tmp_path, [work], sweep=sweep)[str(work)].status == liveness.HELD_STATUS


# ── a source that could not be read is never free ────────────────────────

def test_an_incomplete_process_sweep_gives_unknown(tmp_path, planted):
    work, _ = planted
    answer = ask(tmp_path, [work], sweep=liveness.HolderSweep(incomplete="pid=7 name='x' errno=13"))[str(work)]
    assert answer.status == liveness.UNKNOWN_STATUS and "process sweep" in answer.report() and "pid=7" in answer.report()


def test_an_unreadable_scope_registry_gives_unknown(tmp_path, planted):
    work, _ = planted
    answer = ask(tmp_path, [work], error="ValueError: corrupt record")[str(work)]
    assert answer.status == liveness.UNKNOWN_STATUS and "scope registry" in answer.report()


def test_an_engine_store_that_is_not_a_directory_gives_unknown(tmp_path, planted):
    work, _ = planted
    store = tmp_path / "state"
    store.write_text("not a directory", encoding="utf-8")
    answer = ask(tmp_path, [work], state_dir=store)[str(work)]
    assert answer.status == liveness.UNKNOWN_STATUS and "engine store" in answer.report()


@pytest.mark.parametrize("body", ["{not json", "[]", '{"task_id": "t"}'])
def test_an_engine_state_that_cannot_be_read_as_a_state_gives_unknown(tmp_path, planted, body):
    work, _ = planted
    store = tmp_path / "state"
    store.mkdir()
    (store / "bad.json").write_text(body, encoding="utf-8")
    answer = ask(tmp_path, [work], state_dir=store)[str(work)]
    assert answer.status == liveness.UNKNOWN_STATUS and "engine store" in answer.report()


def test_a_zero_byte_engine_state_records_nothing_and_does_not_make_the_answer_unknown(tmp_path, planted):
    work, _ = planted
    store = tmp_path / "state"
    store.mkdir()
    (store / "empty.json").write_bytes(b"")
    assert ask(tmp_path, [work], state_dir=store)[str(work)].status == liveness.FREE_STATUS


def test_a_zero_byte_engine_state_beside_an_unresolved_task_still_leaves_the_path_held(tmp_path, planted):
    work, _ = planted
    store = tmp_path / "state"
    write_state(store, "sess-a", "EXECUTING", delivery_worktree=str(work))
    (store / "empty.json").write_bytes(b"")
    assert ask(tmp_path, [work], state_dir=store)[str(work)].status == liveness.HELD_STATUS


def test_a_non_empty_unparseable_engine_state_beside_a_zero_byte_one_still_gives_unknown(tmp_path, planted):
    work, _ = planted
    store = tmp_path / "state"
    store.mkdir()
    (store / "empty.json").write_bytes(b"")
    (store / "bad.json").write_text("{", encoding="utf-8")
    answer = ask(tmp_path, [work], state_dir=store)[str(work)]
    assert answer.status == liveness.UNKNOWN_STATUS and "bad.json" in answer.report()


def test_a_missing_engine_store_holds_nothing(tmp_path, planted):
    work, _ = planted
    assert ask(tmp_path, [work], state_dir=tmp_path / "never-created")[str(work)].status == liveness.FREE_STATUS


def test_the_filesystem_root_is_never_answered_free(tmp_path):
    assert ask(tmp_path, ["/"])["/"].status == liveness.UNKNOWN_STATUS


# ── the queried path is only compared as a string ────────────────────────

def test_nothing_at_or_under_a_queried_path_is_stat_ed_resolved_or_walked(tmp_path, planted, monkeypatch):
    work, sibling = planted
    queried = [str(work), str(sibling)]
    seen = []

    def watch(module, name):
        real = getattr(module, name)

        def wrapper(*args, **kwargs):
            first = args[0] if args else kwargs.get("path", kwargs.get("top"))
            try:
                text = os.fsdecode(os.fspath(first))
            except TypeError:
                text = ""
            if any(text == q or text.startswith(q + os.sep) for q in queried):
                seen.append((name, text))
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    for name in ("walk", "scandir", "stat", "lstat", "listdir", "readlink"):
        watch(os, name)
    for name in ("realpath", "exists", "lexists", "isdir", "islink"):
        watch(os.path, name)

    store = tmp_path / "state"
    write_state(store, "sess-a", "EXECUTING", delivery_worktree=str(work))
    record = ScopeRecord(session_id="s1", heartbeat_ts=NOW, cwd=str(work))
    ask(tmp_path, queried, records=[record], state_dir=store)
    ask(tmp_path, queried, state_dir=store)
    assert seen == []


# ── the command line ─────────────────────────────────────────────────────

@pytest.fixture
def cli(tmp_path, monkeypatch):
    for sub in ("home", "config", "project"):
        (tmp_path / sub).mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setattr(liveness, "sweep_holders", lambda: liveness.HolderSweep())
    return tmp_path


def holders_cli(capsys, *paths):
    code = runner.main(["--holders", *map(str, paths)], discover=lambda project, warn: pytest.fail("discovery ran"))
    return code, capsys.readouterr().out.splitlines()


def test_holders_prints_one_tab_separated_line_per_path_and_exits_zero_when_all_are_free(cli, planted, capsys):
    work, sibling = planted
    code, lines = holders_cli(capsys, work, sibling)
    assert code == 0
    assert lines == [f"{work}\tfree\t", f"{sibling}\tfree\t"]


def test_holders_exits_three_when_a_path_is_held_and_names_who(cli, planted, capsys):
    work, sibling = planted
    write_state(config_root.agentctl_state_dir(), "sess-a", "EXECUTING", task_id="the-task", delivery_worktree=str(work))
    code, lines = holders_cli(capsys, work, sibling)
    assert code == 3
    assert lines[0].split("\t")[:2] == [str(work), "held"] and "sess-a" in lines[0] and "the-task" in lines[0]
    assert lines[1] == f"{sibling}\tfree\t"


def test_holders_exits_four_when_any_path_is_unknown_even_if_another_is_held(cli, planted, capsys, monkeypatch):
    work, sibling = planted
    write_state(config_root.agentctl_state_dir(), "sess-a", "EXECUTING", delivery_worktree=str(work))
    monkeypatch.setattr(liveness, "sweep_holders", lambda: liveness.HolderSweep(incomplete="pid=7 unreadable"))
    code, lines = holders_cli(capsys, work, sibling)
    assert code == 4
    assert [line.split("\t")[1] for line in lines] == ["held", "unknown"]


@pytest.mark.parametrize("other", [["--dry-run"], ["--force-run"], ["--only", "a"], ["--list"], ["--upkeep-only"], ["--on-pressure"], ["--no-wait"]])
def test_holders_combines_with_no_other_flag(cli, other):
    with pytest.raises(SystemExit) as raised:
        runner.main(["--holders", "/some/path", *other], discover=lambda project, warn: [])
    assert raised.value.code == 2


def test_holders_needs_a_path(cli):
    with pytest.raises(SystemExit) as raised:
        runner.main(["--holders"], discover=lambda project, warn: [])
    assert raised.value.code == 2


def test_the_hook_script_answers_a_nonexistent_path_as_a_line_and_changes_nothing(cli, tmp_path):
    script = Path(__file__).resolve().parent.parent / "hook-reaper.py"
    probe = tmp_path / "does-not-exist"
    done = subprocess.run([sys.executable, str(script), "--holders", str(probe)], capture_output=True, text=True, env=os.environ.copy())
    status = done.stdout.split("\t")[1] if "\t" in done.stdout else None
    assert done.stdout.startswith(f"{probe}\t") and status in ("free", "unknown")
    assert done.returncode == (0 if status == "free" else 4)
    assert not (tmp_path / "home" / ".local" / "state" / "claude-reaper").exists()
    assert not (tmp_path / "config" / "reaper").exists()
