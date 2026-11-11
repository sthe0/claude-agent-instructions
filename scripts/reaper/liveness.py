"""Proof of death for a temp directory: nothing holds it, nothing owns it, it is idle.

Difficulty removed: a cleaner of a shared temp directory may delete an entry only on
positive proof that nothing uses it, and each clause of that proof is easy to get subtly
wrong (a symlink read as its target, a walk that enters a mount, a /proc sweep that could not
look and reads as "no holder"). The proof lives here once, so every reaper of temp residue
reuses the same clauses and the same tests.

``judge_residue`` returns a ``Judgment``: ``remove`` only when ALL hold, otherwise ``keep`` with
a slug from the closed set below (the slug is the stable key; ``detail`` is free text). The
clauses are tested in this order and the first that fails decides the slug:

* ``not-a-directory`` — it is a real directory (lstat: a symlink or a file is kept);
* ``foreign-owner`` — owned by this uid;
* ``mount-inside`` — on the root's filesystem (``st_dev``);
* ``session-owned`` — no live session-scope record owns it (``owned`` callback);
* ``held`` — no process has its cwd, exe, an open fd or a mapped file at or inside it. One
  ``/proc`` sweep (``sweep_holders``) serves every candidate of a pass;
* ``holder-sweep-incomplete`` — the sweep could look at every process. A process is skipped
  (and counted) only when its ``Uid:`` line in ``/proc/<pid>/status`` — readable by anyone,
  unlike the links, and not subject to the dumpable-owner swap of ``/proc/<pid>`` itself —
  shows none of its uids is this one. A process of this uid whose cwd, exe, fd or maps cannot
  be read (a non-dumpable one, say), or whose status cannot be read, makes the sweep
  incomplete, and an incomplete sweep keeps every candidate. A process that has vanished (the
  entry or its link is gone, ENOENT) holds nothing and is not an error;
* ``os-error`` — also when mountinfo is unreadable;
* ``mount-inside`` — also when no mount point (mountinfo) is at or inside it; the walk never
  enters an entry on another ``st_dev``, so it never reaches a FUSE or network mount;
* ``nested-git`` — no ``.git`` anywhere in its tree, unless the caller's ``git_exemption``
  vouches for the ones found;
* ``idle-floor`` — the newest ``max(mtime, ctime)`` in its tree is older than the floor.

Any other ``OSError`` or exception while judging resolves to ``keep`` / ``os-error``. The walk
uses ``lstat`` only, never follows a symlink, and so never reads a time from outside the tree.

A paused session owns its directory while holding no process at all, so process checks cannot
prove death; the idle floor carries that case.
"""
from __future__ import annotations

import bisect
import dataclasses
import errno
import os
import shutil
import stat
from typing import Callable

from reaper.contract import KEEP, REMOVE

PROC_ROOT = "/proc"
MOUNTINFO = "/proc/self/mountinfo"
GIT_NAME = ".git"
BLOCK_BYTES = 512
DELETED_SUFFIX = " (deleted)"

NOT_A_DIRECTORY = "not-a-directory"
FOREIGN_OWNER = "foreign-owner"
MOUNT_INSIDE = "mount-inside"
SESSION_OWNED = "session-owned"
HELD = "held"
HOLDER_SWEEP_INCOMPLETE = "holder-sweep-incomplete"
NESTED_GIT = "nested-git"
IDLE_FLOOR = "idle-floor"
OS_ERROR = "os-error"

HOLDER_KINDS = ("cwd", "exe", "fd", "maps")
_VANISHED = (errno.ENOENT, errno.ESRCH)


@dataclasses.dataclass(frozen=True)
class Judgment:
    action: str
    slug: str
    detail: str
    size: int = 0


@dataclasses.dataclass
class HolderSweep:
    """Absolute paths the processes of this uid hold, by kind, plus what the sweep could not see."""

    held: "dict[str, list[str]]" = dataclasses.field(default_factory=lambda: {k: [] for k in HOLDER_KINDS})
    foreign_skipped: int = 0
    incomplete: "str | None" = None

    def holder_of(self, real: str) -> "str | None":
        """The kind of the first hold at or inside ``real`` (a resolved absolute path), else None."""
        prefix = real + os.sep
        for kind in HOLDER_KINDS:
            paths = self.held[kind]
            at = bisect.bisect_left(paths, real)
            if at < len(paths) and (paths[at] == real or paths[at].startswith(prefix)):
                return kind
            at = bisect.bisect_left(paths, prefix)
            if at < len(paths) and paths[at].startswith(prefix):
                return kind
        return None

    def mark_incomplete(self, why: str) -> None:
        if self.incomplete is None:
            self.incomplete = why


def _readlink(path: str, sweep: HolderSweep, what: str) -> "str | None":
    try:
        return os.readlink(path)
    except OSError as exc:
        if exc.errno not in _VANISHED:
            sweep.mark_incomplete(f"{what}: {type(exc).__name__}: {exc.strerror or exc}")
        return None


