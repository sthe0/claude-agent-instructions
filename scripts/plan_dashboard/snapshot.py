"""The dashboard's snapshot on disk and the identity-keyed caches that make a refresh cheap.

Difficulty removed: the old code read every state file, scanned about nine thousand binding files twice and
re-parsed a ledger of tens of megabytes on each refresh, so the first rows came after seconds and the
marks after minutes. Here a source is read again only when its *identity* — device, inode, size, mtime and
ctime in nanoseconds — differs from the identity it was read under. A file whose mtime lies within one
resolution step (``MTIME_STEP_NS``) of its read — by the later of its mtime and ctime — is *unconfirmed*: a
second write in the same tick would leave its identity unchanged, so it is read again on the next cycle and
never written to the snapshot as recorded.

``SOURCE_KINDS`` are the kinds of file the dashboard reads; the locator's listing is not among them and is
asked again every cycle.

The snapshot is one JSON file, ``${XDG_CACHE_HOME:-$HOME/.cache}/plan-dashboard/snapshot.json``::

    {"version": 1, "code": code_version(), "body_sha256": sha256 of "sections", "sources_sha256": sha256 of
     "sources", "sections": {<selection>: section},
     "sources": [{"path", "kind", "dev", "ino", "size", "mtime_ns", "ctime_ns"}, ...]}

``sources`` lists the settled identities of the state, binding and ledger files and the watched transcripts as
of the last cycle; a section holds the rows and the overlay that screen showed and the values read from those
sources (state digests, binding contents, transcript tails, ledger sums), which a start seeds the caches
with. The file is written atomically at a clean exit and holds the one section of the selection last shown;
a missing, damaged, half-written, foreign-version or unreadable file is read as absent.

Every section also holds ``code_identities``: the identities of the code files and of the directories the import
resolution looked in when ``code`` was computed. A start whose stat of each finds them unchanged, and settled
when recorded, takes ``code`` as its own version without reading or parsing a module; anything else computes
the version in full (``code_check_report`` says which, for the event stream).
"""
from __future__ import annotations

import fnmatch
import functools
import hashlib
import json
import os
import stat as stat_module
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from lib import config_root, engine_session_binding

VERSION = 1
# Coarser than the kernel's timestamp tick (4-10 ms before Linux 6.13) and than coarse file systems
# (1-2 s): a write inside this window of a read may leave the identity as it was.
MTIME_STEP_NS = 2_000_000_000
CACHE_DIRNAME = "plan-dashboard"
FILE_NAME = "snapshot.json"
SOURCE_KINDS = ("state", "binding", "transcript", "ledger")
IDENTITY_KEYS = ("dev", "ino", "size", "mtime_ns", "ctime_ns")
SCRIPTS = Path(__file__).resolve().parent.parent
CODE_ROOTS = tuple(f"plan_dashboard.{name}" for name in ("model", "attention", "snapshot", "tui", "tui_app", "locate"))
CODE_RECORD_KEY = "code_identities"
CODE_PATH_NO_RECORD = "no record"
CODE_PATH_IDENTITY_DIFFERS = "identity differs"
CODE_PATH_WITHIN_STEP = "write within resolution step"
MISS = object()

_LOCK = threading.Lock()
STOP = threading.Event()


class Stopped(BaseException):
    """Raised in a read thread that finds ``STOP`` set: the app is leaving, nobody waits for the result.
    Not an ``Exception``: a read's own ``except Exception`` must not turn it into a failed source."""


def check_stop() -> None:
    """Called between the sources of a read, so a read that is still going when the app starts to leave ends at
    the next source instead of competing with the snapshot write for the interpreter."""
    if STOP.is_set():
        raise Stopped


def now_ns() -> int:
    return time.time_ns()


def identity_of(st) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def settled(st, read_ns: int | None = None) -> bool:
    """Whether a read at ``read_ns`` saw ``st``'s file a step after it last changed: a writer that restores an
    old mtime (``cp -p``, ``rsync -t``, ``utime``) still moves the ctime."""
    changed = max(st.st_mtime_ns, st.st_ctime_ns)
    return (now_ns() if read_ns is None else read_ns) - changed >= MTIME_STEP_NS


def same_identity(kind: str, cached, live) -> bool:
    return tuple(cached) == tuple(live)


