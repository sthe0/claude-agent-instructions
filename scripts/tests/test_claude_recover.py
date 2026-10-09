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
    fuse_mnt = world.root / "fuse-mnt"
    with open(world.proc / "self/mountinfo", "a") as fh:
        fh.write(f"50 22 0:50 / {fuse_mnt} rw,relatime shared:30 - fuse.examplefs examplefs rw\n")
    world.state = fuse_mnt / "state"
    res = world.run("snapshot")
    assert res.returncode != 0
    assert "FUSE" in res.stderr
    assert not world.state.exists()


def test_snapshot_tmux_failure_writes_nothing_and_keeps_stamp(world):
    world.run("snapshot", now=T0)
    stamp_before = (world.state / "last-ok").read_text()
    world.tmux.write_text('#!/bin/sh\necho "no server running" >&2\nexit 1\n')
    res = world.run("snapshot", now=T0 + 600)
    assert res.returncode != 0
    assert "Traceback" not in res.stderr
    assert len(world.snapshots()) == 1
    assert (world.state / "last-ok").read_text() == stamp_before


def test_snapshot_missing_tmux_binary_writes_nothing(world):
    world.tmux.unlink()
    res = world.run("snapshot")
    assert res.returncode != 0
    assert not (world.state / "last-ok").exists()
    assert not (world.state / "snapshots").exists()


def test_snapshot_same_second_collision_sorts_after_original(world):
    world.run("snapshot", now=T0)
    world.set_status("sess-aaa", "busy")
    assert world.run("snapshot", "--force", now=T0).returncode == 0
    snaps = world.snapshots()
    assert len(snaps) == 2
    newest = {s["sessionId"]: s for s in world.last_snapshot()["sessions"]}
    assert newest["sess-aaa"]["status"] == "busy"
    chosen = world.run("select", "--any-boot")
    assert Path(chosen.stdout.strip()) == snaps[-1]


def test_snapshot_files_and_state_dir_are_private(world):
    world.run("snapshot")
    assert (world.state.stat().st_mode & 0o777) == 0o700
    assert (world.state / "snapshots" / "boot-1").stat().st_mode & 0o777 == 0o700
    assert world.snapshots()[-1].stat().st_mode & 0o777 == 0o600
    assert (world.state / "last-ok").stat().st_mode & 0o777 == 0o600


def test_snapshot_removes_stale_tmp_files(world):
    boot_dir = world.state / "snapshots" / "boot-1"
    boot_dir.mkdir(parents=True)
    stale = [world.state / ".last-ok.tmp.4242", boot_dir / ".20260101T000000Z.json.tmp.4242"]
    for path in stale:
        path.write_text("partial")
    world.run("snapshot")
    assert [p.exists() for p in stale] == [False, False]


def test_snapshot_keeps_tmp_file_of_a_live_writer(world):
    live = world.state / f".restore-plan.json.tmp.{os.getpid()}"
    old = world.state / f".old.json.tmp.{os.getpid()}"
    world.state.mkdir(parents=True)
    live.write_text("in flight")
    old.write_text("pid reused")
    os.utime(old, (T0, T0))
    world.run("snapshot")
    assert live.exists() and not old.exists()


def test_write_atomic_replaces_via_rename_of_a_sibling_file(tmp_path, monkeypatch):
    mod = load_module()
    target = tmp_path / "out.json"
    target.write_text("old")
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((Path(src), Path(dst), Path(src).read_text(), Path(dst).read_text()))
        real_replace(src, dst)

    monkeypatch.setattr(mod.os, "replace", spy)
    mod.write_atomic(target, "new")
    assert len(seen) == 1
    src, dst, src_text, dst_text = seen[0]
    assert src != dst and src.parent == dst.parent
    assert (src_text, dst_text) == ("new", "old")
    assert target.read_text() == "new"


def test_strip_prompt_drops_positional_prompt_after_variadic_flag():
    mod = load_module()
    argv = ["/usr/bin/claude", "--add-dir", "/a", "~/b", "prompt text", "plainword"]
    assert mod.strip_prompt(argv) == ["claude", "--add-dir", "/a", "~/b"]
    assert mod.strip_prompt(["claude", "--add-dir", "/a", "do this"]) == ["claude", "--add-dir", "/a"]


def test_strip_prompt_keeps_every_value_of_variadic_flags():
    mod = load_module()
    argv = ["/usr/bin/claude", "--add-dir", "/a", "/b", "--model", "opus", "--resume", "sid",
            "SECRET-PROMPT"]
    assert mod.strip_prompt(argv) == [
        "claude", "--add-dir", "/a", "/b", "--model", "opus", "--resume", "sid"]


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


def test_snapshot_rotation_keeps_newest_nonempty_snapshot_of_previous_boot(world):
    bdir = world.state / "snapshots" / "old-boot"
    bdir.mkdir(parents=True)
    bodies = {"20260101T000000Z": '{"sessions": [{"sessionId": "s1"}]}',
              "20260101T000001Z": '{"sessions": []}',
              "20260101T000002Z": '{"sessions": []}'}
    for stem, body in bodies.items():
        (bdir / f"{stem}.json").write_text(body)
    world.run("snapshot", now=T0)
    assert sorted(p.name for p in bdir.iterdir()) == [
        "20260101T000000Z.json", "20260101T000002Z.json"]


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


