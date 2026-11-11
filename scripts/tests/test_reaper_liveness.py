"""Tests for the proof of death in scripts/reaper/liveness.py.

Hermetic: every tree is built under tmp_path; time is a ``now`` argument (a tree's ctime cannot
be backdated), a foreign uid, a foreign ``st_dev`` and unreadable /proc entries are injected
or planted in a fake proc root. The four holder kinds are also proven against real processes
started here, asserted on the sweep's own findings (not on the machine's /proc being clean).
"""
from __future__ import annotations

import os
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


def test_a_process_of_this_uid_by_its_status_is_swept_whoever_owns_its_proc_entry(root, tmp_path):
    proc = tmp_path.resolve() / "proc"
    candidate = make_tree(root)
    fake_proc(proc, 23, cwd=candidate / "sub", uid=1000)
    sweep = liveness.sweep_holders(str(proc), uid=1000)
    assert sweep.foreign_skipped == 0 and sweep.holder_of(str(candidate)) == "cwd"


def test_any_of_the_four_uids_in_the_status_line_counts_as_this_uid(tmp_path):
    for number, line in enumerate((
        "Uid:\t1000\t1000\t1000\t1000", "Uid:\t0\t0\t0\t1000", "Uid:\t0\t1000\t0\t0", "Uid:\t0\t0\t1000\t0",
    )):
        proc = tmp_path.resolve() / f"proc{number}"
        entry = proc / "24"
        (entry / "cwd").mkdir(parents=True)
        (entry / "status").write_text(f"Name:\tx\n{line}\n", encoding="utf-8")
        sweep = liveness.sweep_holders(str(proc), uid=1000)
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
