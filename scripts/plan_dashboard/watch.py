"""inotify watches over the directories the dashboard's sources live in (Linux, ``ctypes`` only).

One non-blocking descriptor, no thread: the app asks ``Watcher.pending()`` at each tick and, for a cycle, takes
the events with ``Watcher.drain()``. A watch covers one directory, so every existing directory a source
pattern can descend into is watched (not only the ones that hold a source now), and a directory created
below them is watched before it is listed. Anything the kernel cannot promise (queue overflow, a watched
directory removed or moved, a watch that cannot be added) is reported so the caller falls back to a
check of every source or to the stat path.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import math
import os
import select
import struct
from dataclasses import dataclass, field
from pathlib import Path

IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_NONBLOCK = os.O_NONBLOCK
IN_CLOEXEC = os.O_CLOEXEC

WATCH_MASK = (IN_MODIFY | IN_ATTRIB | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
              | IN_DELETE_SELF | IN_MOVE_SELF)
NAMES_CHANGE = IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
GONE = IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED
HEADER = struct.Struct("iIII")
READ_SIZE = 64 * 1024
WATCH_ENV = "PLAN_DASHBOARD_WATCH"
TICK_S = 0.5
RECONCILE_S = 30.0


class Unavailable(OSError):
    """inotify cannot be used for this run: it is absent or a watch could not be added. ``reason`` says why."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Source:
    """Where files of one kind live: ``depth`` 0 in ``root`` itself, 1 in a directory directly below it.
    ``suffix`` keeps the files named ``*<suffix>``; ``name`` keeps only the file of that name (a directory
    shared with other files)."""

    kind: str
    root: Path
    depth: int = 0
    suffix: str | None = None
    name: str | None = None

    def accepts(self, path) -> bool:
        base = os.path.basename(path)
        if self.name is not None:
            return base == self.name
        return self.suffix is None or base.endswith(self.suffix)

    def covers(self, path) -> bool:
        """Whether ``path`` is a file of this source or a directory its pattern can descend into."""
        try:
            parts = Path(path).relative_to(self.root).parts
        except ValueError:
            return False
        return len(parts) <= self.depth or (len(parts) == self.depth + 1 and self.accepts(path))


def sources_for(state_root, binding_dir, transcript_roots, cost_log, listing_file=None) -> list[Source]:
    """The watch roots and patterns of the dashboard's readers: state ``*.json``, bindings ``*.json``,
    transcripts ``*/*.jsonl`` (and so ``*/{name}.jsonl``) under each projects root, the cost ledger's directory
    filtered to its name and, where the locator's listing is a file, that file's directory filtered to its name."""
    found = [Source("state", Path(state_root), 0, ".json"), Source("binding", Path(binding_dir), 0, ".json"),
             *(Source("transcript", Path(root), 1, ".jsonl") for root in transcript_roots)]
    if cost_log is not None:
        found.append(Source("ledger", Path(cost_log).parent, 0, name=Path(cost_log).name))
    if listing_file is not None:
        found.append(Source("locator_listing", Path(listing_file).parent, 0, name=Path(listing_file).name))
    return found


def _libc():
    path = ctypes.util.find_library("c")
    libc = ctypes.CDLL(path, use_errno=True)
    libc.inotify_init1.argtypes = [ctypes.c_int]
    libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
    return libc


class Inotify:
    """One inotify descriptor: ``add``, ``remove``, ``ready`` (events waiting, none consumed) and ``read``."""

    def __init__(self):
        try:
            self.libc = _libc()
            self.fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        except (OSError, AttributeError) as exc:
            raise Unavailable(f"inotify is not available: {exc}") from exc
        if self.fd < 0:
            number = ctypes.get_errno()
            raise Unavailable(f"inotify_init1 failed: {errno.errorcode.get(number, number)}")
        self._poll = select.poll()
        self._poll.register(self.fd, select.POLLIN)

    def add(self, path) -> int:
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number), str(path))
        return wd

    def remove(self, wd: int) -> None:
        self.libc.inotify_rm_watch(self.fd, wd)

    def ready(self) -> bool:
        return bool(self._poll.poll(0))

    def read(self) -> list[tuple[int, int, int, str]]:
        """Every record waiting, as ``(wd, mask, cookie, name)``."""
        records = []
        while True:
            try:
                data = os.read(self.fd, READ_SIZE)
            except BlockingIOError:
                return records
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise
            offset = 0
            while offset < len(data):
                wd, mask, cookie, length = HEADER.unpack_from(data, offset)
                offset += HEADER.size
                name = data[offset:offset + length].split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
                offset += length
                records.append((wd, mask, cookie, name))

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