def _absolute(target: "str | None") -> "str | None":
    if target is None or not target.startswith("/"):
        return None
    return target.removesuffix(DELETED_SUFFIX)


def _fd_targets(pid_dir: str, sweep: HolderSweep) -> "list[str]":
    try:
        names = os.listdir(os.path.join(pid_dir, "fd"))
    except OSError as exc:
        if exc.errno not in _VANISHED:
            sweep.mark_incomplete(f"{pid_dir}/fd: {type(exc).__name__}: {exc.strerror or exc}")
        return []
    found = []
    for name in names:
        target = _absolute(_readlink(os.path.join(pid_dir, "fd", name), sweep, f"{pid_dir}/fd/{name}"))
        if target:
            found.append(target)
    return found


def _map_paths(pid_dir: str, sweep: HolderSweep) -> "list[str]":
    try:
        with open(os.path.join(pid_dir, "maps"), encoding="utf-8", errors="surrogateescape") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        if exc.errno not in _VANISHED:
            sweep.mark_incomplete(f"{pid_dir}/maps: {type(exc).__name__}: {exc.strerror or exc}")
        return []
    found = set()
    for line in lines:
        fields = line.split(None, 5)
        if len(fields) == 6:
            target = _absolute(fields[5])
            if target:
                found.add(target)
    return sorted(found)


def _read_status(path: str) -> str:
    with open(path, encoding="utf-8", errors="surrogateescape") as handle:
        return handle.read()


def _status_uids(text: str) -> "frozenset[int] | None":
    """The real, effective, saved and filesystem uids of a ``/proc/<pid>/status`` text, else None."""
    for line in text.splitlines():
        if line.startswith("Uid:"):
            try:
                return frozenset(int(field) for field in line.split()[1:])
            except ValueError:
                return None
    return None


def sweep_holders(
    proc_root: str = PROC_ROOT,
    uid: "int | None" = None,
    status_fn: "Callable[[str], str]" = _read_status,
) -> HolderSweep:
    """One pass over ``proc_root``: what every process that has ``uid`` (default: this uid) holds.

    A process is read through its ``status`` file's ``Uid:`` line, never through the owner of
    ``/proc/<pid>`` (the kernel shows root there for a non-dumpable process).
    """
    uid = os.getuid() if uid is None else uid
    sweep = HolderSweep()
    try:
        entries = [e for e in os.listdir(proc_root) if e.isdigit()]
    except OSError as exc:
        sweep.mark_incomplete(f"{proc_root}: {type(exc).__name__}: {exc.strerror or exc}")
        return sweep
    held: "dict[str, set[str]]" = {kind: set() for kind in HOLDER_KINDS}
    for entry in entries:
        pid_dir = os.path.join(proc_root, entry)
        try:
            owners = _status_uids(status_fn(os.path.join(pid_dir, "status")))
        except OSError as exc:
            if exc.errno not in _VANISHED:
                sweep.mark_incomplete(f"{pid_dir}/status: owner unreadable: {type(exc).__name__}: {exc.strerror or exc}")
            continue
        if owners is None:
            sweep.mark_incomplete(f"{pid_dir}/status: owner unreadable: no Uid line")
            continue
        if uid not in owners:
            sweep.foreign_skipped += 1
            continue
        for kind, target in (
            ("cwd", _absolute(_readlink(os.path.join(pid_dir, "cwd"), sweep, f"{pid_dir}/cwd"))),
            ("exe", _absolute(_readlink(os.path.join(pid_dir, "exe"), sweep, f"{pid_dir}/exe"))),
        ):
            if target:
                held[kind].add(target)
        held["fd"].update(_fd_targets(pid_dir, sweep))
        held["maps"].update(_map_paths(pid_dir, sweep))
    sweep.held = {kind: sorted(paths) for kind, paths in held.items()}
    return sweep


def read_mount_points(path: str = MOUNTINFO) -> "frozenset[str]":
    """Mount points from a mountinfo file (octal escapes such as ``\\040`` decoded). Raises OSError."""
    points = set()
    with open(path, encoding="utf-8", errors="surrogateescape") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) > 4:
                points.add(_unescape(fields[4]))
    return frozenset(points)


def _unescape(raw: str) -> str:
    out, at = [], 0
    while at < len(raw):
        if raw[at] == "\\" and raw[at + 1:at + 4].isdigit():
            out.append(chr(int(raw[at + 1:at + 4], 8)))
            at += 4
        else:
            out.append(raw[at])
            at += 1
    return "".join(out)


def mounted_inside(real: str, mounts: "frozenset[str]") -> bool:
    prefix = real + os.sep
    return any(point == real or point.startswith(prefix) for point in mounts)


@dataclasses.dataclass
class TreeFacts:
    newest: float
    size: int
    git_entries: "list[str]" = dataclasses.field(default_factory=list)
    crossed_device: bool = False