BOOT1_AT = 1759990000  # the fixture's btime; "just after boot" is this + 120 s


def test_snapshot_does_not_replace_pending_restore_source_on_second_reboot(world):
    world.run("snapshot", boot="boot-1", now=T0)
    full = world.snapshots("boot-1")[-1]
    # boot-2 is young, its restore has not finished: the timer's snapshot must not land
    res = world.run("snapshot", boot="boot-2", now=BOOT1_AT + 120, config_dirs=str(world.root / "partial"))
    assert res.returncode == 0 and "pending" in res.stdout
    assert world.snapshots("boot-2") == []
    # second reboot mid-restore: the original full snapshot is still the selection
    chosen = world.run("select", boot="boot-3", now=BOOT1_AT + 600)
    assert Path(chosen.stdout.strip()) == full


def test_snapshot_skips_while_restore_lock_is_held(world):
    import fcntl
    world.run("snapshot", boot="boot-1", now=T0)
    world.state.mkdir(parents=True, exist_ok=True)
    with open(world.state / "restore.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        res = world.run("snapshot", boot="boot-1", now=T0 + 5000, config_dirs=str(world.root / "other"))
    assert res.returncode == 0 and "restore.lock" in res.stdout
    assert len(world.snapshots("boot-1")) == 1


def test_snapshot_proceeds_when_no_restore_is_pending(world):
    # no previous-boot snapshot at all
    assert world.run("snapshot", boot="boot-1", now=BOOT1_AT + 120).returncode == 0
    assert len(world.snapshots("boot-1")) == 1
    # previous snapshot exists, but this boot already restored
    (world.state / "done-boot-2").write_text("x\n")
    world.run("snapshot", boot="boot-2", now=BOOT1_AT + 120)
    assert len(world.snapshots("boot-2")) == 1
    # previous snapshot exists, restore pending, but the boot is old (restore never completed)
    world.run("snapshot", boot="boot-3", now=BOOT1_AT + 5 * 3600)
    assert len(world.snapshots("boot-3")) == 1


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


@pytest.mark.parametrize("args", [("--check-fresh", "180"), ()])
def test_status_corrupt_latest_snapshot_fails_without_traceback(world, args):
    world.run("snapshot", now=T0)
    world.snapshots()[-1].write_text("{not json")
    res = world.run("status", *args, now=T0 + 5)
    assert res.returncode == 1
    assert "Traceback" not in res.stderr


def test_status_tmux_failure_is_not_fresh(world):
    world.run("snapshot", now=T0)
    world.tmux.write_text("#!/bin/sh\nexit 1\n")
    res = world.run("status", "--check-fresh", "180", now=T0 + 5)
    assert res.returncode == 1
    assert "Traceback" not in res.stderr


def test_unreadable_boot_id_fails_cleanly(world):
    (world.proc / "sys/kernel/random/boot_id").unlink()
    res = world.run("status", boot="")
    assert res.returncode != 0
    assert "Traceback" not in res.stderr


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


# --- restore ----------------------------------------------------------------------------

PHASES = ["disk-pre", "mounts", "compose", "disk-post", "sessions"]
HOOK_PHASES = PHASES[:4]
MOUNT_A = "/home/u/task-mounts/reboot-recovery"
MOUNT_B = "/home/u/mnt/with space"
GIB_KIB = 1024 * 1024
ALL_SKIPS = {"skip-mounted", "skip-hard-disk", "skip-alive", "skip-no-transcript", "skip-unmounted"}

FAKE_TMUX = """#!/bin/sh
echo "tmux $*" >> "$FAKE_LOG"
case "$1" in
  list-panes) cat "$0.out" ;;
  has-session) [ -e "$0.session" ] ;;
  new-session|start-server|kill-server|kill-session) echo "forbidden: $1" >&2; exit 99 ;;
  *) exit 0 ;;
esac
"""
FAKE_MOUNTPOINT = '#!/bin/sh\ngrep -qxF "$2" "$MOUNTED_FILE" 2>/dev/null\n'
FAKE_DF = """#!/bin/sh
read used avail < "$DF_STATE"
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/x $((used + avail)) $used $avail 50% /home"
"""
FAKE_TOOL = """#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_LOG"
if [ -f "$DF_AFTER" ]; then cp "$DF_AFTER" "$DF_STATE"; fi
"""
FAKE_HOOK = """#!/bin/bash
echo "hook $RECOVER_PHASE dry=$RECOVER_DRY_RUN level=$RECOVER_DISK_LEVEL" >> "$FAKE_LOG"
if [ "$RECOVER_PHASE" = mounts ] && [ "$RECOVER_DRY_RUN" = 0 ]; then
  n=$(cat "$HOOK_COUNT" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$HOOK_COUNT"
  if [ "$n" -le "${HOOK_FAIL_FIRST:-0}" ]; then echo "network down" >&2; exit 1; fi
  python3 - "$RECOVER_PLAN" <<'PY'
import json, os, sys
for m in json.load(open(sys.argv[1]))["mounts"]:
    if m["action"] == "mount":
        open(os.environ["FAKE_LOG"], "a").write("mount " + m["target"] + "\\n")
        open(os.environ["MOUNTED_FILE"], "a").write(m["target"] + "\\n")
PY
fi
exit 0
"""


class RWorld(World):
    """A World with fake tmux/df/mountpoint/hooks that log every call to one file."""

    def __init__(self, root: Path):
        self.log_file = root / "calls.log"
        self.log_file.write_text("")
        self.bin = root / "bin"
        self.bin.mkdir()
        self.hooks = root / "hooks"
        self.hooks.mkdir()
        self.mounted_file = root / "mounted.txt"
        self.mounted_file.write_text("")
        self.df_state = root / "df.state"
        self.df_after = root / "df.after"
        self.hook_count = root / "hook.count"
        super().__init__(root)
        for name, body in (("mountpoint", FAKE_MOUNTPOINT), ("df", FAKE_DF)):
            self.script(self.bin / name, body)
        for name in ("docker", "ya", "arc", "systemctl", "sweep"):
            self.script(self.bin / name, FAKE_TOOL)
        self.script(self.hooks / "20-mount.sh", FAKE_HOOK)
        (self.hooks / "essential-mounts.txt").write_text(f"# essential\n{MOUNT_A}\n")
        Path(str(self.tmux) + ".session").write_text("")
        self.set_disk(200)
        for sid, cwd in (("sess-aaa", "/home/u/proj"), ("sess-bbb", "/home/u/ops"), ("sess-ccc", "/home/u/misc")):
            self.transcript(sid, cwd)

    @staticmethod
    def script(path: Path, body: str):
        path.write_text(body)
        path.chmod(0o755)

    def write_tmux(self):
        super().write_tmux()
        self.script(self.tmux, FAKE_TMUX)

    def transcript(self, sid: str, cwd: str, present: bool = True):
        path = Path(self.cfg) / "projects" / cwd.replace("/", "-").replace(".", "-") / f"{sid}.jsonl"
        if present:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")
        else:
            path.unlink(missing_ok=True)

    def set_disk(self, free_gib: float, total_gib: float = 400):
        free = int(free_gib * GIB_KIB)
        self.df_state.write_text(f"{int(total_gib * GIB_KIB) - free} {free}\n")

    def set_disk_after_cleanup(self, free_gib: float, total_gib: float = 400):
        free = int(free_gib * GIB_KIB)
        self.df_after.write_text(f"{int(total_gib * GIB_KIB) - free} {free}\n")

    def env(self, **kw):
        env = super().env(**kw)
        env.update({
            "PATH": f"{self.bin}{os.pathsep}{env['PATH']}",
            "FAKE_LOG": str(self.log_file),
            "MOUNTED_FILE": str(self.mounted_file),
            "DF_STATE": str(self.df_state),
            "DF_AFTER": str(self.df_after),
            "HOOK_COUNT": str(self.hook_count),
            "DF_BIN": str(self.bin / "df"),
            "MOUNTPOINT_BIN": str(self.bin / "mountpoint"),
            "CLAUDE_RECOVER_HOOKS_DIR": str(self.hooks),
            "CLAUDE_RECOVER_SWEEP_BIN": str(self.bin / "sweep"),
            "CLAUDE_RECOVER_SESSION_PAUSE_S": "0",
            "CLAUDE_RECOVER_RETRY_PAUSE_S": "0",
            "CLAUDE_RECOVER_TMUX_WAIT_S": "0",
        })
        return env

    def reboot(self, keep_alive=(), boot="boot-2"):
        """Snapshot the running machine, then lose every process except those named."""
        assert self.run("snapshot", boot="boot-1", now=T0).returncode == 0
        for pid in self.procs:
            if int(pid) not in keep_alive:
                self.kill(int(pid))
        Path(str(self.tmux) + ".out").write_text("")
        self.log_file.write_text("")
        return boot

    def calls(self) -> list[str]:
        return self.log_file.read_text().splitlines()

    def plan(self, *args, boot="boot-2", expect=0, **kw):
        res = self.run(*args, "--format", "json", boot=boot, **kw)
        assert res.returncode == expect, res.stderr
        return json.loads(res.stdout)

    def edit_snapshot(self, fn, name="edited.json") -> Path:
        mod = load_module()
        snap = json.loads(self.snapshots()[-1].read_text())
        fn(snap)
        snap["sha256"] = mod.content_hash(snap)
        path = self.root / name
        path.write_text(json.dumps(snap))
        return path


@pytest.fixture
def rw(tmp_path):
    return RWorld(tmp_path)


def by(plan, section, key):
    return {x[key]: x for x in plan[section]}


def actions(plan, section):
    return {x["action"] for x in plan[section]}


def assert_no_forbidden_calls(world: RWorld):
    text = "\n".join(world.calls())
    for bad in ("new-session", "start-server", "kill-server", "systemctl"):
        assert bad not in text


def new_windows(world: RWorld) -> list[str]:
    return [c for c in world.calls() if c.startswith("tmux new-window")]


def mutating_calls(world: RWorld) -> list[str]:
    return [c for c in world.calls()
            if c.split()[0] in ("docker", "ya", "arc", "sweep", "systemctl", "mount")
            or c.startswith("tmux new-")]


def test_restore_is_the_default_command(rw):
    rw.reboot()
    bare = rw.plan("--dry-run")
    explicit = rw.plan("restore", "--dry-run")
    assert bare == explicit
    assert bare["dry_run"] is True


def test_plan_json_contract(rw):
    rw.reboot()
    plan = rw.plan("--dry-run")
    assert set(plan) >= {"dry_run", "snapshot", "snapshot_boot_id", "current_boot_id", "phases", "disk",
                         "mounts", "sessions", "summary"}
    assert plan["phases"] == ["disk-pre", "mounts", "compose", "disk-post", "sessions"]
    assert plan["snapshot_boot_id"] == "boot-1" and plan["current_boot_id"] == "boot-2"
    assert set(plan["disk"]) >= {"free_gib", "free_pct", "level", "actions"}
    assert plan["disk"]["level"] == "ok" and plan["disk"]["actions"] == []
    for m in plan["mounts"]:
        assert set(m) >= {"target", "essential", "action", "status"}
        assert m["action"] in ("mount", "skip-mounted", "skip-hard-disk")
    for s in plan["sessions"]:
        assert set(s) >= {"session_id", "cwd", "name", "action", "command", "status"}
        assert s["action"] in ("resume", "reopen") or s["action"] in ALL_SKIPS
        assert s["status"] in ("planned", "done", "failed", "skipped")


def test_dry_run_hooks_receive_the_phase_names_in_order(rw):
    rw.reboot()
    plan = rw.plan("--dry-run")
    phases = [c.split()[1] for c in rw.calls() if c.startswith("hook ")]
    assert phases == HOOK_PHASES == plan["phases"][:4]
    assert all(c.endswith("level=ok") for c in rw.calls() if c.startswith("hook "))
    assert all("dry=1" in c for c in rw.calls() if c.startswith("hook "))


def test_dry_run_mutates_nothing(rw):
    rw.set_disk(5)
    rw.reboot()
    before = sorted(p.name for p in rw.state.rglob("*"))
    plan = rw.plan("--dry-run")
    assert plan["disk"]["level"] == "hard"
    assert mutating_calls(rw) == []
    assert sorted(p.name for p in rw.state.rglob("*")) == before
    assert rw.mounted_file.read_text() == ""
    assert {a["status"] for a in plan["disk"]["actions"]} == {"planned"}
    assert not (rw.state / "recover.log").exists()


def test_simulate_reboot_plans_everything_from_the_current_boot_snapshot(rw):
    rw.run("snapshot", boot="boot-1", now=T0)
    plan = rw.plan("--dry-run", "--simulate-reboot", boot="boot-1")
    assert actions(plan, "mounts") == {"mount"}
    assert {s["action"] for s in plan["sessions"]} <= {"resume", "reopen"}
    assert len(plan["sessions"]) == 3
    assert plan["simulate_reboot"] is True


def test_simulate_reboot_ignores_what_is_really_mounted_and_alive(rw):
    rw.run("snapshot", boot="boot-1", now=T0)
    rw.mounted_file.write_text(f"{MOUNT_A}\n{MOUNT_B}\n")
    live = rw.plan("--dry-run", "--any-boot", boot="boot-1")
    assert actions(live, "mounts") == {"skip-mounted"}
    assert actions(live, "sessions") == {"skip-alive"}
    simulated = rw.plan("--dry-run", "--simulate-reboot", boot="boot-1")
    assert actions(simulated, "mounts") == {"mount"}


def test_simulate_reboot_is_refused_without_dry_run(rw):
    rw.reboot()
    res = rw.run("--simulate-reboot", boot="boot-2")
    assert res.returncode == 2
    assert mutating_calls(rw) == []


def test_current_boot_snapshot_needs_any_boot(rw):
    rw.run("snapshot", boot="boot-1", now=T0)
    assert rw.run("--dry-run", boot="boot-1").returncode == 1
    assert rw.run("--dry-run", "--any-boot", boot="boot-1").returncode == 0


def test_live_machine_plan_has_only_skips(rw):
    rw.run("snapshot", boot="boot-1", now=T0)
    rw.mounted_file.write_text(f"{MOUNT_A}\n{MOUNT_B}\n")
    plan = rw.plan("--dry-run", "--any-boot", boot="boot-1")
    assert actions(plan, "mounts") | actions(plan, "sessions") <= ALL_SKIPS
    assert actions(plan, "sessions") == {"skip-alive"}


def test_snapshot_option_selects_a_given_file(rw):
    rw.reboot()
    path = rw.edit_snapshot(lambda s: s.update(sessions=s["sessions"][:1]))
    plan = rw.plan("--dry-run", "--snapshot", str(path))
    assert [s["session_id"] for s in plan["sessions"]] == ["sess-aaa"]
    assert plan["snapshot"] == str(path)


def test_snapshot_with_wrong_checksum_is_refused(rw):
    rw.reboot()
    path = rw.edit_snapshot(lambda s: None)
    data = json.loads(path.read_text())
    data["sessions"] = []
    path.write_text(json.dumps(data))
    res = rw.run("--dry-run", "--snapshot", str(path), boot="boot-2")
    assert res.returncode == 1 and "sha256" in res.stderr


@pytest.mark.parametrize("bad", [
    {"sessions": [{"cwd": "/x"}]},
    {"sessions": [None]},
    {"mounts": [{"x": 1}]},
    {"mounts": None},
])
def test_malformed_snapshot_entries_are_dropped_not_fatal(rw, bad):
    rw.reboot()
    path = rw.edit_snapshot(lambda s: (s.update(bad), s.pop("sha256", None)))
    res = rw.run("--dry-run", "--snapshot", str(path), "--format", "json", boot="boot-2")
    assert res.returncode == 0 and "Traceback" not in res.stderr
    plan = json.loads(res.stdout)
    assert all(isinstance(s["session_id"], str) for s in plan["sessions"])


def test_fixture_snapshot_with_unmounted_target_plans_a_mount(rw):
    plan = rw.plan("--dry-run", "--snapshot", str(FIXTURES / "snapshot-unmounted.json"))
    target = "/home/the0/task-mounts/__recover-fixture-not-mounted"
    assert by(plan, "mounts", "target")[target]["action"] == "mount"
    assert plan["sessions"][0]["action"] == "reopen"


def test_full_restore_mounts_then_opens_every_session(rw):
    rw.reboot()
    plan = rw.plan()
    assert plan["dry_run"] is False
    assert {m["target"]: m["status"] for m in plan["mounts"]} == {MOUNT_A: "done", MOUNT_B: "done"}
    assert {s["action"] for s in plan["sessions"]} <= {"resume", "reopen"}
    assert {s["status"] for s in plan["sessions"]} == {"done"}
    assert len(new_windows(rw)) == 3
    sess = by(plan, "sessions", "session_id")["sess-aaa"]
    cmd = next(c for c in new_windows(rw) if "sess-aaa" in c)
    assert "-d -t ccgram: -n work -c /home/u/proj" in cmd
    assert "claude --resume sess-aaa" in cmd and "CLAUDE_PERSONAL=1" in cmd
    assert "EXITED" in cmd and "SECRET" not in cmd and "--model" not in cmd
    assert sess["command"][:3] == [str(rw.tmux), "new-window", "-d"]
    assert_no_forbidden_calls(rw)


def test_phase_order_in_a_real_run(rw):
    rw.reboot()
    rw.plan()
    seq = []
    for c in rw.calls():
        if c.startswith("hook "):
            seq.append(c.split()[1])
        elif c.startswith("tmux new-window") and seq[-1:] != ["sessions"]:
            seq.append("sessions")
    assert seq == HOOK_PHASES + ["sessions"]


def test_second_run_is_a_no_op_once_everything_is_up(rw):
    rw.reboot()
    rw.plan()
    first_mounts = [c for c in rw.calls() if c.startswith("mount ")]
    assert sorted(first_mounts) == sorted([f"mount {MOUNT_A}", f"mount {MOUNT_B}"])
    rw.build_proc()
    rw.log_file.write_text("")
    plan = rw.plan()
    assert actions(plan, "mounts") == {"skip-mounted"}
    assert actions(plan, "sessions") == {"skip-alive"}
    assert new_windows(rw) == []
    assert [c for c in rw.calls() if c.startswith("mount ")] == []


def test_hook_never_gets_a_target_twice(rw):
    rw.reboot()
    rw.plan()
    rw.plan()
    targets = [c for c in rw.calls() if c.startswith("mount ")]
    assert len(targets) == len(set(targets)) == 2


def test_alive_session_is_not_reopened(rw):
    rw.reboot(keep_alive=(301,))
    plan = rw.plan("--dry-run")
    sessions = by(plan, "sessions", "session_id")
    assert sessions["sess-ccc"]["action"] == "skip-alive" and sessions["sess-ccc"]["command"] == []
    assert sessions["sess-aaa"]["action"] in ("resume", "reopen")
    rw.plan()
    assert not any("sess-ccc" in c for c in new_windows(rw))


def test_session_with_missing_transcript_is_skipped(rw):
    rw.reboot()
    rw.transcript("sess-bbb", "/home/u/ops", present=False)
    plan = rw.plan("--dry-run")
    assert by(plan, "sessions", "session_id")["sess-bbb"]["action"] == "skip-no-transcript"


def test_mount_hook_failure_leaves_session_in_that_mount_unmounted(rw):
    rw.reboot()
    path = rw.edit_snapshot(lambda s: s["sessions"][0].update(cwd=MOUNT_A + "/work"))
    env = {"HOOK_FAIL_FIRST": "99"}
    res = subprocess.run([sys.executable, str(MODULE_PATH), "--snapshot", str(path), "--format", "json"],
                         env={**rw.env(boot="boot-2"), **env}, capture_output=True, text=True, timeout=60)
    plan = json.loads(res.stdout)
    assert res.returncode == 1
    assert by(plan, "mounts", "target")[MOUNT_A]["status"] == "failed"
    sessions = by(plan, "sessions", "session_id")
    assert sessions["sess-aaa"]["action"] == "skip-unmounted"
    assert sessions["sess-bbb"]["action"] in ("resume", "reopen")
    assert not any("sess-aaa" in c for c in new_windows(rw))


@pytest.mark.parametrize("free,level", [(200, "ok"), (30, "warn"), (60, "ok"), (9, "hard"), (11, "warn")])
def test_disk_level_thresholds(rw, free, level):
    rw.set_disk(free)
    rw.reboot()
    assert rw.plan("--dry-run")["disk"]["level"] == level


def test_disk_level_warns_on_low_percentage_even_with_many_gib(rw):
    rw.set_disk(70, total_gib=1000)
    rw.reboot()
    assert rw.plan("--dry-run")["disk"]["level"] == "warn"


def test_hard_disk_mounts_only_essential_mounts(rw):
    rw.set_disk(5)
    rw.reboot()
    plan = rw.plan("--dry-run")
    mounts = by(plan, "mounts", "target")
    assert mounts[MOUNT_A]["essential"] is True and mounts[MOUNT_A]["action"] == "mount"
    assert mounts[MOUNT_B]["essential"] is False and mounts[MOUNT_B]["action"] == "skip-hard-disk"
    assert mounts[MOUNT_B]["status"] == "skipped"


def test_hard_disk_simulation_keeps_the_real_disk_level(rw):
    rw.set_disk(5)
    rw.run("snapshot", boot="boot-1", now=T0)
    plan = rw.plan("--dry-run", "--simulate-reboot", boot="boot-1")
    assert plan["disk"]["level"] == "hard"
    assert by(plan, "mounts", "target")[MOUNT_B]["action"] == "skip-hard-disk"


def test_hard_disk_skips_sessions_of_unmounted_mounts_only(rw):
    rw.set_disk(5)
    rw.reboot()
    path = rw.edit_snapshot(lambda s: s["sessions"][0].update(cwd=MOUNT_B + "/x"))
    plan = rw.plan("--dry-run", "--snapshot", str(path))
    sessions = by(plan, "sessions", "session_id")
    assert sessions["sess-aaa"]["action"] == "skip-unmounted"
    assert sessions["sess-bbb"]["action"] in ("resume", "reopen")


def test_cleanup_actions_have_classes_and_hard_level_lists_confirm_commands(rw):
    rw.set_disk(5)
    rw.reboot()
    acts = rw.plan("--dry-run")["disk"]["actions"]
    classes = {a["class"] for a in acts}
    assert classes == {"run", "report", "confirm"}
    assert any(a["cmd"] == "docker system prune -a" and a["class"] == "confirm" for a in acts)
    assert any(a["cmd"] == "arc gc --dry-run" and a["class"] == "report" for a in acts)
    assert not any(a["cmd"] == "arc gc" and a["class"] != "confirm" for a in acts)


def test_cleanup_runs_before_mounts_and_a_recovered_disk_lifts_hard_mode(rw):
    rw.set_disk(5)
    rw.set_disk_after_cleanup(120)
    rw.reboot()
    plan = rw.plan()
    assert plan["disk"]["initial_level"] == "hard" and plan["disk"]["level"] == "ok"
    assert {m["target"]: m["status"] for m in plan["mounts"]} == {MOUNT_A: "done", MOUNT_B: "done"}
    calls = rw.calls()
    first_hook = next(i for i, c in enumerate(calls) if c.startswith("hook "))
    assert calls.index("docker image prune -f") < first_hook
    assert all(a["status"] == "done" for a in plan["disk"]["actions"] if a["class"] == "run" and a["owner"] == "core")


def test_confirm_commands_are_never_executed(rw):
    rw.set_disk(5)
    rw.reboot()
    res = rw.run("--format", "json", boot="boot-2")
    plan = json.loads(res.stdout)
    calls = "\n".join(rw.calls())
    for banned in ("docker system prune", "docker image prune -a", "arc gc", "arc unmount", "ya gc"):
        assert banned not in calls
    confirm = [a for a in plan["disk"]["actions"] if a["class"] == "confirm"]
    assert confirm and {a["status"] for a in confirm} == {"skipped"}
    assert "docker system prune -a" in res.stderr and "Needs your decision" in res.stderr


def test_auto_run_writes_pending_confirm_and_never_runs_confirm(rw):
    rw.set_disk(5)
    rw.reboot()
    res = rw.run("--auto", boot="boot-2")
    assert res.returncode == 0, res.stderr
    pending = (rw.state / "pending-confirm.txt").read_text()
    assert "docker system prune -a" in pending and "Needs your decision" in pending
    assert "docker system prune" not in "\n".join(rw.calls())
    assert "<4>" in res.stderr
    assert "Needs your decision" in (rw.state / "recover.log").read_text()


def test_healthy_disk_leaves_no_pending_confirm(rw):
    rw.reboot()
    (rw.state / "pending-confirm.txt").write_text("stale\n")
    rw.run("--auto", boot="boot-2")
    assert not (rw.state / "pending-confirm.txt").exists()


def test_auto_marker_makes_the_second_boot_run_a_no_op(rw):
    rw.reboot()
    assert rw.run("--auto", boot="boot-2").returncode == 0
    assert (rw.state / "done-boot-2").exists()
    rw.log_file.write_text("")
    res = rw.run("--auto", boot="boot-2")
    assert res.returncode == 0 and "already restored" in res.stderr
    assert rw.calls() == []
    assert rw.run(boot="boot-2").returncode == 0
    assert rw.calls() != []


def test_failed_auto_run_leaves_no_marker(rw):
    rw.reboot()
    res = subprocess.run([sys.executable, str(MODULE_PATH), "--auto"], capture_output=True, text=True,
                         env={**rw.env(boot="boot-2"), "HOOK_FAIL_FIRST": "99"}, timeout=60)
    assert res.returncode == 1
    assert not (rw.state / "done-boot-2").exists()


def test_auto_marker_not_written_when_tmux_session_missing(rw):
    rw.reboot()
    Path(str(rw.tmux) + ".session").unlink()
    res = rw.run("--auto", "--format", "json", boot="boot-2")
    assert res.returncode == 0
    details = {s["detail"] for s in json.loads(res.stdout)["sessions"]}
    assert any("not found" in d for d in details)
    assert not (rw.state / "done-boot-2").exists()


def test_auto_marker_not_written_when_deadline_skipped_sessions(rw):
    rw.reboot()
    res = subprocess.run([sys.executable, str(MODULE_PATH), "--auto"], capture_output=True, text=True,
                         timeout=60, env={**rw.env(boot="boot-2"), "CLAUDE_RECOVER_DEADLINE_S": "0"})
    assert res.returncode == 0
    assert not (rw.state / "done-boot-2").exists()


def test_auto_without_a_snapshot_is_not_an_error(rw):
    res = rw.run("--auto", boot="boot-2")
    assert res.returncode == 0 and "no snapshot" in res.stderr
    assert rw.run(boot="boot-2").returncode == 1


def test_auto_retries_the_mounts_phase_until_the_hook_succeeds(rw):
    rw.reboot()
    res = subprocess.run([sys.executable, str(MODULE_PATH), "--auto", "--format", "json"],
                         capture_output=True, text=True, timeout=60,
                         env={**rw.env(boot="boot-2"), "HOOK_FAIL_FIRST": "2"})
    assert res.returncode == 0, res.stderr
    mount_runs = [c for c in rw.calls() if c.startswith("hook mounts")]
    assert len(mount_runs) == 3
    assert MOUNT_A in rw.mounted_file.read_text().splitlines()
    assert {m["status"] for m in json.loads(res.stdout)["mounts"]} == {"done"}
    assert "retrying" in (rw.state / "recover.log").read_text()


def test_manual_run_does_not_retry(rw):
    rw.reboot()
    subprocess.run([sys.executable, str(MODULE_PATH)], capture_output=True, text=True, timeout=60,
                   env={**rw.env(boot="boot-2"), "HOOK_FAIL_FIRST": "2"})
    assert len([c for c in rw.calls() if c.startswith("hook mounts")]) == 1


def test_open_mode_sends_no_prompt_continue_mode_only_to_busy_sessions(rw):
    rw.reboot()
    open_plan = rw.plan("--dry-run")
    assert {s["with_prompt"] for s in open_plan["sessions"]} == {False}
    assert {s["action"] for s in open_plan["sessions"]} == {"reopen"}
    plan = rw.plan("--dry-run", "--mode", "continue")
    sessions = by(plan, "sessions", "session_id")
    assert sessions["sess-bbb"]["action"] == "resume" and sessions["sess-bbb"]["with_prompt"] is True
    assert sessions["sess-aaa"]["action"] == "reopen" and sessions["sess-aaa"]["with_prompt"] is False
    assert len(sessions["sess-bbb"]["command"][-1]) > len(sessions["sess-aaa"]["command"][-1])
    assert "agentctl status" in sessions["sess-bbb"]["command"][-1]
    assert "agentctl status" not in sessions["sess-aaa"]["command"][-1]


def test_missing_tmux_session_is_waited_for_and_never_created(rw):
    rw.reboot()
    Path(str(rw.tmux) + ".session").unlink()
    plan = rw.plan()
    assert {s["status"] for s in plan["sessions"]} == {"skipped"}
    assert any(c.startswith("tmux has-session") for c in rw.calls())
    assert new_windows(rw) == []
    assert_no_forbidden_calls(rw)


def test_low_memory_pauses_session_opening(rw):
    rw.reboot()
    (rw.proc / "meminfo").write_text("MemTotal: 33554432 kB\nMemAvailable: 1048576 kB\n")
    plan = rw.plan()
    assert {s["status"] for s in plan["sessions"]} == {"skipped"}
    assert "MemAvailable" in plan["sessions"][0]["detail"]
    assert new_windows(rw) == []


def test_new_window_failure_is_reported_and_fails_the_run(rw):
    rw.reboot()
    body = FAKE_TMUX.replace("*) exit 0 ;;", "new-window) echo boom >&2; exit 1 ;;\n  *) exit 0 ;;")
    rw.script(rw.tmux, body)
    res = rw.run("--format", "json", boot="boot-2")
    plan = json.loads(res.stdout)
    assert res.returncode == 1
    assert {s["status"] for s in plan["sessions"]} == {"failed"}
    assert plan["summary"]["failed"] >= 3


def test_deadline_turns_remaining_actions_into_skipped(rw):
    rw.reboot()
    res = subprocess.run([sys.executable, str(MODULE_PATH), "--format", "json"], capture_output=True,
                         text=True, timeout=60, env={**rw.env(boot="boot-2"), "CLAUDE_RECOVER_DEADLINE_S": "0"})
    plan = json.loads(res.stdout)
    assert res.returncode == 0
    every = [*plan["mounts"], *plan["sessions"]]
    assert {x["status"] for x in every} == {"skipped"}
    assert {x["detail"] for x in every} == {"deadline reached"}
    assert [c for c in rw.calls() if not c.startswith("tmux list-panes")] == []


def test_deadline_constant_covers_the_phase_budgets():
    mod = load_module()
    assert mod.DEADLINE_S == 45 * 60
    assert mod.DEADLINE_S > mod.CLEANUP_CAP_S + 15 * mod.SESSION_PAUSE_S


def test_log_has_a_line_per_action_and_a_summary(rw):
    rw.reboot()
    rw.plan()
    log = (rw.state / "recover.log").read_text()
    for needle in (MOUNT_A, MOUNT_B, "sess-aaa", "sess-bbb", "sess-ccc", "restore start", "restore done"):
        assert needle in log
    assert log.count("hook 20-mount.sh") == 4


def test_log_is_rotated_by_size(rw):
    rw.reboot()
    (rw.state).mkdir(parents=True, exist_ok=True)
    (rw.state / "recover.log").write_text("x" * 5000)
    subprocess.run([sys.executable, str(MODULE_PATH)], capture_output=True, text=True, timeout=60,
                   env={**rw.env(boot="boot-2"), "CLAUDE_RECOVER_LOG_MAX_BYTES": "1000"})
    assert (rw.state / "recover.log.1").read_text() == "x" * 5000
    assert "restore done" in (rw.state / "recover.log").read_text()


def test_restore_never_touches_systemctl_or_a_tmux_server(rw):
    rw.set_disk(5)
    rw.reboot()
    rw.plan()
    rw.plan("--dry-run", "--simulate-reboot", "--any-boot")
    assert_no_forbidden_calls(rw)
    assert not any(c.startswith("systemctl") for c in rw.calls())


def test_restore_without_hooks_warns_and_fails_the_mounts(rw):
    rw.reboot()
    for hook in rw.hooks.glob("*.sh"):
        hook.unlink()
    plan = rw.plan("--dry-run")
    assert any("no recover.d hooks" in w for w in plan["warnings"])
    assert actions(plan, "mounts") == {"mount"}


def test_text_format_lists_the_phases(rw):
    rw.reboot()
    res = rw.run("--dry-run", boot="boot-2")
    assert res.returncode == 0
    positions = [res.stdout.index(f"phase {p}") for p in PHASES]
    assert positions == sorted(positions)


def test_restore_help_documents_the_flags(rw):
    out = rw.run("restore", "--help").stdout
    for flag in ("--dry-run", "--snapshot", "--mode", "--simulate-reboot", "--any-boot", "--auto", "--format"):
        assert flag in out
    assert "restore" in rw.run("--help").stdout


def test_decide_mounts_is_pure_and_orders_essential_first():
    mod = load_module()
    snap = [{"mountpoint": "/z"}, {"mountpoint": "/a"}, {"mountpoint": "/e2"}, {"mountpoint": "/e1"}]
    out = mod.decide_mounts(snap, {"/a"}, "ok", ["/e1", "/e2"])
    assert [m["target"] for m in out] == ["/e1", "/e2", "/a", "/z"]
    assert {m["target"]: m["action"] for m in out} == {
        "/e1": "mount", "/e2": "mount", "/a": "skip-mounted", "/z": "mount"}
    hard = mod.decide_mounts(snap, {"/a"}, "hard", ["/e1", "/e2"])
    assert {m["target"]: m["action"] for m in hard} == {
        "/e1": "mount", "/e2": "mount", "/a": "skip-mounted", "/z": "skip-hard-disk"}