@dataclass
class Drained:
    """What one ``drain`` found: the source files named by an event, the directories whose names changed (their
    listing must be read again), the directories newly watched, and whether the kernel lost track
    (``overflow``: the queue overflowed, or a watched directory was removed or moved)."""

    paths: set[str] = field(default_factory=set)
    directories: set[str] = field(default_factory=set)
    added: set[str] = field(default_factory=set)
    overflow: bool = False
    reasons: dict[str, str] = field(default_factory=dict)


class Watcher:
    def __init__(self, sources: list[Source], inotify: Inotify | None = None):
        self.sources = sources
        self.inotify = inotify if inotify is not None else Inotify()
        self.directories: dict[int, str] = {}
        self.watched: dict[str, int] = {}

    def reaches(self, directory) -> bool:
        """Whether a source pattern can descend into ``directory``."""
        return any(self._within(source, directory) for source in self.sources)

    @staticmethod
    def _within(source: Source, directory) -> bool:
        try:
            return len(Path(directory).relative_to(source.root).parts) <= source.depth
        except ValueError:
            return False

    def accepts(self, path) -> bool:
        return any(source.covers(path) and source.accepts(path) for source in self.sources)

    def covers(self, path) -> bool:
        return any(source.covers(path) for source in self.sources)

    def watch(self, directory) -> None:
        directory = str(directory)
        if directory in self.watched:
            return
        try:
            wd = self.inotify.add(directory)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                return
            raise Unavailable(f"inotify_add_watch {directory}: {errno.errorcode.get(exc.errno, exc.errno)}") from exc
        old = self.directories.get(wd)
        if old is not None and old != directory:
            self.watched.pop(old, None)
        self.directories[wd] = directory
        self.watched[directory] = wd

    def watch_tree(self, source: Source) -> None:
        for directory in directories_to_watch(source):
            self.watch(directory)

    def start(self) -> None:
        for source in self.sources:
            self.watch_tree(source)

    def restore(self) -> None:
        """Watch again every directory that lost its watch."""
        self.start()

    def pending(self) -> bool:
        return self.inotify.ready()

    def drain(self) -> Drained:
        """Take the events waiting and say what they name. A directory created below a watched one is watched
        before this returns, so a file written into it afterwards is named by an event."""
        found = Drained()
        for wd, mask, _cookie, name in self.inotify.read():
            if mask & IN_Q_OVERFLOW:
                found.overflow = True
                found.reasons["overflow"] = "the inotify queue overflowed"
                continue
            directory = self.directories.get(wd)
            if directory is None:
                found.overflow = True
                found.reasons["unknown watch"] = "an event named a watch this run does not know"
                continue
            if mask & GONE:
                self.directories.pop(wd, None)
                self.watched.pop(directory, None)
                found.overflow = True
                found.directories.add(os.path.dirname(directory))
                found.reasons[directory] = "a watched directory was removed or moved"
                continue
            if not name:
                found.paths.add(directory)
                continue
            path = os.path.join(directory, name)
            if mask & IN_ISDIR:
                found.directories.add(directory)
                if mask & (IN_CREATE | IN_MOVED_TO) and self.reaches(path):
                    adopt(self, path, found)
                continue
            if self.accepts(path):
                found.paths.add(path)
                if mask & NAMES_CHANGE:
                    found.directories.add(directory)
        return found

    def close(self) -> None:
        self.inotify.close()
        self.directories.clear()
        self.watched.clear()


def adopt(watcher: Watcher, directory: str, found: Drained) -> None:
    """Watch a new directory and every directory a pattern descends into below it, each before it is
    listed; what they already hold is found by listing them."""
    watcher.watch(directory)
    found.added.add(directory)
    found.directories.add(directory)
    for entry in _subdirectories(directory):
        if watcher.reaches(entry.path):
            adopt(watcher, entry.path, found)