class IdentityCache:
    """Values by path, valid while the path's identity is the one they were read under."""

    def __init__(self, kind: str):
        self.kind = kind
        self._items: dict[str, tuple] = {}

    def get(self, key: str, ident):
        entry = self._items.get(key)
        if entry is not None and same_identity(self.kind, entry[0], ident):
            return entry[1]
        return MISS

    def put(self, key: str, ident, value, keep: bool = True) -> None:
        with _LOCK:
            if keep:
                self._items[key] = (tuple(ident), value)
            else:
                self._items.pop(key, None)

    def fetch(self, path, st, load):
        """The cached value, else ``load(path)``, kept only when the file was settled when read."""
        check_stop()
        key, ident = str(path), identity_of(st)
        value = self.get(key, ident)
        if value is not MISS:
            return value
        read_ns = now_ns()
        value = load(path)
        OBSERVED.note_read(key)
        self.put(key, ident, value, settled(st, read_ns))
        return value

    def entries(self) -> dict[str, tuple]:
        with _LOCK:
            return dict(self._items)

    def prune(self, keep) -> None:
        with _LOCK:
            for key in [k for k in self._items if k not in keep]:
                del self._items[key]

    def clear(self) -> None:
        with _LOCK:
            self._items.clear()


def scan(root, suffix: str) -> dict[str, tuple[str, os.stat_result]]:
    """``{stem: (path, stat)}`` of the regular files named ``*<suffix>`` in ``root``, from one directory pass."""
    found = {}
    try:
        for entry in KEPT.scandir(root):
            check_stop()
            if not entry.name.endswith(suffix):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if stat_module.S_ISREG(st.st_mode):
                found[entry.name[: -len(suffix)]] = (entry.path, st)
    except OSError:
        pass
    return found


def directory_marker(path) -> list[int] | None:
    """``[dev, ino, mtime_ns, ctime_ns]`` of a directory: a file created, removed or renamed in it moves them."""
    try:
        st = KEPT.stat(path)
    except OSError:
        return None
    return [st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns]


class Recorded(dict):
    """The identities a snapshot recorded (``path → (kind, identity)``) and, per kind, what a path of that kind
    it does not record is judged by (``unrecorded``: ``kind → (directory, written_ns, marker)``): the directory
    it lies in, the snapshot's write time, and the directory's marker when it was settled at its listing."""

    def __init__(self, items, unrecorded: dict[str, tuple[str, int, list[int] | None]] | None = None):
        super().__init__(items)
        self.unrecorded = unrecorded or {}


def _last_change(ident_or_marker) -> int:
    """The later of the mtime and the ctime of an identity (``…, mtime_ns, ctime_ns``) or a marker."""
    return max(ident_or_marker[-2:])


def _unchanged(reference: dict, path: str, kind: str, ident, live_marker=None) -> bool:
    """Whether ``path`` at ``ident`` is as ``reference`` (an observation, or a snapshot's ``Recorded``) saw it.
    A path a snapshot does not record is unchanged when it last changed a resolution step before the write,
    and, unless its directory's ``live_marker`` is still the recorded one, no later than the directory's last
    change the marker recorded: a file created or renamed in since is newer than that."""
    if path in reference:
        return same_identity(kind, reference[path][1], ident)
    directory, written_ns, marker = getattr(reference, "unrecorded", {}).get(kind, (None, None, None))
    if directory != os.path.dirname(path) or marker is None:
        return False
    if not settled(SimpleNamespace(st_mtime_ns=ident[3], st_ctime_ns=ident[4]), written_ns):
        return False
    return live_marker == marker or _last_change(ident) <= _last_change(marker)


