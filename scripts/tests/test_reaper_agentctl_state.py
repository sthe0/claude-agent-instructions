"""Tests for the `agentctl-state` reaper and the session-ownership query it relies on.

Hermetic: HOME and the config root are redirected under tmp_path, so the state directory
the reaper resolves is a throw-away one; no test reads or writes the real
~/.claude-agent/agentctl/state. The runner is driven through execute_pass with only this
reaper, never through main(), which would also discover the git-worktrees reaper.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper import registry, runner  # noqa: E402
from reaper.builtin import agentctl_state as st  # noqa: E402
from reaper.contract import KEEP, REMOVE, ReapContext, session_owned  # noqa: E402
from session_scope.registry import ScopeRecord  # noqa: E402

NOW = 2_000_000_000.0
DAY = 86400.0
OLD = NOW - 30 * DAY
DEAD_PID = 2**22 + 12345

REMOVABLE = "old-unowned-classified.json"
EXPECTED_KEEPS = {
    "fresh-classified.json": "fresh",
    "old-resolved.json": "node RESOLVED",
    "old-routed.json": "node ROUTED",
    "unreadable.json": "unreadable",
    "owned-by-heartbeat.json": "owned",
    "owned-by-pid.json": "owned",
}
SUFFIX_DECOYS = (
    "old-unowned-classified.delivery.json",
    "old-unowned-classified.bak-1.json",
    "old-unowned-classified.SUPERSEDED.json",
    "plan-approved-0123abcd.toml",
)


def _write(directory: Path, name: str, body: str, mtime: float) -> None:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _state(node: str) -> str:
    return json.dumps({"session_id": "x", "node": node})


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root / "config"))
    directory = st.state_dir()
    assert directory == root / "config" / "agentctl" / "state"
    directory.mkdir(parents=True)
    _write(directory, REMOVABLE, _state("CLASSIFIED"), OLD)
    _write(directory, "fresh-classified.json", _state("CLASSIFIED"), NOW - 2 * DAY)
    _write(directory, "old-resolved.json", _state("RESOLVED"), OLD)
    _write(directory, "old-routed.json", _state("ROUTED"), OLD)
    _write(directory, "unreadable.json", "{not json", OLD)
    _write(directory, "owned-by-heartbeat.json", _state("CLASSIFIED"), OLD)
    _write(directory, "owned-by-pid.json", _state("CLASSIFIED"), OLD)
    for name in SUFFIX_DECOYS:
        _write(directory, name, _state("CLASSIFIED"), OLD)
    return directory


def owners() -> "list[ScopeRecord]":
    return [
        ScopeRecord(session_id="owned-by-heartbeat", heartbeat_ts=NOW - 3600.0, pid=DEAD_PID),
        ScopeRecord(session_id="owned-by-pid", heartbeat_ts=NOW - 10 * DAY, pid=os.getpid()),
        ScopeRecord(session_id=REMOVABLE[: -len(".json")], heartbeat_ts=NOW - 10 * DAY, pid=DEAD_PID),
    ]


def make_ctx(directory: Path, *, dry_run: bool = False) -> ReapContext:
    return ReapContext(
        now=NOW, dry_run=dry_run, project_dir=directory, deadletter_dir=directory / "deadletter",
        scope_records=owners(),
    )


def run_pass(directory: Path, *, dry_run: bool):
    reaper = registry.Reaper(st.NAME, "builtin", st.__file__, st)
    out, err = io.StringIO(), io.StringIO()
    log = directory.parent / "removed.jsonl"
    runner.execute_pass(
        [reaper], make_ctx(directory, dry_run=dry_run), dry_run=dry_run, force_run=not dry_run, only=None,
        stamps=directory.parent / "stamps", log_path=log, out=out, err=err,
    )
    return out.getvalue(), err.getvalue(), log


def test_scan_proposes_only_the_old_unowned_classified_plain_file(state_dir):
    verdicts = {Path(v.path).name: v for v in st.scan(make_ctx(state_dir))}
    assert {n for n, v in verdicts.items() if v.action == REMOVE} == {REMOVABLE}
    kept = {n: v.reason for n, v in verdicts.items() if v.action == KEEP}
    assert {n: r.split(" (")[0] for n, r in kept.items()} == EXPECTED_KEEPS


def test_scan_never_looks_at_a_name_outside_the_plain_session_pattern(state_dir):
    seen = {Path(v.path).name for v in st.scan(make_ctx(state_dir))}
    assert seen.isdisjoint(SUFFIX_DECOYS)


def test_fresh_classified_file_is_kept_below_the_age_floor(state_dir):
    _write(state_dir, "just-under-floor.json", _state("CLASSIFIED"), NOW - (st.MIN_AGE_DAYS - 0.5) * DAY)
    verdict = st.judge(state_dir / "just-under-floor.json", make_ctx(state_dir))
    assert verdict.action == KEEP and verdict.reason.startswith("fresh")


def test_pass_removes_exactly_the_residue_file_and_logs_it(state_dir):
    before = set(os.listdir(state_dir))
    _, err, log = run_pass(state_dir, dry_run=False)
    assert before - set(os.listdir(state_dir)) == {REMOVABLE}
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(r["reaper"], Path(r["path"]).name) for r in records] == [(st.NAME, REMOVABLE)]
    assert f"removed {state_dir / REMOVABLE}" in err


def test_dry_run_removes_nothing_and_prints_the_removal(state_dir):
    before = sorted(os.listdir(state_dir))
    out, _, log = run_pass(state_dir, dry_run=True)
    assert sorted(os.listdir(state_dir)) == before
    assert not log.exists()
    assert f"{st.NAME} REMOVE {state_dir / REMOVABLE} " in out
    assert sum(" REMOVE " in line for line in out.splitlines()) == 1


def test_missing_state_directory_yields_no_verdicts(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    assert st.scan(make_ctx(tmp_path)) == []


def test_remove_rejudges_and_keeps_a_session_that_moved_on_after_the_scan(state_dir):
    ctx = make_ctx(state_dir)
    target = state_dir / REMOVABLE
    assert st.judge(target, ctx).action == REMOVE
    _write(state_dir, REMOVABLE, _state("ROUTED"), OLD)
    assert st.remove(str(target), ctx) is False
    assert target.exists()


def test_remove_refuses_a_name_outside_the_plain_session_pattern(state_dir):
    target = state_dir / SUFFIX_DECOYS[0]
    assert st.remove(str(target), make_ctx(state_dir)) is False
    assert target.exists()


def test_session_owned_by_live_pid_or_fresh_heartbeat_and_by_sanitized_id():
    records = owners()
    assert session_owned("owned-by-heartbeat", records, NOW)
    assert session_owned("owned-by-pid", records, NOW)
    assert session_owned("owned-by-pid!", records, NOW)
    assert not session_owned(REMOVABLE[: -len(".json")], records, NOW)
    assert not session_owned("nobody", records, NOW)


def test_session_ownership_ends_when_the_heartbeat_passes_the_ttl():
    records = [ScopeRecord(session_id="s", heartbeat_ts=NOW - 25 * 3600.0, pid=DEAD_PID)]
    assert not session_owned("s", records, NOW)
    assert session_owned("s", records, NOW, heartbeat_ttl_hours=26.0)


def test_production_discovery_finds_agentctl_state_as_builtin(state_dir, tmp_path):
    found = registry.discover(tmp_path)
    assert ("agentctl-state", "builtin") in [(r.name, r.layer) for r in found]
