"""Tests for the proof of death in scripts/reaper/liveness.py.

Hermetic: every tree is built under tmp_path; time is a ``now`` argument (a tree's ctime cannot
be backdated), a foreign uid, a foreign ``st_dev`` and unreadable /proc entries are injected
or planted in a fake proc root. The four holder kinds are also proven against real processes
started here, asserted on the sweep's own findings (not on the machine's /proc being clean).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper import liveness  # noqa: E402
from reaper.contract import KEEP, REMOVE  # noqa: E402

DAY = 86400.0
FLOOR = 3 * DAY


def make_tree(root: Path, name: str = "tmp.dead") -> Path:
    candidate = root / name
    (candidate / "sub").mkdir(parents=True)
    (candidate / "sub" / "file.txt").write_text("x" * 5000, encoding="utf-8")
    return candidate


def newest_time(top: Path) -> float:
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(top):
        for name in [".", *dirnames, *filenames]:
            st = os.lstat(os.path.join(dirpath, name))
            newest = max(newest, st.st_mtime, st.st_ctime)
    return newest


def judge(candidate: Path, **kw):
    params = dict(
        root_dev=os.stat(candidate.parent).st_dev,
        now=time.time() + 10 * DAY,
        floor_s=FLOOR,
        sweep=liveness.HolderSweep(),
        mounts=frozenset(),
        owned=lambda path: False,
    )
    params.update(kw)
    return liveness.judge_residue(str(candidate), **params)


def stat_with(path: str, **changes):
    st = os.lstat(path)
    fields = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
    fields.update(changes)
    return types.SimpleNamespace(**fields)


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path.resolve() / "root"
    path.mkdir()
    return path


@pytest.fixture
def procs():
    started = []
    yield started
    for proc in started:
        proc.kill()
        proc.wait()


def start_python(procs, code: str, **kw) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, **kw,
    )
    procs.append(proc)
    assert proc.stdout.readline().strip() == "ready"
    return proc


# ── the judgment, clause by clause ────────────────────────────────────────

def test_a_dead_idle_tree_is_removable_and_measured(root):
    candidate = make_tree(root)
    result = judge(candidate)
    assert (result.action, result.slug) == (REMOVE, "removable")
    assert result.size >= 5000


def test_a_tree_younger_than_the_idle_floor_is_kept(root):
    candidate = make_tree(root)
    result = judge(candidate, now=time.time())
    assert (result.action, result.slug) == (KEEP, liveness.IDLE_FLOOR)


def test_the_idle_floor_is_exact_at_the_newest_time_in_the_tree(root):
    candidate = make_tree(root)
    newest = newest_time(candidate)
    assert judge(candidate, now=newest + FLOOR - 5).slug == liveness.IDLE_FLOOR
    assert judge(candidate, now=newest + FLOOR + 5).action == REMOVE


def test_a_fresh_file_deep_in_the_tree_keeps_it(root):
    candidate = make_tree(root)
    old = time.time() - 10 * DAY
    for path in (candidate, candidate / "sub", candidate / "sub" / "file.txt"):
        os.utime(path, (old, old))
    now = newest_time(candidate) + 10 * DAY
    deep = candidate / "sub" / "fresh.txt"
    deep.write_text("y", encoding="utf-8")
    os.utime(deep, (now - 60, now - 60))
    os.utime(candidate / "sub", (old, old))
    assert judge(candidate, now=now).slug == liveness.IDLE_FLOOR


def test_a_candidate_of_another_uid_is_kept(root):
    candidate = make_tree(root)
    result = judge(candidate, uid=os.getuid() + 1)
    assert (result.action, result.slug) == (KEEP, liveness.FOREIGN_OWNER)


def test_a_symlink_in_place_of_a_candidate_is_kept_though_its_target_is_dead(root, tmp_path):
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    real = make_tree(outside, "real")
    link = root / "tmp.link"
    link.symlink_to(real)
    result = judge(link)
    assert (result.action, result.slug) == (KEEP, liveness.NOT_A_DIRECTORY)
    assert real.exists()


def test_a_plain_file_in_place_of_a_candidate_is_kept(root):
    plain = root / "tmp.file"
    plain.write_text("x", encoding="utf-8")
    assert judge(plain).slug == liveness.NOT_A_DIRECTORY


def test_a_candidate_owned_by_a_live_session_is_kept(root):
    candidate = make_tree(root)
    seen = []
    result = judge(candidate, owned=lambda path: seen.append(path) or True)
    assert (result.action, result.slug) == (KEEP, liveness.SESSION_OWNED)
    assert seen == [str(candidate)]


def test_a_mount_point_inside_the_tree_keeps_it(root):
    candidate = make_tree(root)
    inside = frozenset({str(candidate / "sub")})
    assert judge(candidate, mounts=inside).slug == liveness.MOUNT_INSIDE
    assert judge(candidate, mounts=frozenset({str(candidate)})).slug == liveness.MOUNT_INSIDE


def test_a_mount_point_beside_the_tree_with_a_longer_name_does_not_keep_it(root):
    candidate = make_tree(root)
    beside = frozenset({str(candidate) + "-other", str(root)})
    assert judge(candidate, mounts=beside).action == REMOVE


def test_an_unreadable_mountinfo_keeps_the_tree_with_os_error(root):
    candidate = make_tree(root)
    result = judge(candidate, mounts=None)
    assert (result.action, result.slug) == (KEEP, liveness.OS_ERROR)


def test_a_nested_git_directory_or_file_keeps_the_tree(root):
    for name, make in (("tmp.gitdir", lambda p: p.mkdir()), ("tmp.gitfile", lambda p: p.write_text("gitdir: x"))):
        candidate = make_tree(root, name)
        make(candidate / "sub" / ".git")
        assert judge(candidate).slug == liveness.NESTED_GIT


def test_a_symlink_inside_the_tree_is_not_followed_and_its_target_is_not_read(root, tmp_path):
    outside = tmp_path.resolve() / "outside"
    (outside / ".git").mkdir(parents=True)
    now = time.time() + 10 * DAY
    os.utime(outside, (now - 60, now - 60))
    candidate = make_tree(root)
    (candidate / "sub" / "link").symlink_to(outside)
    result = judge(candidate, now=now)
    assert (result.action, result.slug) == (REMOVE, "removable")


def test_an_entry_on_another_filesystem_is_not_entered_and_keeps_the_tree(root):
    candidate = make_tree(root)
    foreign = str(candidate / "sub")
    entered = []

    def lstat_fn(path):
        st = os.lstat(path)
        return stat_with(path, st_dev=st.st_dev + 1) if path == foreign else st

    def scandir_fn(path):
        entered.append(path)
        return os.scandir(path)

    result = judge(candidate, lstat_fn=lstat_fn, scandir_fn=scandir_fn)
    assert (result.action, result.slug) == (KEEP, liveness.MOUNT_INSIDE)
    assert foreign not in entered


def test_an_os_error_while_judging_keeps_with_the_error_as_reason(root):
    candidate = make_tree(root)
    target = str(candidate / "sub" / "file.txt")

    def lstat_fn(path):
        if path == target:
            raise PermissionError(13, "Permission denied", path)
        return os.lstat(path)

    result = judge(candidate, lstat_fn=lstat_fn)
    assert (result.action, result.slug) == (KEEP, liveness.OS_ERROR)
    assert "PermissionError" in result.detail


def test_a_vanished_candidate_resolves_to_keep(root):
    result = judge(root / "tmp.gone")
    assert (result.action, result.slug) == (KEEP, liveness.OS_ERROR)


# ── the /proc sweep: planted proc roots ───────────────────────────────────

def write_status(entry: Path, uid: int) -> None:
    (entry / "status").write_text(
        f"Name:\tx\nUmask:\t0022\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t0\t0\t0\t0\n", encoding="utf-8",
    )


def fake_proc(base: Path, pid: int, *, cwd=None, exe=None, fds=(), maps=(), uid=None) -> Path:
    entry = base / str(pid)
    (entry / "fd").mkdir(parents=True)
    write_status(entry, os.getuid() if uid is None else uid)
    if cwd is not None:
        (entry / "cwd").symlink_to(cwd)
    if exe is not None:
        (entry / "exe").symlink_to(exe)
    for number, target in enumerate(fds):
        (entry / "fd" / str(number)).symlink_to(target)
    lines = [f"7f00-7f10 r-xp 00000000 08:01 42 {path}" for path in maps]
    (entry / "maps").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return entry


def test_each_holder_kind_in_a_planted_proc_root_is_found_under_its_own_kind(root, tmp_path):
    proc = tmp_path.resolve() / "proc"
    held = root / "tmp.held"
    held.mkdir()
    fake_proc(proc, 11, cwd=held)
    fake_proc(proc, 12, exe=held / "bin")
    fake_proc(proc, 13, fds=[held / "log"])
    fake_proc(proc, 14, maps=[held / "lib.so"])
    sweep = liveness.sweep_holders(str(proc))
    assert sweep.incomplete is None
    assert {kind: [Path(p).name for p in paths] for kind, paths in sweep.held.items()} == {
        "cwd": ["tmp.held"], "exe": ["bin"], "fd": ["log"], "maps": ["lib.so"],
    }
    assert sweep.holder_of(str(held)) in liveness.HOLDER_KINDS
    assert sweep.holder_of(str(root / "tmp.other")) is None


def test_holder_lookup_matches_at_or_inside_and_not_a_longer_sibling_name(root, tmp_path):
    proc = tmp_path.resolve() / "proc"
    fake_proc(proc, 11, cwd=root / "tmp.a-sibling", fds=[root / "tmp.a" / "deep" / "f"])
    sweep = liveness.sweep_holders(str(proc))
    assert sweep.holder_of(str(root / "tmp.a")) == "fd"
    assert sweep.holder_of(str(root / "tmp.a-sibling")) == "cwd"
    assert sweep.holder_of(str(root / "tmp")) is None


def test_a_deleted_suffix_pipes_and_spaces_in_proc_targets_are_read_correctly(root, tmp_path):
    proc = tmp_path.resolve() / "proc"
    spaced = root / "tmp.sp ace"
    fake_proc(proc, 11, fds=[f"{root}/tmp.gone/f (deleted)", "pipe:[1234]", "anon_inode:[eventfd]"], maps=[spaced / "m.so"])
    sweep = liveness.sweep_holders(str(proc))
    assert sweep.held["fd"] == [f"{root}/tmp.gone/f"]
    assert sweep.held["maps"] == [str(spaced / "m.so")]


def unreadable_entry(proc: Path, pid: int, *, uid: int) -> None:
    """A process whose cwd link cannot be read (a directory in its place), owned by ``uid``."""
    entry = proc / str(pid)
    (entry / "cwd").mkdir(parents=True)
    write_status(entry, uid)


def test_a_process_proven_by_its_status_uid_to_be_another_users_is_skipped_and_counted(root, tmp_path):
    proc = tmp_path.resolve() / "proc"
    unreadable_entry(proc, 21, uid=os.getuid() + 1)
    fake_proc(proc, 22, cwd=root / "tmp.theirs", uid=os.getuid() + 1)
    sweep = liveness.sweep_holders(str(proc))
    assert (sweep.foreign_skipped, sweep.incomplete) == (2, None)
    assert sweep.holder_of(str(root / "tmp.theirs")) is None


def test_a_same_uid_process_whose_links_cannot_be_read_makes_the_sweep_incomplete(tmp_path):
    proc = tmp_path.resolve() / "proc"
    unreadable_entry(proc, 21, uid=os.getuid())
    sweep = liveness.sweep_holders(str(proc))
    assert sweep.foreign_skipped == 0 and sweep.incomplete and "cwd" in sweep.incomplete


def test_a_same_uid_process_with_unreadable_links_keeps_the_candidate(root, tmp_path):
    proc = tmp_path.resolve() / "proc"
    unreadable_entry(proc, 21, uid=os.getuid())
    sweep = liveness.sweep_holders(str(proc))
    result = judge(make_tree(root), sweep=sweep)
    assert (result.action, result.slug) == (KEEP, liveness.HOLDER_SWEEP_INCOMPLETE)


OTHER_UID = os.getuid() + 7  # never the owner of a planted /proc entry, which the test process creates


def test_a_process_of_this_uid_by_its_status_is_swept_whoever_owns_its_proc_entry(root, tmp_path):
    proc = tmp_path.resolve() / "proc"
    candidate = make_tree(root)
    fake_proc(proc, 23, cwd=candidate / "sub", uid=OTHER_UID)
    sweep = liveness.sweep_holders(str(proc), uid=OTHER_UID)
    assert sweep.foreign_skipped == 0 and sweep.holder_of(str(candidate)) == "cwd"


def test_a_non_dumpable_process_of_this_uid_is_judged_by_its_status_uid_not_its_directory_owner(tmp_path):
    proc = tmp_path.resolve() / "proc"
    unreadable_entry(proc, 23, uid=OTHER_UID)
    sweep = liveness.sweep_holders(str(proc), uid=OTHER_UID)
    assert sweep.foreign_skipped == 0 and sweep.incomplete and "pid=23" in sweep.incomplete


def test_any_of_the_four_uids_in_the_status_line_counts_as_this_uid(tmp_path):
    mine = OTHER_UID
    for number, line in enumerate((
        f"Uid:\t{mine}\t{mine}\t{mine}\t{mine}", f"Uid:\t0\t0\t0\t{mine}", f"Uid:\t0\t{mine}\t0\t0", f"Uid:\t0\t0\t{mine}\t0",
    )):
        proc = tmp_path.resolve() / f"proc{number}"
        entry = proc / "24"
        (entry / "cwd").mkdir(parents=True)
        (entry / "status").write_text(f"Name:\tx\n{line}\n", encoding="utf-8")
        sweep = liveness.sweep_holders(str(proc), uid=mine)
        assert sweep.foreign_skipped == 0 and sweep.incomplete, line


def test_a_status_without_a_uid_line_makes_the_sweep_incomplete(tmp_path):
    proc = tmp_path.resolve() / "proc"
    entry = proc / "25"
    entry.mkdir(parents=True)
    (entry / "status").write_text("Name:\tx\n", encoding="utf-8")
    sweep = liveness.sweep_holders(str(proc))
    assert sweep.foreign_skipped == 0 and sweep.incomplete and "no Uid line" in sweep.incomplete


def test_an_entry_whose_owner_cannot_be_read_makes_the_sweep_incomplete(tmp_path):
    proc = tmp_path.resolve() / "proc"
    fake_proc(proc, 31)

    def denied(path):
        raise PermissionError(13, "Permission denied", path)

    sweep = liveness.sweep_holders(str(proc), status_fn=denied)
    assert sweep.incomplete and "owner unreadable" in sweep.incomplete


def test_a_process_that_vanished_is_not_an_error(tmp_path):
    proc = tmp_path.resolve() / "proc"
    fake_proc(proc, 31)

    def gone(path):
        raise FileNotFoundError(2, "No such file", path)

    assert liveness.sweep_holders(str(proc), status_fn=gone).incomplete is None


def test_a_proc_root_that_cannot_be_listed_is_an_incomplete_sweep(tmp_path):
    assert liveness.sweep_holders(str(tmp_path / "no-proc")).incomplete


# ── the allowlist of same-uid system helpers (K10) ────────────────────────

MAIN_PID = 4859


def manager_is(pid):
    return lambda uid: pid


def helper_proc(base: Path, pid: int, name: str, argv0: str, *, ppid: int = 1, cmdline="auto", uid=None) -> Path:
    """A same-uid process whose cwd link cannot be read (a non-dumpable one), with its status and cmdline."""
    entry = base / str(pid)
    (entry / "cwd").mkdir(parents=True)
    me = os.getuid() if uid is None else uid
    (entry / "status").write_text(
        f"Name:\t{name}\nPPid:\t{ppid}\nUid:\t{me}\t{me}\t{me}\t{me}\n", encoding="utf-8",
    )
    if cmdline == "auto":
        cmdline = argv0 + "\0"
    if cmdline is not None:
        (entry / "cmdline").write_text(cmdline, encoding="utf-8")
    return entry


def sweep_of(proc: Path, **kw) -> liveness.HolderSweep:
    kw.setdefault("manager_pid_fn", manager_is(MAIN_PID))
    return liveness.sweep_holders(str(proc), **kw)


HELPERS = [
    pytest.param(MAIN_PID, "systemd", 1, "/usr/lib/systemd/systemd", id="manager-by-mainpid"),
    pytest.param(4860, "(sd-pam)", MAIN_PID, "(sd-pam)", id="sd-pam-of-the-manager"),
    pytest.param(30, "sshd", 20, "sshd: user@pts/28", id="sshd-session-process"),
    pytest.param(31, "sshd-session", 20, "sshd-session: user@pts/28", id="sshd-session"),
    pytest.param(32, "gpg-agent", MAIN_PID, "/usr/bin/gpg-agent", id="gpg-agent"),
    pytest.param(33, "ssh-agent", 1, "ssh-agent", id="ssh-agent"),
    pytest.param(34, "fusermount", 35, "fusermount", id="fusermount"),
    pytest.param(36, "fusermount3", 37, "fusermount3", id="fusermount3"),
]


@pytest.mark.parametrize(("pid", "name", "ppid", "argv0"), HELPERS)
def test_a_known_helper_with_unreadable_links_is_skipped_and_counted(tmp_path, pid, name, ppid, argv0):
    proc = tmp_path.resolve() / "proc"
    helper_proc(proc, pid, name, argv0, ppid=ppid)
    sweep = sweep_of(proc)
    assert (sweep.incomplete, sweep.helpers_skipped) == (None, 1)


def test_the_seventeen_unreadable_processes_seen_on_a_real_machine_leave_the_sweep_complete(tmp_path):
    proc = tmp_path.resolve() / "proc"
    helper_proc(proc, 4859, "systemd", "/usr/lib/systemd/systemd", ppid=1)
    helper_proc(proc, 4860, "(sd-pam)", "(sd-pam)", ppid=4859)
    helper_proc(proc, 3333455, "gpg-agent", "/usr/bin/gpg-agent", ppid=4859)
    helper_proc(proc, 2476276, "sshd", "sshd: user@pts/28", ppid=2476217)
    helper_proc(proc, 2478703, "sshd", "sshd: user@pts/33,pts/39,pts/40", ppid=2478485)
    fusermounts = (
        (2063752, 2063479), (2080892, 2080638), (2082500, 2082081), (2084968, 2084613), (2399734, 2399415),
        (2457345, 2456945), (2457711, 2457515), (2458151, 2457857), (2458554, 2458346), (3445810, 3445609),
        (542122, 541622), (746078, 744703),
    )
    for pid, ppid in fusermounts:
        helper_proc(proc, pid, "fusermount", "fusermount", ppid=ppid)
    sweep = sweep_of(proc, manager_pid_fn=manager_is(4859))
    assert (sweep.incomplete, sweep.helpers_skipped) == (None, 17)


def test_an_unreadable_same_uid_process_that_is_not_on_the_list_makes_the_sweep_incomplete(tmp_path):
    proc = tmp_path.resolve() / "proc"
    helper_proc(proc, 41, "screen", "screen", ppid=1)
    sweep = sweep_of(proc)
    assert sweep.helpers_skipped == 0
    assert sweep.incomplete.startswith("pid=41 name='screen' argv0='screen' errno=")
    assert "(EINVAL)" in sweep.incomplete and "/proc" in sweep.incomplete and "cwd" in sweep.incomplete


def test_the_cause_names_the_pid_the_status_name_the_raw_argv0_and_the_errno(tmp_path):
    proc = tmp_path.resolve() / "proc"
    helper_proc(proc, 42, "keyring", "/usr/bin/keyring-daemon --foreground", ppid=1)
    cause = sweep_of(proc).incomplete
    assert re.match(
        r"pid=42 name='keyring' argv0='/usr/bin/keyring-daemon --foreground' errno=\d+ \([A-Z]+\): ", cause,
    )


@pytest.mark.parametrize(("name", "argv0"), [
    pytest.param("sshd", "bash", id="listed-name-other-argv0"),
    pytest.param("bash", "sshd", id="other-name-listed-argv0"),
    pytest.param("sshd", "sshd-session", id="two-different-listed-names"),
    pytest.param("fusermount", "/tmp/evil dir/fusermount3", id="spaced-path-token-differs"),
])
def test_a_helper_needs_both_its_name_and_its_argv0_token_to_match_one_pair(tmp_path, name, argv0):
    proc = tmp_path.resolve() / "proc"
    helper_proc(proc, 43, name, argv0)
    sweep = sweep_of(proc)
    assert sweep.helpers_skipped == 0 and sweep.incomplete and "pid=43" in sweep.incomplete


def test_the_manager_name_is_exempt_only_as_the_mainpid_of_the_user_manager(tmp_path):
    proc = tmp_path.resolve() / "proc"
    helper_proc(proc, 44, "systemd", "/usr/lib/systemd/systemd", ppid=1)
    assert sweep_of(proc, manager_pid_fn=manager_is(MAIN_PID)).incomplete
    assert sweep_of(proc, manager_pid_fn=manager_is(44)).incomplete is None


@pytest.mark.parametrize("answer", [pytest.param(None, id="query-failed-or-timed-out"), pytest.param(0, id="mainpid-zero")])
def test_a_manager_query_that_gave_no_pid_exempts_neither_the_manager_nor_its_sd_pam(tmp_path, answer):
    proc = tmp_path.resolve() / "proc"
    helper_proc(proc, 44, "systemd", "systemd", ppid=1)
    helper_proc(proc, 45, "(sd-pam)", "(sd-pam)", ppid=44)
    # an unusable answer is what user_manager_main_pid returns for a failure; the sweep never sees 0
    sweep = sweep_of(proc, manager_pid_fn=lambda uid: answer or None)
    assert sweep.helpers_skipped == 0 and sweep.incomplete


def test_sd_pam_is_exempt_only_when_its_parent_is_the_manager(tmp_path):
    proc = tmp_path.resolve() / "proc"
    helper_proc(proc, 46, "(sd-pam)", "(sd-pam)", ppid=1234)
    sweep = sweep_of(proc)
    assert sweep.helpers_skipped == 0 and sweep.incomplete


def test_the_manager_is_asked_once_per_sweep_and_only_when_a_process_needs_it(tmp_path):
    proc = tmp_path.resolve() / "proc"
    helper_proc(proc, 47, "fusermount", "fusermount")
    asked = []
    sweep_of(proc, manager_pid_fn=lambda uid: asked.append(uid))
    assert asked == []
    helper_proc(proc, MAIN_PID, "systemd", "systemd")
    helper_proc(proc, 48, "(sd-pam)", "(sd-pam)", ppid=MAIN_PID)
    sweep_of(proc, manager_pid_fn=lambda uid: asked.append(uid) or MAIN_PID)
    assert asked == [os.getuid()]


def test_an_unreadable_or_empty_cmdline_never_matches_the_allowlist(tmp_path):
    proc = tmp_path.resolve() / "proc"
    helper_proc(proc, 51, "fusermount", "fusermount", cmdline=None)
    sweep = sweep_of(proc)
    assert sweep.helpers_skipped == 0 and "pid=51" in sweep.incomplete and "cmdline unreadable" in sweep.incomplete
    other = tmp_path.resolve() / "proc2"
    helper_proc(other, 52, "fusermount", "fusermount", cmdline="")
    sweep = sweep_of(other)
    assert sweep.helpers_skipped == 0 and "pid=52" in sweep.incomplete and "empty cmdline" in sweep.incomplete


def test_a_readable_helper_is_swept_like_any_process_and_not_counted(root, tmp_path):
    proc = tmp_path.resolve() / "proc"
    candidate = make_tree(root)
    entry = fake_proc(proc, 53, cwd=candidate / "sub")
    (entry / "status").write_text(f"Name:\tgpg-agent\nPPid:\t1\nUid:\t{os.getuid()}\t0\t0\t0\n", encoding="utf-8")
    (entry / "cmdline").write_text("/usr/bin/gpg-agent\0", encoding="utf-8")
    sweep = sweep_of(proc)
    assert (sweep.incomplete, sweep.helpers_skipped) == (None, 0)
    assert judge(candidate, sweep=sweep).slug == liveness.HELD


def test_an_unreadable_helper_does_not_hide_the_holds_that_were_readable(root, tmp_path):
    proc = tmp_path.resolve() / "proc"
    candidate = make_tree(root)
    entry = helper_proc(proc, 54, "ssh-agent", "ssh-agent")
    (entry / "fd").mkdir()
    (entry / "fd" / "3").symlink_to(candidate / "sub" / "file.txt")
    sweep = sweep_of(proc)
    assert sweep.helpers_skipped == 1 and sweep.holder_of(str(candidate)) == "fd"


@pytest.mark.parametrize(("cmdline", "expected"), [
    ("/usr/lib/systemd/systemd\0--user\0", ("/usr/lib/systemd/systemd", "systemd")),
    ("sshd: user@pts/28", ("sshd: user@pts/28", "sshd")),
    ("sshd: user@pts/33,pts/39,pts/40\0\0", ("sshd: user@pts/33,pts/39,pts/40", "sshd")),
    ("(sd-pam)\0", ("(sd-pam)", "(sd-pam)")),
    ("/usr/bin/gpg-agent --supervised", ("/usr/bin/gpg-agent --supervised", "gpg-agent")),
    ("fusermount\0-u\0/mnt/x", ("fusermount", "fusermount")),
    ("", None),
    ("\0\0", None),
    (": rewritten", None),
])
def test_the_argv0_token_is_cut_at_the_first_colon_or_space_then_reduced_to_its_basename(cmdline, expected):
    assert liveness._argv0(cmdline) == expected


class Done:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout = returncode, stdout


@pytest.mark.parametrize(("outcome", "expected"), [
    pytest.param(Done(0, "MainPID=4859\n"), 4859, id="running"),
    pytest.param(Done(0, "MainPID=0\n"), None, id="stopped-unit"),
    pytest.param(Done(1, "MainPID=4859\n"), None, id="non-zero-exit"),
    pytest.param(Done(0, "MainPID=abc\n"), None, id="not-a-number"),
    pytest.param(Done(0, "Other=1\n"), None, id="no-line"),
    pytest.param(subprocess.TimeoutExpired("systemctl", 5), None, id="timeout"),
    pytest.param(FileNotFoundError("systemctl"), None, id="no-systemctl"),
])
def test_the_manager_pid_query_answers_only_a_running_unit_and_any_failure_means_none(monkeypatch, outcome, expected):
    calls = []

    def fake_run(command, **kw):
        calls.append((command, kw))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(liveness.subprocess, "run", fake_run)
    assert liveness.user_manager_main_pid(1000) == expected
    command, kw = calls[0]
    assert command == ["systemctl", "show", "-p", "MainPID", "user@1000.service"]
    assert kw["timeout"] == liveness.MANAGER_QUERY_TIMEOUT_S and set(kw["env"]) == {"PATH", "LC_ALL"}


# ── vanished processes and entries ────────────────────────────────────────

def test_a_process_whose_status_disappears_between_listing_and_reading_leaves_the_sweep_complete(tmp_path):
    proc = tmp_path.resolve() / "proc"
    fake_proc(proc, 61)
    fake_proc(proc, 62)

    def status(path):
        if "/62/" in path:
            raise ProcessLookupError(3, "No such process", path)
        return Path(path).read_text(encoding="utf-8")

    sweep = liveness.sweep_holders(str(proc), status_fn=status)
    assert sweep.incomplete is None and sweep.foreign_skipped == 0


def test_a_same_uid_zombie_whose_cwd_and_exe_are_gone_but_whose_status_remains_is_not_incomplete(tmp_path):
    proc = tmp_path.resolve() / "proc"
    entry = proc / "63"
    entry.mkdir(parents=True)
    write_status(entry, os.getuid())
    sweep = sweep_of(proc)
    assert (sweep.incomplete, sweep.helpers_skipped) == (None, 0)


def test_a_vanished_fd_skips_only_that_entry_and_a_later_fd_still_holds_the_candidate(root, tmp_path):
    proc = tmp_path.resolve() / "proc"
    candidate = make_tree(root)
    fake_proc(proc, 64, fds=["/nonexistent/a", "/nonexistent/b", candidate / "sub" / "file.txt"])

    def readlink(path):
        if path.endswith("/fd/0"):
            raise FileNotFoundError(2, "No such file or directory", path)
        return os.readlink(path)

    sweep = liveness.sweep_holders(str(proc), readlink_fn=readlink)
    assert sweep.incomplete is None and sweep.holder_of(str(candidate)) == "fd"
    assert judge(candidate, sweep=sweep).slug == liveness.HELD


def test_a_permission_error_on_a_single_fd_of_a_live_process_is_still_incomplete(tmp_path):
    proc = tmp_path.resolve() / "proc"
    fake_proc(proc, 65, fds=["/nonexistent/a"])

    def readlink(path):
        raise PermissionError(13, "Permission denied", path)

    sweep = liveness.sweep_holders(str(proc), readlink_fn=readlink, cmdline_fn=lambda path: "x\0")
    assert sweep.incomplete and "pid=65" in sweep.incomplete and "(EACCES)" in sweep.incomplete


def test_a_status_that_is_denied_is_incomplete_with_its_errno_and_a_gone_status_is_not(tmp_path):
    proc = tmp_path.resolve() / "proc"
    fake_proc(proc, 66)
    for err, incomplete in ((PermissionError(13, "denied"), True), (PermissionError(1, "not permitted"), True),
                            (FileNotFoundError(2, "gone"), False)):
        def status(path, err=err):
            raise err

        sweep = liveness.sweep_holders(str(proc), status_fn=status)
        assert bool(sweep.incomplete) is incomplete
        if incomplete:
            assert "pid=66" in sweep.incomplete and "errno=" in sweep.incomplete


def test_an_incomplete_sweep_keeps_every_candidate_with_its_own_slug(root):
    sweep = liveness.HolderSweep(incomplete="/proc/9/cwd: PermissionError")
    for name in ("tmp.a", "tmp.b"):
        result = judge(make_tree(root, name), sweep=sweep)
        assert (result.action, result.slug) == (KEEP, liveness.HOLDER_SWEEP_INCOMPLETE)


def test_a_held_candidate_keeps_its_held_slug_even_when_the_sweep_is_incomplete(root, tmp_path):
    proc = tmp_path.resolve() / "proc"
    candidate = make_tree(root)
    fake_proc(proc, 11, cwd=candidate)
    sweep = liveness.sweep_holders(str(proc))
    sweep.incomplete = "something else"
    assert judge(candidate, sweep=sweep).slug == liveness.HELD


# ── the /proc sweep: real processes ───────────────────────────────────────

def test_a_process_with_its_cwd_inside_the_tree_is_found(root, procs):
    candidate = make_tree(root)
    start_python(procs, "import time; print('ready', flush=True); time.sleep(60)", cwd=candidate / "sub")
    sweep = liveness.sweep_holders()
    assert sweep.holder_of(str(candidate)) == "cwd"
    assert judge(candidate, sweep=sweep).slug == liveness.HELD


def test_a_process_running_an_executable_inside_the_tree_is_found(root, procs):
    candidate = make_tree(root)
    binary = candidate / "sleepcopy"
    shutil.copy2(shutil.which("sleep"), binary)
    proc = subprocess.Popen([str(binary), "60"], cwd=root.parent)
    procs.append(proc)
    sweep = liveness.sweep_holders()
    assert str(binary) in sweep.held["exe"]
    assert sweep.holder_of(str(candidate)) is not None
    assert judge(candidate, sweep=sweep).slug == liveness.HELD


def test_a_process_with_an_open_file_inside_the_tree_is_found(root, procs):
    candidate = make_tree(root)
    target = candidate / "sub" / "file.txt"
    start_python(procs, f"import time; f = open({str(target)!r}); print('ready', flush=True); time.sleep(60)", cwd=root.parent)
    sweep = liveness.sweep_holders()
    assert str(target) in sweep.held["fd"]
    assert judge(candidate, sweep=sweep).slug == liveness.HELD


def test_a_process_with_a_mapped_file_inside_the_tree_is_found_under_the_maps_kind(root, procs):
    candidate = make_tree(root)
    target = candidate / "sub" / "file.txt"
    code = (
        f"import mmap, time; f = open({str(target)!r}, 'r+b'); m = mmap.mmap(f.fileno(), 0); f.close();"
        " print('ready', flush=True); time.sleep(60)"
    )
    start_python(procs, code, cwd=root.parent)
    sweep = liveness.sweep_holders()
    assert str(target) in sweep.held["maps"]
    assert judge(candidate, sweep=sweep).slug == liveness.HELD


# ── mountinfo and deletion ────────────────────────────────────────────────

def test_mount_points_are_read_with_octal_escapes_decoded(tmp_path):
    info = tmp_path / "mountinfo"
    info.write_text(
        "36 35 98:0 / /mnt/with\\040space rw - ext3 /dev/root rw\n37 35 0:5 / /plain rw - tmpfs t rw\n",
        encoding="utf-8",
    )
    assert liveness.read_mount_points(str(info)) == {"/mnt/with space", "/plain"}


def test_mounted_inside_is_component_wise():
    mounts = frozenset({"/a/tmp.x/mnt", "/a/tmp.y"})
    assert liveness.mounted_inside("/a/tmp.x", mounts)
    assert liveness.mounted_inside("/a/tmp.y", mounts)
    assert not liveness.mounted_inside("/a/tmp", mounts)


def test_remove_tree_deletes_a_tree_including_unwritable_directories(root):
    candidate = make_tree(root)
    os.chmod(candidate / "sub", 0o500)
    liveness.remove_tree(str(root), candidate.name)
    assert not candidate.exists()


def test_remove_tree_does_not_follow_a_symlink_swapped_in_for_the_name(root, tmp_path):
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious", encoding="utf-8")
    (root / "tmp.swapped").symlink_to(outside)
    with pytest.raises(OSError):
        liveness.remove_tree(str(root), "tmp.swapped")
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "precious"
    assert stat.S_ISLNK(os.lstat(root / "tmp.swapped").st_mode)


def test_remove_tree_of_a_missing_name_is_a_no_op(root):
    liveness.remove_tree(str(root), "tmp.gone")