def walk_tree(
    top: str,
    top_stat: os.stat_result,
    root_dev: int,
    lstat_fn: "Callable[[str], os.stat_result]" = os.lstat,
    scandir_fn: "Callable[[str], object]" = os.scandir,
) -> TreeFacts:
    """Newest time, disk bytes, every ``.git`` entry and a foreign filesystem, over the tree of ``top``.

    Entries are read with ``lstat`` and a symlink is neither entered nor read through; an entry
    whose ``st_dev`` differs from ``root_dev`` is noted and not entered. Raises OSError.
    """
    facts = TreeFacts(max(top_stat.st_mtime, top_stat.st_ctime), top_stat.st_blocks * BLOCK_BYTES)
    pending = [top]
    while pending:
        current = pending.pop()
        with scandir_fn(current) as entries:  # type: ignore[attr-defined]
            for entry in entries:
                child = lstat_fn(entry.path)
                if child.st_dev != root_dev:
                    facts.crossed_device = True
                    continue
                facts.newest = max(facts.newest, child.st_mtime, child.st_ctime)
                facts.size += child.st_blocks * BLOCK_BYTES
                if entry.name == GIT_NAME:
                    facts.git_entries.append(entry.path)
                if stat.S_ISDIR(child.st_mode):
                    pending.append(entry.path)
    return facts


def judge_residue(
    path: str,
    *,
    root_dev: int,
    now: float,
    floor_s: float,
    sweep: HolderSweep,
    mounts: "frozenset[str] | None",
    owned: "Callable[[str], bool]",
    uid: "int | None" = None,
    lstat_fn: "Callable[[str], os.stat_result]" = os.lstat,
    scandir_fn: "Callable[[str], object]" = os.scandir,
    git_exemption: "Callable[[str, list[str]], bool] | None" = None,
) -> Judgment:
    """The proof of death for one candidate ``path`` (a resolved absolute path). See the module docstring.

    ``git_exemption(path, git_entries)`` may vouch that the ``.git`` entries found in the tree
    protect nothing; any exception it raises resolves to ``keep`` like every other.
    """
    uid = os.getuid() if uid is None else uid
    try:
        top = lstat_fn(path)
        if not stat.S_ISDIR(top.st_mode):
            return _keep(NOT_A_DIRECTORY, "not a real directory (a symlink or another file type)", top.st_blocks * BLOCK_BYTES)
        if top.st_uid != uid:
            return _keep(FOREIGN_OWNER, f"owned by uid {top.st_uid}, not {uid}")
        if top.st_dev != root_dev:
            return _keep(MOUNT_INSIDE, "on another filesystem than its root")
        facts = walk_tree(path, top, root_dev, lstat_fn, scandir_fn)
        if owned(path):
            return _keep(SESSION_OWNED, "a live session-scope record owns it", facts.size)
        holder = sweep.holder_of(path)
        if holder:
            return _keep(HELD, f"a process holds it ({holder})", facts.size)
        if sweep.incomplete:
            return _keep(HOLDER_SWEEP_INCOMPLETE, sweep.incomplete, facts.size)
        if mounts is None:
            return _keep(OS_ERROR, "mountinfo unreadable", facts.size)
        if facts.crossed_device or mounted_inside(path, mounts):
            return _keep(MOUNT_INSIDE, "a mount point is at or inside it", facts.size)
        if facts.git_entries and not (git_exemption is not None and git_exemption(path, facts.git_entries)):
            return _keep(NESTED_GIT, "a .git is inside it", facts.size)
        idle = now - facts.newest
        if idle < floor_s:
            return _keep(IDLE_FLOOR, f"idle {idle / 3600.0:.1f}h, under the {floor_s / 3600.0:.0f}h floor", facts.size)
        return Judgment(REMOVE, "removable", f"no holder, idle {idle / 3600.0:.0f}h", facts.size)
    except Exception as exc:
        return Judgment(KEEP, OS_ERROR, f"{type(exc).__name__}: {exc}")


def _keep(slug: str, detail: str, size: int = 0) -> Judgment:
    return Judgment(KEEP, slug, detail, size)


def make_writable(top: str) -> None:
    pending = [top]
    while pending:
        current = pending.pop()
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            continue
        if not stat.S_ISDIR(mode):
            continue
        try:
            os.chmod(current, stat.S_IMODE(mode) | 0o700)
            pending.extend(os.path.join(current, name) for name in os.listdir(current))
        except OSError:
            continue


def remove_tree(parent: str, name: str) -> None:
    """Delete ``parent/name`` through the parent's directory fd, so a symlink swapped in for
    ``name`` after the judgment is not followed. Retries once after making the tree writable."""
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            shutil.rmtree(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except PermissionError:
            make_writable(os.path.join(parent, name))
            try:
                shutil.rmtree(name, dir_fd=parent_fd)
            except FileNotFoundError:
                return
    finally:
        os.close(parent_fd)