class Observed:
    """The identities the dashboard saw this cycle, against those of the cycle before (or of the snapshot it
    started from): the set a cycle reports as ``applied``, and the ``sources`` of the next snapshot."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        with _LOCK:
            self.previous: dict[str, tuple] | None = None
            self.base: dict[str, tuple] | None = None
            self.current: dict[str, dict[str, tuple]] = {kind: {} for kind in SOURCE_KINDS}
            self.directories: dict[str, dict] = {}
            self.loaded: set[str] = set()
            self.touched: set[str] = set()
            self.read: set[str] = set()

    def seed(self, recorded: Recorded) -> None:
        with _LOCK:
            self.previous = self.base = recorded

    def scanned(self, kind: str, items: dict[str, tuple], directory: dict | None = None) -> None:
        """Replace every identity of ``kind`` by those of one full directory pass: ``{path: (stat, read_ns)}``;
        ``directory`` is the pass's ``{"path", "marker", "taken_ns"}``, the marker stat'ed before the listing."""
        with _LOCK:
            self.current[kind] = {path: (identity_of(st), settled(st, read_ns)) for path, (st, read_ns) in items.items()}
            if directory is not None:
                self.directories[kind] = directory

    def marker(self, kind: str) -> list[int] | None:
        """The marker of the directory of ``kind`` as of its last listing."""
        return (self.directories.get(kind) or {}).get("marker")

    def unsettled(self, kind: str) -> list[str]:
        with _LOCK:
            return [path for path, (_, ok) in self.current[kind].items() if not ok]

    def used(self, paths) -> None:
        """The state files the last collect loaded: the ones the rows it made are read from."""
        with _LOCK:
            self.loaded = set(paths)

    def ledger(self, path) -> None:
        try:
            st = KEPT.stat(path)
        except OSError:
            self.scanned("ledger", {})
            return
        self.scanned("ledger", {str(path): (st, now_ns())})

    def note(self, kind: str, path, st) -> None:
        with _LOCK:
            self.current[kind][str(path)] = (identity_of(st), settled(st))
            self.touched.add(str(path))

    def note_read(self, path: str) -> None:
        with _LOCK:
            self.read.add(path)

    def _changed(self, now: dict) -> set[str]:
        changed = set(self.read)
        reference = self.base if self.base is not None else self.previous
        if reference is not None:
            changed |= {path for path, (kind, ident) in now.items()
                        if not _unchanged(reference, path, kind, ident, self.marker(kind))}
            changed |= {path for path in reference if path not in now}
        return changed

    def _now(self) -> dict:
        return {path: (kind, ident) for kind, items in self.current.items() for path, (ident, _) in items.items()}

    def take(self) -> list[str]:
        """The paths created, changed or deleted since the observation before the last one, and every path read
        since the last call: sorted. The observation is then rotated, so that a poll made at any moment of a
        cycle is covered by the report of that cycle. With no snapshot behind it a first call reports only
        what was read."""
        with _LOCK:
            self.current["transcript"] = {p: v for p, v in self.current["transcript"].items() if p in self.touched}
            now = self._now()
            changed = self._changed(now)
            self.base, self.previous, self.read, self.touched = self.previous, now, set(), set()
            return sorted(changed)

    def peek(self) -> list[str]:
        """What ``take`` would report, without rotating anything."""
        with _LOCK:
            return sorted(self._changed(self._now()))

    def kinds(self) -> dict[str, str]:
        with _LOCK:
            out = {path: kind for earlier in (self.base, self.previous) if earlier
                   for path, (kind, _) in earlier.items()}
            out.update({path: kind for kind, items in self.current.items() for path in items})
            return out

    def sources(self, keep=None) -> list[dict]:
        """The settled identities of this cycle; with ``keep(kind, path)``, only those it keeps."""
        with _LOCK:
            return [{"path": path, "kind": kind, **dict(zip(IDENTITY_KEYS, ident))}
                    for kind, items in self.current.items() for path, (ident, ok) in sorted(items.items())
                    if ok and (keep is None or keep(kind, path))]


OBSERVED = Observed()
STATES = IdentityCache("state")
FOUND_BY_STAT = "stat"


class KeptEntry:
    """A directory entry answered from the kept view: ``name``, ``path``, ``is_dir`` and ``stat``."""

    __slots__ = ("name", "path", "_is_dir", "_view")

    def __init__(self, name: str, path: str, is_dir: bool, view: "KeptView"):
        self.name, self.path, self._is_dir, self._view = name, path, is_dir, view

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return self._is_dir

    def stat(self):
        return self._view.stat(self.path)


class KeptView:
    """What the event path knows of the file system without asking it again.

    Every ``stat`` and every directory listing a reader makes goes through here and is kept. While
    ``trusting``, a path under a watched root (``covers``) is answered from memory until an event, an unsettled
    stat or a sweep marks it dirty; then it is asked again. With nothing trusted every call is a live one, so
    the stat path behaves as before. ``found`` says which route marked each path, for ``cycle_end``."""

    def __init__(self):
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.trusting = False
            self.covers = None
            self.stats: dict[str, os.stat_result | None] = {}
            self.listings: dict[str, list[tuple[str, bool]]] = {}
            self.dirty: dict[str, int] = {}
            self.relist: dict[str, int] = {}
            self.marks = 0
            self.found: dict[str, str] = {}

    def trust(self, covers) -> None:
        with self._lock:
            self.covers, self.trusting = covers, True

    def distrust(self) -> None:
        with self._lock:
            self.trusting = False

    def _kept(self, key: str) -> bool:
        return self.trusting and (self.covers is None or self.covers(key))

    def mark(self, paths=(), directories=(), by: str = "event") -> None:
        """Ask again about ``paths`` and about the names in ``directories`` at the next read of each."""
        with self._lock:
            for key in (*map(str, paths), *map(str, directories)):
                self.marks += 1
                self.dirty[key] = self.marks
                self.found.setdefault(key, by)
            for key in map(str, directories):
                self.relist[key] = self.marks

    def take_found(self) -> dict[str, str]:
        with self._lock:
            found, self.found = self.found, {}
            return found

    def stat(self, path):
        key = str(path)
        with self._lock:
            seen = self.dirty.get(key)
            if self._kept(key) and key in self.stats and seen is None:
                kept = self.stats[key]
                if kept is None:
                    raise FileNotFoundError(2, "absent when last looked", key)
                return kept
        try:
            st = os.stat(key)
        except FileNotFoundError:
            self._keep(key, None, seen)
            raise
        self._keep(key, st, seen)
        return st

    def _keep(self, key: str, st, seen) -> None:
        with self._lock:
            self.stats[key] = st
            if self.dirty.get(key) == seen:
                self.dirty.pop(key, None)
            if st is not None and not settled(st):
                self.marks += 1
                self.dirty[key] = self.marks
                self.found.setdefault(key, FOUND_BY_STAT)

    def scandir(self, root) -> list[KeptEntry]:
        key = str(root)
        with self._lock:
            seen = self.relist.get(key)
            kept = self.listings.get(key) if self._kept(key) and seen is None else None
        if kept is None:
            with os.scandir(key) as listing:
                kept = [(entry.name, entry.is_dir()) for entry in listing]
            with self._lock:
                self.listings[key] = kept
                if self.relist.get(key) == seen:
                    self.relist.pop(key, None)
        return [KeptEntry(name, os.path.join(key, name), is_dir, self) for name, is_dir in kept]

    def glob(self, root, pattern: str) -> list[Path]:
        """``Path(root).glob(pattern)`` for a pattern of ``*`` segments and literal or ``*``-suffixed names,
        sorted, from the kept listings."""
        level = [str(root)]
        parts = pattern.split("/")
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            matched = []
            for directory in level:
                try:
                    entries = self.scandir(directory)
                except OSError:
                    continue
                matched += [entry.path for entry in entries
                            if fnmatch.fnmatchcase(entry.name, part) and (last or entry.is_dir())]
            level = matched
        return sorted(Path(path) for path in level)

    def sweep_items(self) -> list[tuple[str, str]]:
        """Everything kept, as ``(kind, path)`` with kind ``stat`` or ``dir``, in a fixed order: the ring a
        reconciliation walks."""
        with self._lock:
            return sorted([("stat", key) for key in self.stats if self._kept(key)]
                          + [("dir", key) for key in self.listings if self._kept(key)], key=lambda i: (i[1], i[0]))

    def differs(self, item: tuple[str, str]) -> bool:
        """Whether the file system no longer holds what the kept view has for ``item`` (one live stat or
        listing). A difference is only reported; the path is marked by the caller."""
        kind, key = item
        with self._lock:
            kept = self.stats.get(key) if kind == "stat" else self.listings.get(key)
            if key in (self.dirty if kind == "stat" else self.relist):
                return False
        try:
            if kind == "stat":
                live = os.stat(key)
                return kept is None or identity_of(live) != identity_of(kept)
            with os.scandir(key) as listing:
                return sorted((e.name, e.is_dir()) for e in listing) != sorted(kept or ())
        except FileNotFoundError:
            return kept is not None if kind == "stat" else True
        except OSError:
            return True


KEPT = KeptView()


def listed(kind: str, root, suffix: str) -> dict[str, tuple[str, os.stat_result]]:
    """``scan(root, suffix)``, recorded in ``OBSERVED`` as every identity of ``kind``, with the directory's
    marker taken before the pass."""
    read_ns = now_ns()
    marker = directory_marker(root)
    found = scan(root, suffix)
    directory = None if marker is None else {"path": str(root), "marker": marker, "taken_ns": read_ns}
    OBSERVED.scanned(kind, {path: (st, read_ns) for path, st in found.values()}, directory)
    return found


def _driven(path) -> list[str]:
    return list(dict.fromkeys(entry["session"] for entry in engine_session_binding._read(Path(path))))


def fingerprint(items) -> int:
    return hash(tuple(items))


class BindingScan:
    """``engine_session_binding.harness_index`` over the binding files, each read once per identity."""

    def __init__(self):
        self.files = IdentityCache("binding")
        self.memo: tuple | None = None
        self.presumption: tuple[frozenset[str], Recorded] | None = None

    def directory(self) -> Path:
        return config_root.agentctl_dir() / engine_session_binding.BINDINGS_DIRNAME

    def presume(self, covered, recorded: Recorded) -> None:
        """Take a binding file the snapshot ``recorded`` does not record, and that has not changed since, to
        drive none of the engine sessions ``covered``: the snapshot records every one that drove them."""
        self.presumption = (frozenset(covered), recorded)

    def read(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """``(harness_index, driven)``: the harness sessions bound to each engine session in file-name order,
        and each harness session's engine sessions in drive order. Under a presumption a file it holds for is
        not read and drives none, and the index is a ``PresumedIndex``."""
        presumption = self.presumption
        found = listed("binding", self.directory(), ".json")
        driven, marker = {}, OBSERVED.marker("binding")
        for stem, (path, st) in sorted(found.items()):
            if (presumption is not None and path not in presumption[1]
                    and _unchanged(presumption[1], path, "binding", identity_of(st), marker)):
                driven[stem] = []
            else:
                driven[stem] = self.files.fetch(path, st, _driven)
        self.files.prune({path for path, _ in found.values()})
        key = fingerprint((str(self.directory()), presumption is not None,
                           *((stem, identity_of(st)) for stem, (_, st) in sorted(found.items()))))
        memo = self.memo
        if memo is None or memo[0] != key:
            index: dict[str, list[str]] = {}
            for stem, engines in driven.items():
                for engine in engines:
                    index.setdefault(engine, []).append(stem)
            if presumption is not None:
                index = PresumedIndex(index, presumption[0])
            memo = self.memo = (key, index, driven)
        return memo[1], memo[2]

    def driven(self) -> dict[str, list[str]]:
        """The engine sessions each harness session drives, as of the last ``read``."""
        memo = self.memo
        return memo[2] if memo is not None else {}

    def clear(self) -> None:
        self.files.clear()
        self.memo = None
        self.presumption = None


class PresumedIndex(dict):
    """A harness index read under a presumption: a lookup of an engine session outside the presumption's cover
    ends the presumption and is answered from a read of every binding file."""

    def __init__(self, items, covered: frozenset[str]):
        super().__init__(items)
        self.covered = covered

    def get(self, engine, default=None):
        if engine in self.covered:
            return super().get(engine, default)
        BINDINGS.presumption = None
        return BINDINGS.read()[0].get(engine, default)


BINDINGS = BindingScan()


def export_values(cache: IdentityCache, identities: dict[str, tuple]) -> dict:
    """The values of ``cache`` whose file the snapshot records under the very identity the value was read
    under: a value read before the file moved on must not be saved under the identity it moved to."""
    return {path: value for path, (ident, value) in cache.entries().items()
            if path in identities and tuple(ident) == tuple(identities[path])}


def seed_values(cache: IdentityCache, values, identities: dict[str, tuple]) -> None:
    """Adopt snapshot ``values`` (path → value) under the identities the snapshot recorded for their files."""
    if not isinstance(values, dict):
        return
    for path, value in values.items():
        if path in identities and cache.get(path, identities[path]) is MISS:
            cache.put(path, identities[path], value)


def identities_of(sources) -> dict[str, tuple]:
    return {item["path"]: tuple(item[key] for key in IDENTITY_KEYS) for item in sources}


def _stem(path: str, suffix: str = ".json") -> str:
    return os.path.basename(path)[: -len(suffix)]


def snapshot_sources(shown) -> tuple[list[dict], dict]:
    """``(sources, observed)`` of a snapshot showing the tasks ``shown`` (engine session ids). ``sources`` are
    the settled identities the shown values are read from: the state files the last collect loaded, the
    binding files driving a covered task, the transcripts and the ledger. ``observed`` records the write time,
    each scanned directory's marker, and the covered tasks: the shown ones no unsettled binding file drives
    (and, under a presumption, inside its cover), so no binding file left out drives one of them."""
    covered = {config_root.sanitize_session_id(s) for s in shown if s}
    if BINDINGS.presumption is not None:
        covered &= BINDINGS.presumption[0]
    driven = BINDINGS.driven()
    for path in OBSERVED.unsettled("binding"):
        covered -= set(driven.get(_stem(path), ()))

    def keep(kind: str, path: str) -> bool:
        if kind == "state":
            return path in OBSERVED.loaded
        if kind == "binding":
            return not covered.isdisjoint(driven.get(_stem(path), ()))
        return True

    observed = {"written_ns": now_ns(), "covered": sorted(covered),
                "directories": {kind: dict(record) for kind, record in OBSERVED.directories.items()}}
    return OBSERVED.sources(keep), observed


def _valid_observed(observed) -> bool:
    try:
        return (isinstance(observed["written_ns"], int) and all(isinstance(s, str) for s in observed["covered"])
                and all(isinstance(r["path"], str) and isinstance(r["taken_ns"], int)
                        and len(r["marker"]) == 4 and all(isinstance(n, int) for n in r["marker"])
                        for r in observed["directories"].values()))
    except (KeyError, TypeError, AttributeError):
        return False


def seed_observation(sources, observed) -> None:
    """Start the observation from a snapshot's ``sources`` and ``observed`` record (see ``_unchanged`` for a
    path it does not record). A directory whose marker was not settled at its listing, or a snapshot with no
    valid record, leaves every path it does not record counted as changed, and every binding file read."""
    recorded = {item["path"]: (item["kind"], tuple(item[key] for key in IDENTITY_KEYS)) for item in sources}
    unrecorded = {}
    if _valid_observed(observed):
        for kind, record in observed["directories"].items():
            marker = record["marker"]
            settled_marker = record["taken_ns"] - _last_change(marker) >= MTIME_STEP_NS
            unrecorded[kind] = (record["path"], observed["written_ns"], marker if settled_marker else None)
    reference = Recorded(recorded, unrecorded)
    OBSERVED.seed(reference)
    if unrecorded.get("binding", (None, None, None))[2] is not None:
        BINDINGS.presume(observed["covered"], reference)


def harness_index() -> dict[str, list[str]]:
    """Look an engine session up with ``get``: under a presumption, other reads see only its cover."""
    return BINDINGS.read()[0]


def harness_sessions_driving(engine_session: str) -> list[str]:
    if not engine_session:
        return []
    return list(harness_index().get(config_root.sanitize_session_id(engine_session), []))


class TranscriptListing:
    """The main-session transcripts by session id, each project directory listed again only when its identity
    changed (a created or deleted transcript moves its directory's mtime)."""

    def __init__(self):
        self.dirs = IdentityCache("transcript")

    def index(self, roots) -> dict[str, list[Path]]:
        found: dict[str, list[Path]] = {}
        seen = set()
        for root in roots:
            for sub, (path, st) in sorted(self._subdirs(root).items()):
                seen.add(path)
                for transcript in self.dirs.fetch(path, st, self._names):
                    found.setdefault(transcript.stem, []).append(transcript)
        self.dirs.prune(seen)
        for matches in found.values():
            if len(matches) > 1:
                matches.sort(key=str)
        return found

    @staticmethod
    def _subdirs(root) -> dict[str, tuple]:
        found = {}
        try:
            for entry in KEPT.scandir(root):
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if stat_module.S_ISDIR(st.st_mode):
                    found[entry.name] = (entry.path, st)
        except OSError:
            pass
        return found

    @staticmethod
    def _names(directory) -> list[Path]:
        try:
            return [Path(entry.path) for entry in sorted(KEPT.scandir(directory), key=lambda e: e.name)
                    if entry.name.endswith(".jsonl") and not entry.is_dir()]
        except OSError:
            return []

    def clear(self) -> None:
        self.dirs.clear()


TRANSCRIPTS = TranscriptListing()


def iter_transcripts(pattern: str = "*/*.jsonl") -> list[Path]:
    """``config_root.iter_transcripts`` from the kept listings."""
    return sorted(path for root in config_root.projects_roots() for path in KEPT.glob(root, pattern))


def transcript_roots(transcripts_root) -> list[Path]:
    return [Path(transcripts_root)] if transcripts_root is not None else config_root.projects_roots()


def reset() -> None:
    KEPT.reset()
    OBSERVED.reset()
    STATES.clear()
    BINDINGS.clear()
    TRANSCRIPTS.clear()


def tasks_of_source(path: str, kind: str, driven: dict[str, list[str]], tasks) -> set[str]:
    """The listed ``tasks`` a source belongs to by the contract's rule: a state file its own task, a binding file
    the tasks its harness session drives, a transcript those and the task of the same name, the ledger all."""
    stem = Path(path).stem
    if kind == "state":
        return {stem} & set(tasks)
    if kind == "binding":
        return set(driven.get(stem, [])) & set(tasks)
    if kind == "transcript":
        return (set(driven.get(stem, [])) | {stem}) & set(tasks)
    return set(tasks)


def applied_by_task(changed, kinds: dict[str, str], driven: dict[str, list[str]], tasks) -> dict[str, list[str]]:
    """Each listed task's share of ``changed``, by ``tasks_of_source``."""
    out: dict[str, list[str]] = {task: [] for task in tasks}
    for path in changed:
        for task in tasks_of_source(path, kinds.get(path, "state"), driven, tasks):
            out[task].append(path)
    return out


def observation(rotate: bool = True) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
    """``(changed, kinds, driven)`` for a report of ``applied``: what was created, changed, deleted or read
    (``Observed.take``, or ``peek`` without ``rotate``), each path's kind and the binding scan's drive map."""
    check_stop()
    changed = OBSERVED.take() if rotate else OBSERVED.peek()
    return changed, OBSERVED.kinds(), BINDINGS.driven()


def selection_key(include_all: bool, window_hours, sessions) -> str:
    return f"{'all' if include_all else 'live'}:{float(window_hours):g}:{','.join(sorted(map(str, sessions or ())))}"


def default_path(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CACHE_HOME") or str(Path(environ.get("HOME") or Path.home()) / ".cache")
    return Path(base) / CACHE_DIRNAME / FILE_NAME


def body_sha256(body) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _repo_module_file(dotted: str, consult=None) -> Path | None:
    base = SCRIPTS.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if consult is not None:
            consult(candidate)
        if candidate.is_file():
            return candidate
    return None


class _CodeWalk:
    """The identities the walk of the code files looked at, so a later start can tell by a stat of each that
    none changed: every code file before it is parsed, and for every place an import could resolve to, the
    nearest existing directory above it, which a module created or removed there changes. A place the walk
    cannot vouch for by a directory — a non-file where a module could be, an entry it cannot stat — breaks it."""

    def __init__(self):
        self.taken_ns = now_ns()
        self.files: dict[str, list[int]] = {}
        self.dirs: dict[str, list[int]] = {}
        self.broken = False

    def _identity(self, path) -> list[int] | None:
        try:
            return list(identity_of(os.stat(path)))
        except OSError:
            self.broken = True
            return None

    def read(self, path: Path) -> None:
        identity = self._identity(path)
        if identity is not None:
            self.files.setdefault(os.path.relpath(path, SCRIPTS), identity)

    def consult(self, candidate: Path) -> None:
        if os.path.lexists(candidate) and not candidate.is_file():
            self.broken = True
            return
        directory = candidate.parent
        while not os.path.lexists(directory) and directory != directory.parent:
            directory = directory.parent
        key = os.path.relpath(directory, SCRIPTS)
        if key not in self.dirs:
            identity = self._identity(directory)
            if identity is not None:
                self.dirs[key] = identity


class CodeFiles(tuple):
    """The sorted code files, and the walk that found them."""

    def __new__(cls, paths, walk: _CodeWalk):
        files = super().__new__(cls, paths)
        files.walk = walk
        return files


def _statements(nodes):
    """The statements of ``nodes`` and, below them, those of every block (``body``, ``orelse``, ``finalbody``,
    ``handlers``, ``cases``). An import is a statement, so a walk of the blocks finds every import that a walk
    of every node finds, without visiting the expressions, which are most of the tree."""
    for node in nodes:
        yield node
        for block in ("body", "orelse", "finalbody", "handlers", "cases"):
            yield from _statements(getattr(node, block, ()))


def _imported_names(path: Path) -> set[str]:
    """Every dotted module name ``path`` imports, at any depth, relative imports resolved against its package
    and each ``from X import y`` also naming ``X.y`` (``y`` may be a submodule)."""
    import ast

    package = path.relative_to(SCRIPTS).parent.parts
    names: set[str] = set()
    for node in _statements(ast.parse(path.read_bytes()).body):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - (node.level - 1)] if node.level else ()
            module = ".".join((*base, *([node.module] if node.module else [])))
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return {".".join(parts[:end]) for parts in (name.split(".") for name in names if name) for end in range(1, len(parts) + 1)}


@functools.lru_cache(maxsize=1)
def code_files() -> tuple[Path, ...]:
    """The repo modules whose code makes what the snapshot stores: ``CODE_ROOTS`` and every module under
    ``scripts/`` they import, transitively. Derived from the imports, so a helper a stored value passes
    through (an effort count, a timestamp parser) is covered without anyone listing it."""
    walk = _CodeWalk()
    found: dict[Path, None] = {}
    pending = [_repo_module_file(name, walk.consult) or SCRIPTS.joinpath(*name.split(".")).with_suffix(".py")
               for name in CODE_ROOTS]
    while pending:
        path = pending.pop()
        if path in found:
            continue
        found[path] = None
        walk.read(path)
        for name in _imported_names(path):
            module = _repo_module_file(name, walk.consult)
            if module is not None and module not in found:
                pending.append(module)
    return CodeFiles(sorted(found), walk)


def _computed_code_version() -> tuple[str, dict | None]:
    """A hash of the modules that compute what the snapshot stores (state digests, tails, sums, row shape).
    A snapshot written by other code is read as absent: its values are kept for as long as a file's identity
    holds, so values another computation made would otherwise outlive the fix of that computation. An
    unreadable or unparsable module gives a version no other process shares."""
    digest = hashlib.sha256()
    try:
        files = code_files()
        for path in files:
            name = path.relative_to(SCRIPTS).as_posix()
            digest.update(name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    except (OSError, SyntaxError, ValueError):
        return f"unreadable-{os.urandom(8).hex()}", None
    version = digest.hexdigest()
    return version, code_record(files, version)


def code_record(files, version: str) -> dict | None:
    """What a snapshot keeps of the walk that gave ``version``, so the next start can adopt the version after a
    stat of each identity; None when a stat cannot vouch for it: no walk, a broken one, an unreadable version."""
    walk = getattr(files, "walk", None)
    if walk is None or walk.broken or version.startswith("unreadable-"):
        return None
    return {"root": str(SCRIPTS), "version": version, "taken_ns": walk.taken_ns,
            "files": dict(walk.files), "dirs": dict(walk.dirs)}


def _is_identity(value) -> bool:
    return (isinstance(value, list) and len(value) == len(IDENTITY_KEYS)
            and all(isinstance(number, int) and not isinstance(number, bool) for number in value))


def _valid_code_record(record) -> bool:
    return (isinstance(record, dict) and isinstance(record.get("root"), str) and isinstance(record.get("version"), str)
            and isinstance(record.get("taken_ns"), int) and not isinstance(record.get("taken_ns"), bool)
            and all(isinstance(record.get(group), dict) and all(isinstance(name, str) and _is_identity(identity)
                                                              for name, identity in record[group].items())
                    for group in ("files", "dirs"))
            and bool(record["files"]))


def same_code_identities(recorded, live) -> bool:
    return tuple(recorded) == tuple(live)


def accepted_code_version(record) -> tuple[str | None, str]:
    """``record``'s version when every identity it holds is the live one and was settled when recorded, with no
    file read or parsed; else None and the first reason the full path is taken."""
    if not _valid_code_record(record):
        return None, CODE_PATH_NO_RECORD
    if record["root"] != str(SCRIPTS):
        return None, CODE_PATH_IDENTITY_DIFFERS
    entries = [*record["files"].items(), *record["dirs"].items()]
    if not all(settled(SimpleNamespace(st_mtime_ns=identity[3], st_ctime_ns=identity[4]), record["taken_ns"])
               for _, identity in entries):
        return None, CODE_PATH_WITHIN_STEP
    for name, identity in entries:
        try:
            live = identity_of(os.stat(SCRIPTS / name))
        except OSError:
            return None, CODE_PATH_IDENTITY_DIFFERS
        if not same_code_identities(identity, live):
            return None, CODE_PATH_IDENTITY_DIFFERS
    return record["version"], ""


class _CodeVersion:
    """``code_version()``: computed once per process (``_computed_code_version``), or adopted from a snapshot's
    record that ``accepted_code_version`` confirmed. ``record`` is what a snapshot written now keeps of it;
    ``path`` says how this process came by it, for the event stream, with the ``reason`` a computation was
    called with."""

    def __init__(self):
        self.cache_clear()

    def cache_clear(self) -> None:
        self.version: str | None = None
        self.record: dict | None = None
        self.path: dict = {}

    def __call__(self, reason: str = CODE_PATH_NO_RECORD) -> str:
        if self.version is None:
            self.version, self.record = _computed_code_version()
            self.path = {"code_version_path": "full", "code_version_reason": reason}
        return self.version

    def adopt(self, record: dict) -> None:
        if self.version is None:
            self.version, self.record = record["version"], record
            self.path = {"code_version_path": "fast"}


_CODE = code_version = _CodeVersion()


def code_check_report() -> dict:
    """``code_version_path`` (``fast`` or ``full``) and, on ``full``, ``code_version_reason``; empty while this
    process has no code version yet."""
    return dict(_CODE.path)


def _stored_code_record(sections: dict):
    records = [section.get(CODE_RECORD_KEY) for section in sections.values() if isinstance(section, dict)]
    return records[0] if records and all(record == records[0] for record in records) else None


def _with_code_record(section, record):
    if not isinstance(section, dict):
        return section
    rest = {key: value for key, value in section.items() if key != CODE_RECORD_KEY}
    return rest if record is None else {**rest, CODE_RECORD_KEY: record}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _remove_stale_temporaries(path: Path) -> None:
    """Remove the temporaries of writers that were killed; another console's temporary, still being written
    by a live process, stays."""
    for stale in path.parent.glob(f"{path.name}.tmp-*"):
        pid = stale.name.rpartition("-")[2]
        if pid.isdigit() and int(pid) != os.getpid() and _pid_alive(int(pid)):
            continue
        stale.unlink(missing_ok=True)


def write(path, sections: dict, sources: list[dict]) -> Path:
    """Write the snapshot atomically: a temporary name beside it, flushed to disk, then renamed over it.
    ``body_sha256`` covers the sections, as the contract says; ``sources_sha256`` covers the identities."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    version = code_version()
    record = _CODE.record if _CODE.version == version else None
    sections = {name: _with_code_record(section, record) for name, section in sections.items()}
    document = {"version": VERSION, "code": version, "body_sha256": body_sha256(sections),
                "sources_sha256": body_sha256(sources), "sections": sections, "sources": sources}
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        _remove_stale_temporaries(path)
        # backslashreplace writes a lone surrogate as its own JSON escape, so it reads back unchanged
        with open(temporary, "w", encoding="utf-8", errors="backslashreplace") as handle:
            handle.write(json.dumps(document, separators=(",", ":"), ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _valid_source(item) -> bool:
    return (isinstance(item, dict) and isinstance(item.get("path"), str) and item.get("kind") in SOURCE_KINDS
            and all(isinstance(item.get(key), int) and not isinstance(item.get(key), bool) for key in IDENTITY_KEYS))


def load(path) -> dict | None:
    """The snapshot at ``path``, or None when it is missing, unreadable, half-written, of another version or
    written by other code, or its sections or sources do not match their hashes."""
    try:
        document = json.loads(Path(path).read_bytes())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict) or document.get("version") != VERSION or isinstance(document.get("version"), bool):
        return None
    sections, sources = document.get("sections"), document.get("sources")
    if not isinstance(sections, dict) or not isinstance(sources, list) or not all(map(_valid_source, sources)):
        return None
    if document.get("body_sha256") != body_sha256(sections) or document.get("sources_sha256") != body_sha256(sources):
        return None
    reason = ""
    if _CODE.version is None:
        record = _stored_code_record(sections)
        version, reason = accepted_code_version(record)
        if version is not None:
            _CODE.adopt(record)
    if document.get("code") != code_version(reason):
        return None
    return document