def _subdirectories(directory) -> list[os.DirEntry]:
    try:
        with os.scandir(directory) as listing:
            return [entry for entry in listing if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def directories_to_watch(source: Source) -> list[Path]:
    """The source's root and, below it, every existing directory its pattern can descend into, whether or not
    it holds a source now: a file written into an empty one must still be named by an event."""
    found, level = [], [source.root] if source.root.is_dir() else []
    for depth in range(source.depth + 1):
        found += level
        if depth == source.depth:
            break
        level = [Path(entry.path) for directory in level for entry in _subdirectories(directory)]
    return found


def start(sources: list[Source]) -> Watcher:
    """A started ``Watcher``; ``Unavailable`` (with its reason) when inotify is absent or a watch cannot be added."""
    watcher = Watcher(sources)
    try:
        watcher.start()
    except Unavailable:
        watcher.close()
        raise
    return watcher


def choose_path(sources: list[Source], emit, enabled: bool = True) -> Watcher | None:
    """The watcher of the event path, or None for the stat path; writes ``detection`` once, after the last
    start-up watch is in (event) or at the reason the watches cannot be used (stat)."""
    if not enabled:
        emit("detection", path="stat", reason=f"{WATCH_ENV}=off")
        return None
    try:
        watcher = start(sources)
    except Unavailable as exc:
        emit("detection", path="stat", reason=exc.reason)
        return None
    emit("detection", path="event", reason="every watch is in")
    return watcher


def intake(watcher: Watcher, kept) -> Drained:
    """Take the events waiting and mark in ``kept`` what they name; nothing else is read again."""
    found = watcher.drain()
    kept.mark(found.paths, found.directories, by="event")
    return found


def begin_cycle(watcher: Watcher, kept, stamp) -> Drained:
    """An event-path cycle: ``stamp`` (its ``cycle_start``) first, then the descriptor, so a write made before
    the stamp is in this cycle."""
    stamp()
    return intake(watcher, kept)


def needs_sweep(found: Drained) -> bool:
    return found.overflow


def slice_entries(count: int, tick_s: float = TICK_S) -> int:
    """The share of a ``count``-entry read set one tick checks so a revolution takes ``RECONCILE_S``."""
    return math.ceil(count * tick_s / RECONCILE_S)


@dataclass
class SliceResult:
    entries: int = 0
    found: int = 0
    truncated: bool = False


class Sweep:
    """A ring over everything the kept view holds: the rolling check, and the one-off full check that runs first
    (after the start-up watches or an overflow) and ends after one revolution."""

    def __init__(self, kept):
        self.kept = kept
        self.ring: list[tuple[str, str]] = []
        self.pos = 0
        self.one_off = False

    @property
    def reason(self) -> str:
        return "overflow-sweep" if self.one_off else "reconciliation"

    def begin_one_off(self) -> None:
        self.ring, self.pos, self.one_off = self.kept.sweep_items(), 0, True

    def size(self) -> int:
        return len(self.ring) if self.ring else len(self.kept.sweep_items())

    def next_item(self):
        if self.pos >= len(self.ring):
            finished, self.one_off = self.one_off, False
            self.ring, self.pos = self.kept.sweep_items(), 0
            if finished or not self.ring:
                return None
        item = self.ring[self.pos]
        self.pos += 1
        return item


def sweep_slice(sweep: Sweep, allowance: int | None, budget_s: float, mono, deadline: float | None = None) -> SliceResult:
    """Check up to ``allowance`` entries of the ring (all that fit when None), stopping at ``budget_s`` on ``mono``
    or at ``deadline``, whichever is earlier; a difference is only marked, for the next cycle to read."""
    limit = mono() + budget_s
    if deadline is not None:
        limit = min(limit, deadline)
    result = SliceResult()
    while allowance is None or result.entries < allowance:
        if mono() >= limit:
            result.truncated = True
            break
        item = sweep.next_item()
        if item is None:
            break
        result.entries += 1
        kind, key = item
        if sweep.kept.differs(item):
            result.found += 1
            sweep.kept.mark([key] if kind == "stat" else [], [key] if kind == "dir" else [], by=sweep.reason)
    return result
