"""Tests for scripts/claude-recover.py — snapshot, boot-aware select, freshness status.

tmux, /proc, the session registry and the state dir are all fixtures under tmp_path; nothing
touches the real $HOME. The module under test comes from $CLAUDE_RECOVER_PY (the mutation
catalogue points it at a mutated copy) and defaults to the sibling script.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "claude_recover"
MODULE_PATH = Path(os.environ.get("CLAUDE_RECOVER_PY") or HERE.parent / "claude-recover.py")

T0 = 1_760_000_000
SECRET_MARKERS = ("SECRET-TOKEN-123", "SECRET-HDR-VALUE", "SECRET-PROMPT", "SECRET-SETTINGS",
                  "SECRET-NESTED", "SECRET-SYS")


def load_module():
    spec = importlib.util.spec_from_file_location("claude_recover_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class World:
    """A fake machine: /proc tree, tmux, session registry, state dir, HOME."""

    def __init__(self, root: Path):
        self.root = root
        self.proc = root / "proc"
        self.cfg = root / "cfg"
        self.state = root / "state"
        self.tmux = root / "tmux"
        self.procs = json.loads((FIXTURES / "procs.json").read_text())
        self.panes = json.loads((FIXTURES / "panes.json").read_text())
        shutil.copytree(FIXTURES / "sessions", self.cfg / "sessions")
        self.build_proc()
        self.write_tmux()

    def build_proc(self):
        shutil.rmtree(self.proc, ignore_errors=True)
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        (self.proc / "sys/kernel/random/boot_id").write_text("proc-boot-id\n")
        (self.proc / "stat").write_text("cpu 1 2 3\nbtime 1759990000\n")
        (self.proc / "self").mkdir()
        shutil.copy(FIXTURES / "mountinfo", self.proc / "self/mountinfo")
        for pid, spec in self.procs.items():
            pdir = self.proc / pid
            pdir.mkdir()
            (pdir / "stat").write_text(f"{pid} ({spec['comm']}) S {spec['ppid']} {pid} {pid} 0 -1\n")
            (pdir / "cmdline").write_bytes(b"\0".join(a.encode() for a in spec["argv"]) + b"\0")
            (pdir / "environ").write_bytes(
                b"\0".join(f"{k}={v}".encode() for k, v in spec["environ"].items()) + b"\0")

    def write_tmux(self):
        rows = "\n".join("\t".join(p) for p in self.panes)
        Path(str(self.tmux) + ".out").write_text(rows + "\n")
        self.tmux.write_text('#!/bin/sh\ncat "$0.out"\n')
        self.tmux.chmod(0o755)

    def kill(self, pid: int):
        shutil.rmtree(self.proc / str(pid))

    def set_status(self, name: str, status: str):
        path = self.cfg / "sessions" / f"{name}.json"
        data = json.loads(path.read_text())
        data["status"] = status
        path.write_text(json.dumps(data))

    def env(self, boot="boot-1", now=T0, config_dirs=None):
        env = dict(os.environ)
        env.update({
            "TMUX_BIN": str(self.tmux),
            "PROC_ROOT": str(self.proc),
            "CLAUDE_RECOVER_STATE_DIR": str(self.state),
            "CLAUDE_RECOVER_CONFIG_DIRS": str(self.cfg) if config_dirs is None else config_dirs,
            "CLAUDE_RECOVER_NOW": str(now),
            "CLAUDE_RECOVER_BOOT_ID": boot,
            "HOME": "/home/u",
        })
        return env

    def run(self, *args, **env_kw):
        return subprocess.run([sys.executable, str(MODULE_PATH), *args], env=self.env(**env_kw),
                              capture_output=True, text=True, timeout=60)

    def snapshots(self, boot="boot-1"):
        return sorted((self.state / "snapshots" / boot).glob("*.json"))

    def last_snapshot(self, boot="boot-1"):
        return json.loads(self.snapshots(boot)[-1].read_text())


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def session_ids(snapshot):
    return {s["sessionId"] for s in snapshot["sessions"]}


def test_snapshot_keeps_only_top_claude_of_each_pane(world):
    assert world.run("snapshot").returncode == 0
    snap = world.last_snapshot()
    assert session_ids(snap) == {"sess-aaa", "sess-bbb", "sess-ccc"}


def test_snapshot_binds_session_to_its_pane(world):
    world.run("snapshot")
    by_id = {s["sessionId"]: s for s in world.last_snapshot()["sessions"]}
    assert by_id["sess-bbb"]["pane"]["pane_id"] == "%2"
    assert by_id["sess-aaa"]["pane"] == {
        "session_name": "main", "window_id": "@1", "pane_id": "%1", "pane_pid": 100,
        "window_name": "work", "pane_current_path": "/home/u/proj"}
    assert by_id["sess-ccc"]["cwd"] == "/home/u/misc"


def test_snapshot_env_whitelist_keeps_no_secret(world):
    world.run("snapshot")
    text = world.snapshots()[-1].read_text()
    for marker in SECRET_MARKERS:
        assert marker not in text
    by_id = {s["sessionId"]: s for s in json.loads(text)["sessions"]}
    assert by_id["sess-aaa"]["env"] == {
        "CLAUDE_PERSONAL": "1",
        "CLAUDE_CONFIG_DIR": "/home/u/.claude-agent",
        "custom_header_names": ["X-Profile", "Authorization"],
    }


def test_snapshot_argv_drops_prompt_and_unlisted_values(world):
    world.run("snapshot")
    by_id = {s["sessionId"]: s for s in world.last_snapshot()["sessions"]}
    assert by_id["sess-aaa"]["argv"] == [
        "claude", "--dangerously-skip-permissions", "--resume", "sess-aaa", "--model", "opus",
        "--settings"]
    assert by_id["sess-ccc"]["argv"] == ["claude", "--resume=sess-ccc", "--append-system-prompt"]


def test_snapshot_records_status_and_transcript_path(world):
    world.run("snapshot")
    by_id = {s["sessionId"]: s for s in world.last_snapshot()["sessions"]}
    bbb = by_id["sess-bbb"]
    assert (bbb["status"], bbb["waitingFor"]) == ("busy", "approve tool")
    assert bbb["transcript"] == f"{world.cfg}/projects/-home-u-ops/sess-bbb.jsonl"


def test_snapshot_records_fuse_mounts_under_home_only(world):
    world.run("snapshot")
    mounts = world.last_snapshot()["mounts"]
    assert [m["mountpoint"] for m in mounts] == [
        "/home/u/mnt/with space", "/home/u/task-mounts/reboot-recovery"]
    assert {m["fstype"] for m in mounts} == {"fuse.examplefs"}


def test_snapshot_writes_only_when_content_changed(world):
    world.run("snapshot", now=T0)
    world.run("snapshot", now=T0 + 60)
    assert len(world.snapshots()) == 1
    world.set_status("sess-aaa", "busy")
    world.run("snapshot", now=T0 + 120)
    assert len(world.snapshots()) == 2


def test_snapshot_force_writes_unchanged_content(world):
    world.run("snapshot", now=T0)
    world.run("snapshot", "--force", now=T0)
    assert len(world.snapshots()) == 2


def test_snapshot_file_is_written_atomically_without_leftovers(world):
    world.run("snapshot")
    leftovers = [p.name for p in (world.state / "snapshots" / "boot-1").iterdir()
                 if not p.name.endswith(".json") or p.name.startswith(".")]
    assert leftovers == []


def test_snapshot_refuses_state_dir_under_fuse_mount(world):
    world.state = Path("/home/u/task-mounts/reboot-recovery/state")
    res = world.run("snapshot")
    assert res.returncode != 0
    assert "FUSE" in res.stderr


def test_snapshot_rotation_keeps_fifty_per_boot(world):
    for i in range(55):
        assert world.run("snapshot", "--force", now=T0 + i).returncode == 0
    names = [p.name for p in world.snapshots()]
    iso = load_module().iso_ts
    assert len(names) == 50
    assert names[0] == f"{iso(T0 + 5)}.json"
    assert names[-1] == f"{iso(T0 + 54)}.json"


def test_snapshot_rotation_keeps_last_snapshot_of_three_previous_boots(world):
    for i in range(1, 6):
        bdir = world.state / "snapshots" / f"old-boot-{i}"
        bdir.mkdir(parents=True)
        for j in range(3):
            (bdir / f"2026010{i}T00000{j}Z.json").write_text("{}")
    world.run("snapshot", now=T0)
    snaps = world.state / "snapshots"
    assert sorted(p.name for p in snaps.iterdir()) == [
        "boot-1", "old-boot-3", "old-boot-4", "old-boot-5"]
    for i in (3, 4, 5):
        assert [p.name for p in (snaps / f"old-boot-{i}").iterdir()] == [f"2026010{i}T000002Z.json"]


def test_select_takes_other_boot_even_when_current_boot_is_newer(tmp_path):
    mod = load_module()
    other = tmp_path / "snapshots" / "boot-A"
    cur = tmp_path / "snapshots" / "boot-B"
    other.mkdir(parents=True)
    cur.mkdir(parents=True)
    (other / "20260101T000000Z.json").write_text("{}")
    (cur / "20260102T000000Z.json").write_text("{}")
    assert mod.select_snapshot(tmp_path, "boot-B") == other / "20260101T000000Z.json"
    assert mod.select_snapshot(tmp_path, "boot-B", any_boot=True) == cur / "20260102T000000Z.json"


def test_select_skips_empty_snapshot_of_intermediate_boot(tmp_path):
    mod = load_module()
    for boot, name, body in (
            ("boot-A", "20260101T000000Z.json", '{"sessions": [{"sessionId": "s1"}]}'),
            ("boot-B", "20260102T000000Z.json", '{"sessions": []}')):
        d = tmp_path / "snapshots" / boot
        d.mkdir(parents=True)
        (d / name).write_text(body)
    (tmp_path / "snapshots" / "boot-C").mkdir()
    assert mod.select_snapshot(tmp_path, "boot-C") == tmp_path / "snapshots" / "boot-A" / "20260101T000000Z.json"


def test_select_returns_none_without_foreign_boot_snapshot(tmp_path):
    mod = load_module()
    cur = tmp_path / "snapshots" / "boot-B"
    cur.mkdir(parents=True)
    (cur / "20260102T000000Z.json").write_text("{}")
    assert mod.select_snapshot(tmp_path, "boot-B") is None
    assert mod.select_snapshot(tmp_path / "missing", "boot-B") is None


def test_select_after_reboot_returns_pre_crash_snapshot(world):
    world.run("snapshot", boot="boot-1", now=T0)
    pre_crash = world.snapshots("boot-1")[-1]
    res = world.run("snapshot", boot="boot-2", now=T0 + 1000, config_dirs=str(world.root / "empty"))
    assert res.returncode == 0
    assert session_ids(world.last_snapshot("boot-2")) == set()
    chosen = world.run("select", boot="boot-2")
    assert chosen.returncode == 0
    assert Path(chosen.stdout.strip()) == pre_crash
    newest = world.run("select", "--any-boot", boot="boot-2")
    assert Path(newest.stdout.strip()).parent.name == "boot-2"


def test_select_cli_fails_when_nothing_to_restore(world):
    world.run("snapshot", boot="boot-1")
    assert world.run("select", boot="boot-1").returncode == 1


def test_status_check_fresh_passes_right_after_snapshot(world):
    world.run("snapshot", now=T0)
    assert world.run("status", "--check-fresh", "180", now=T0 + 5).returncode == 0


def test_status_check_fresh_fails_on_old_stamp(world):
    world.run("snapshot", now=T0)
    assert world.run("status", "--check-fresh", "180", now=T0 + 600).returncode == 1


def test_status_check_fresh_fails_without_stamp(world):
    assert world.run("status", "--check-fresh", "180").returncode == 1


def test_status_check_fresh_fails_when_session_sets_differ(world):
    world.run("snapshot", now=T0)
    world.kill(301)
    assert world.run("status", "--check-fresh", "180", now=T0 + 5).returncode == 1


def test_status_check_fresh_fails_on_empty_snapshot_with_live_sessions(world):
    world.run("snapshot", now=T0, config_dirs=str(world.root / "empty"))
    assert session_ids(world.last_snapshot()) == set()
    assert world.run("status", "--check-fresh", "180", now=T0 + 5).returncode == 1


def test_status_check_fresh_passes_when_nothing_is_live_and_snapshot_is_empty(world):
    empty = str(world.root / "empty")
    world.run("snapshot", now=T0, config_dirs=empty)
    assert world.run("status", "--check-fresh", "180", now=T0 + 5, config_dirs=empty).returncode == 0


def test_status_unchanged_snapshot_run_still_refreshes_freshness(world):
    world.run("snapshot", now=T0)
    world.run("snapshot", now=T0 + 600)
    assert len(world.snapshots()) == 1
    assert world.run("status", "--check-fresh", "180", now=T0 + 600).returncode == 0


def test_status_without_check_fresh_reports_and_exits_zero(world):
    world.run("snapshot", now=T0)
    res = world.run("status", now=T0 + 10)
    assert res.returncode == 0
    info = json.loads(res.stdout)
    assert info["stamp_age_s"] == 10
    assert info["snapshot_sessions"] == info["live_sessions"] == ["sess-aaa", "sess-bbb", "sess-ccc"]


@pytest.mark.parametrize("sub", ["snapshot", "status", "select"])
def test_snapshot_status_select_help_exit_zero(world, sub):
    assert world.run(sub, "--help").returncode == 0
