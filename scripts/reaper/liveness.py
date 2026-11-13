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
  incomplete, and an incomplete sweep keeps every candidate — except a *known system helper*
  (``HELPER_PAIRS`` plus the user's systemd manager and its ``(sd-pam)``), skipped and counted
  in ``helpers_skipped``. Identity is an exact pair: the status ``Name`` AND the argv0 token of
  ``cmdline`` (cut at the first ``:`` or space, then its basename); ``systemd`` must also be
  MainPID of ``user@<uid>.service`` and ``(sd-pam)`` its child; an unreadable or empty
  ``cmdline`` never matches. A readable helper is swept like any process. A process whose
  ``/proc/<pid>`` or status is gone (ENOENT/ESRCH) holds nothing and is not an error; an entry
  (cwd, exe, one fd or map) that vanished while its process is still listed skips only itself;
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

``path_holders`` answers the same question for a caller that decides alone: per path ``free``, ``held``
(a live scope record, an unresolved agentctl engine task, or a process) or ``unknown`` (a source could
not be read in full). It compares normalized path strings only and never touches the filesystem under
a queried path.
"""
from __future__ import annotations

import bisect
import dataclasses
import errno
import functools
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Callable

from lib import config_root
from reaper.contract import KEEP, REMOVE, ReapContext, owning_record

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

FREE_STATUS = "free"
HELD_STATUS = "held"
UNKNOWN_STATUS = "unknown"
KIND_SCOPE = "session-scope"
KIND_ENGINE = "engine-task"
KIND_PROCESS = "process"
RESOLVED_NODE = "RESOLVED"
STATE_FILE_NAME = re.compile(r"[0-9A-Za-z_-]+\.json")

HOLDER_KINDS = ("cwd", "exe", "fd", "maps")
_VANISHED = (errno.ENOENT, errno.ESRCH)

MANAGER = ("systemd", "systemd")
SD_PAM = ("(sd-pam)", "(sd-pam)")
HELPER_PAIRS = frozenset({
    ("sshd", "sshd"), ("sshd-session", "sshd-session"), ("gpg-agent", "gpg-agent"),
    ("ssh-agent", "ssh-agent"), ("fusermount", "fusermount"), ("fusermount3", "fusermount3"),
})
MANAGER_QUERY_TIMEOUT_S = 5


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
    helpers_skipped: int = 0
    incomplete: "str | None" = None

    def holder_of(self, real: str) -> "str | None":
        """The kind of the first hold at or inside ``real``, else None.

        The held paths are spelled as ``/proc`` reports them (symlink-free), so ``real`` must be too:
        a caller with a symlinked spelling resolves it first."""
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


Failures = "list[tuple[str, OSError]]"


def _readlink(path: str, failures: Failures, readlink_fn: "Callable[[str], str]") -> "str | None":
    try:
        return readlink_fn(path)
    except OSError as exc:
        if exc.errno not in _VANISHED:
            failures.append((path, exc))
        return None


def _absolute(target: "str | None") -> "str | None":
    if target is None or not target.startswith("/"):
        return None
    return target.removesuffix(DELETED_SUFFIX)


def _fd_targets(pid_dir: str, failures: Failures, readlink_fn: "Callable[[str], str]") -> "list[str]":
    try:
        names = os.listdir(os.path.join(pid_dir, "fd"))
    except OSError as exc:
        if exc.errno not in _VANISHED:
            failures.append((os.path.join(pid_dir, "fd"), exc))
        return []
    found = []
    for name in sorted(names):
        target = _absolute(_readlink(os.path.join(pid_dir, "fd", name), failures, readlink_fn))
        if target:
            found.append(target)
    return found


def _map_paths(pid_dir: str, failures: Failures) -> "list[str]":
    try:
        with open(os.path.join(pid_dir, "maps"), encoding="utf-8", errors="surrogateescape") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        if exc.errno not in _VANISHED:
            failures.append((os.path.join(pid_dir, "maps"), exc))
        return []
    found = set()
    for line in lines:
        fields = line.split(None, 5)
        if len(fields) == 6:
            target = _absolute(fields[5])
            if target:
                found.add(target)
    return sorted(found)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="surrogateescape") as handle:
        return handle.read()


@dataclasses.dataclass(frozen=True)
class StatusFacts:
    """What the sweep needs from a ``/proc/<pid>/status`` text: the four uids, ``Name`` and ``PPid``."""

    uids: "frozenset[int] | None"
    name: "str | None"
    ppid: "int | None"


def _parse_status(text: str) -> StatusFacts:
    uids = name = ppid = None
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key == "Uid" and uids is None:
            try:
                uids = frozenset(int(field) for field in rest.split())
            except ValueError:
                uids = None
        elif key == "Name" and name is None:
            name = rest.strip()
        elif key == "PPid" and ppid is None:
            try:
                ppid = int(rest.strip())
            except ValueError:
                ppid = None
    return StatusFacts(uids, name, ppid)


def _argv0(cmdline: str) -> "tuple[str, str] | None":
    """(raw first argument, its identity token) of a ``/proc/<pid>/cmdline`` text, or None when empty.

    The token is the raw argument cut at its first ``:`` or space (a daemon that rewrites its title
    to ``sshd: user@pts/0``) and then reduced to its basename."""
    raw = cmdline.split("\0", 1)[0]
    cut = min((i for i in (raw.find(":"), raw.find(" ")) if i >= 0), default=len(raw))
    token = os.path.basename(raw[:cut])
    return (raw, token) if token else None


def _errno_text(exc: "OSError | None") -> str:
    if exc is None or exc.errno is None:
        return "errno=none"
    return f"errno={exc.errno} ({errno.errorcode.get(exc.errno, '?')})"


def _cause(pid: str, name: "str | None", argv0: "str | None", where: str, exc: "OSError | None") -> str:
    return f"pid={pid} name={name!r} argv0={argv0!r} {_errno_text(exc)}: {where}"


def user_manager_main_pid(uid: int) -> "int | None":
    """MainPID of ``user@<uid>.service`` (the user's systemd manager) from ``systemctl show``, else
    None on any failure, timeout or a stopped unit (MainPID=0)."""
    try:
        done = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", f"user@{uid}.service"], capture_output=True, text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}, timeout=MANAGER_QUERY_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    for line in done.stdout.splitlines():
        if line.startswith("MainPID="):
            try:
                main = int(line.partition("=")[2])
            except ValueError:
                return None
            return main if main > 0 else None
    return None


@dataclasses.dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    name: "str | None"
    ppid: "int | None"
    argv0: "str | None"


def is_known_helper(proc: ProcessIdentity, manager_main_pid: "Callable[[], int | None]") -> bool:
    """True when ``proc`` matches one pair of the closed allowlist (see the module docstring).

    Both its status ``Name`` and its argv0 token must equal the pair. The user's systemd manager
    must also be MainPID of ``user@<uid>.service`` and its ``(sd-pam)`` child must have that pid as
    parent; if MainPID cannot be read, neither matches."""
    pair = (proc.name, proc.argv0)
    if pair == MANAGER:
        main = manager_main_pid()
        return main is not None and proc.pid == main
    if pair == SD_PAM:
        main = manager_main_pid()
        return main is not None and proc.ppid == main
    return pair in HELPER_PAIRS


def sweep_holders(
    proc_root: str = PROC_ROOT,
    uid: "int | None" = None,
    status_fn: "Callable[[str], str]" = _read_text,
    readlink_fn: "Callable[[str], str]" = os.readlink,
    cmdline_fn: "Callable[[str], str]" = _read_text,
    manager_pid_fn: "Callable[[int], int | None]" = user_manager_main_pid,
) -> HolderSweep:
    """One pass over ``proc_root``: what every process that has ``uid`` (default: this uid) holds.

    A process is read through its ``status`` file's ``Uid:`` line, never through the owner of
    ``/proc/<pid>`` (the kernel shows root there for a non-dumpable process). A process whose
    directory or status is gone holds nothing; a link, fd or map entry that vanished while the
    process is still listed (a zombie, an exiting process) skips only that entry. A same-uid process
    with an unreadable link is skipped and counted only when it is a known helper, else the sweep
    is incomplete.
    """
    uid = os.getuid() if uid is None else uid
    sweep = HolderSweep()
    try:
        entries = [e for e in os.listdir(proc_root) if e.isdigit()]
    except OSError as exc:
        sweep.mark_incomplete(f"{proc_root}: {type(exc).__name__}: {exc.strerror or exc}")
        return sweep
    manager: "list[int | None]" = []

    def manager_main_pid() -> "int | None":
        if not manager:
            manager.append(manager_pid_fn(uid))
        return manager[0]

    held: "dict[str, set[str]]" = {kind: set() for kind in HOLDER_KINDS}
    for entry in entries:
        pid_dir = os.path.join(proc_root, entry)
        try:
            facts = _parse_status(status_fn(os.path.join(pid_dir, "status")))
        except OSError as exc:
            if exc.errno not in _VANISHED:
                sweep.mark_incomplete(_cause(entry, None, None, f"{pid_dir}/status: owner unreadable", exc))
            continue
        if facts.uids is None:
            sweep.mark_incomplete(_cause(entry, facts.name, None, f"{pid_dir}/status: owner unreadable: no Uid line", None))
            continue
        if uid not in facts.uids:
            sweep.foreign_skipped += 1
            continue
        failures: Failures = []
        for kind in ("cwd", "exe"):
            target = _absolute(_readlink(os.path.join(pid_dir, kind), failures, readlink_fn))
            if target:
                held[kind].add(target)
        held["fd"].update(_fd_targets(pid_dir, failures, readlink_fn))
        held["maps"].update(_map_paths(pid_dir, failures))
        if failures:
            _judge_unreadable(sweep, entry, pid_dir, facts, failures, cmdline_fn, manager_main_pid)
    sweep.held = {kind: sorted(paths) for kind, paths in held.items()}
    return sweep


def _judge_unreadable(
    sweep: HolderSweep, entry: str, pid_dir: str, facts: StatusFacts, failures: Failures,
    cmdline_fn: "Callable[[str], str]", manager_main_pid: "Callable[[], int | None]",
) -> None:
    """A same-uid process with an unreadable entry is a counted known helper, else an incomplete sweep.

    An unreadable or empty ``cmdline`` identifies nothing, so it never matches the allowlist."""
    argv0: "tuple[str, str] | None" = None
    cmdline_error: "OSError | None" = None
    try:
        argv0 = _argv0(cmdline_fn(os.path.join(pid_dir, "cmdline")))
    except OSError as exc:
        cmdline_error = exc
    if argv0 is not None and is_known_helper(ProcessIdentity(int(entry), facts.name, facts.ppid, argv0[1]), manager_main_pid):
        sweep.helpers_skipped += 1
        return
    where, error = failures[0]
    shown = argv0[0] if argv0 else (f"<cmdline unreadable: {_errno_text(cmdline_error)}>" if cmdline_error else "<empty cmdline>")
    sweep.mark_incomplete(_cause(entry, facts.name, shown, where, error))


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


@dataclasses.dataclass(frozen=True)
class HolderAnswer:
    """Who holds one queried path: ``free``, ``held`` by ``kind`` (``session-scope``, ``engine-task`` or
    ``process``) with ``detail``, or ``unknown`` with the ``detail`` naming the source that could not be read."""

    status: str
    kind: str = ""
    detail: str = ""

    def report(self) -> str:
        if self.status == HELD_STATUS:
            return f"{self.kind}: {self.detail}"
        return self.detail


def _lexical(path: str) -> str:
    """Absolute, normalized spelling of ``path`` by string rules alone: no stat, no symlink resolved."""
    return os.path.normpath(os.path.abspath(path))


def _inside(container: str, candidate: str) -> bool:
    return candidate == container or candidate.startswith(container.rstrip(os.sep) + os.sep)


@dataclasses.dataclass(frozen=True)
class EngineTask:
    session_id: str
    task_id: str
    roots: "tuple[str, ...]"


def read_engine_tasks(state_dir: "Path | None" = None) -> "tuple[list[EngineTask], str | None]":
    """Every agentctl session state whose node is not RESOLVED, and why the store could not be read in full.

    Reads ``<state dir>/<session>.json`` as plain JSON (the store's own format), never importing the
    engine. A missing store holds nothing, and so does a zero-byte state file (it records no task); an
    unreadable directory, or a non-empty state file that cannot be read or parsed, is returned as the error (the first cause and a count) beside whatever else was readable."""
    root = config_root.agentctl_state_dir() if state_dir is None else state_dir
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], f"engine store {root}: {type(exc).__name__}: {exc.strerror or exc}"
    tasks: "list[EngineTask]" = []
    problems: "list[str]" = []
    for name in names:
        if not STATE_FILE_NAME.fullmatch(name):
            continue
        path = os.path.join(root, name)
        try:
            text = _read_text(path)
            if not text:
                continue
            data = json.loads(text)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            problems.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        node = data.get("node") if isinstance(data, dict) else None
        if not isinstance(node, str):
            problems.append(f"{path}: no node")
            continue
        if node == RESOLVED_NODE:
            continue
        roots = tuple(
            _lexical(value) for key in ("repo_root", "delivery_worktree")
            if isinstance(value := data.get(key), str) and value
        )
        tasks.append(EngineTask(name[: -len(".json")], str(data.get("task_id") or ""), roots))
    error = f"engine store: {problems[0]}" + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else "") if problems else None
    return tasks, error


def path_holders(
    paths: "list[str]",
    ctx: ReapContext,
    *,
    sweep: "HolderSweep | None" = None,
    state_dir: "Path | None" = None,
) -> "dict[str, HolderAnswer]":
    """For each queried path: ``free``, ``held`` or ``unknown``, from three sources, read-only.

    * a live session-scope record (``ctx.scope_records``, the ownership rule of ``contract.owning_record``)
      whose cwd or repo_root is at or inside the path;
    * an agentctl session state whose node is not RESOLVED and whose repo_root or delivery_worktree is at
      or inside it (``engine-task``: session id and task id);
    * a process whose cwd, exe, an open fd or a mapped file is at or inside it (one ``sweep_holders`` call).

    A path is ``held`` when any source holds it (the first in the order above names it). Otherwise it is
    ``unknown`` when a source could not be read in full — a scope registry that failed to load, an engine
    store that could not be read, an incomplete process sweep — and ``free`` only when all three were read.
    A queried path is matched by its normalized absolute spelling and by its symlink-resolved one (the
    path itself is resolved, nothing under it is listed or walked). A holder's recorded path is resolved
    too, except one that lexically sits inside any queried path: that one already matches, and resolving
    it would stat inside a queried tree (a dead mount must not hang the query). ``sweep`` and
    ``state_dir`` let a caller (a test) supply the process sweep and the engine store directory.
    """
    sweep = sweep_holders() if sweep is None else sweep
    tasks, engine_error = read_engine_tasks(state_dir)
    unreadable = [
        why for why in (
            f"scope registry: {ctx.scope_registry_error}" if ctx.scope_registry_error else None,
            engine_error,
            f"process sweep: {sweep.incomplete}" if sweep.incomplete else None,
        ) if why
    ]
    answers: "dict[str, HolderAnswer]" = {}
    queried = [_lexical(raw) for raw in paths]
    resolve = functools.lru_cache(maxsize=None)(os.path.realpath)

    def resolve_outside(root: str) -> str:
        return root if any(_inside(path, root) for path in queried) else resolve(root)

    for raw in paths:
        wanted = _lexical(raw)
        if wanted == os.sep:
            answers[raw] = HolderAnswer(UNKNOWN_STATUS, detail="the filesystem root is not a queryable path")
            continue
        real = resolve(wanted)
        spellings = [wanted] if real == wanted else [wanted, real]
        record = next(
            (found for spelling in spellings
             if (found := owning_record(spelling, ctx.scope_records, ctx.now, ctx.heartbeat_ttl_hours, resolve_outside))),
            None,
        )
        task = next((t for t in tasks if any(_inside(s, resolve_outside(root)) for s in spellings for root in t.roots)), None)
        process = next((kind for s in spellings if (kind := sweep.holder_of(s))), None)
        if record is not None:
            answers[raw] = HolderAnswer(HELD_STATUS, KIND_SCOPE, f"session={record.session_id} cwd={record.cwd or record.repo_root}")
        elif task is not None:
            answers[raw] = HolderAnswer(HELD_STATUS, KIND_ENGINE, f"session={task.session_id} task={task.task_id}")
        elif process is not None:
            answers[raw] = HolderAnswer(HELD_STATUS, KIND_PROCESS, process)
        elif unreadable:
            answers[raw] = HolderAnswer(UNKNOWN_STATUS, detail="; ".join(unreadable))
        else:
            answers[raw] = HolderAnswer(FREE_STATUS)
    return answers


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
