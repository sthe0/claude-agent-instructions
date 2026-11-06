"""The dashboard's snapshot on disk and the identity-keyed caches that make a refresh cheap.

Difficulty removed: the old code read every state file, scanned about nine thousand binding files twice and
re-parsed a ledger of tens of megabytes on each refresh, so the first rows came after seconds and the
marks after minutes. Here a source is read again only when its *identity* — device, inode, size, mtime and
ctime in nanoseconds — differs from the identity it was read under. A file whose mtime lies within one
resolution step (``MTIME_STEP_NS``) of its read is *unconfirmed*: a second write in the same tick would leave
its identity unchanged, so it is read again on the next cycle and never written to the snapshot as recorded.

``SOURCE_KINDS`` are the kinds of file the dashboard reads; the locator's listing is not among them and is
asked again every cycle.

The snapshot is one JSON file, ``${XDG_CACHE_HOME:-$HOME/.cache}/plan-dashboard/snapshot.json``::

    {"version": 1, "body_sha256": sha256 of "sections", "sections": {<selection>: section},
     "sources": [{"path", "kind", "dev", "ino", "size", "mtime_ns", "ctime_ns"}, ...]}

``sources`` lists the settled identities of the state, binding and ledger files and the watched transcripts as
of the last cycle; a section holds the rows and the overlay that screen showed and the values read from those
sources (state digests, binding contents, transcript tails, ledger sums), which a start seeds the caches
with. The file is written atomically at a clean exit and holds the one section of the selection last shown;
a missing, damaged, half-written, foreign-version or unreadable file is read as absent.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
import threading
import time
from pathlib import Path

from lib import config_root, engine_session_binding

VERSION = 1
MTIME_STEP_NS = 1_000_000
CACHE_DIRNAME = "plan-dashboard"
FILE_NAME = "snapshot.json"
SOURCE_KINDS = ("state", "binding", "transcript", "ledger")
IDENTITY_KEYS = ("dev", "ino", "size", "mtime_ns", "ctime_ns")
MISS = object()

_LOCK = threading.Lock()


def now_ns() -> int:
    return time.time_ns()


def identity_of(st) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def settled(st, read_ns: int | None = None) -> bool:
    """Whether a read at ``read_ns`` saw ``st``'s file after its mtime tick had passed."""
    return (now_ns() if read_ns is None else read_ns) - st.st_mtime_ns >= MTIME_STEP_NS


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
        with os.scandir(root) as listing:
            for entry in listing:
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
            self.touched: set[str] = set()
            self.read: set[str] = set()

    def seed(self, sources) -> None:
        with _LOCK:
            self.previous = {item["path"]: (item["kind"], tuple(item[k] for k in IDENTITY_KEYS)) for item in sources}
            self.base = dict(self.previous)

    def scanned(self, kind: str, items: dict[str, tuple]) -> None:
        """Replace every identity of ``kind`` by those of one full directory pass: ``{path: (stat, read_ns)}``."""
        with _LOCK:
            self.current[kind] = {path: (identity_of(st), settled(st, read_ns)) for path, (st, read_ns) in items.items()}

    def ledger(self, path) -> None:
        try:
            st = os.stat(path)
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
                        if path not in reference or not same_identity(kind, reference[path][1], ident)}
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

    def sources(self) -> list[dict]:
        with _LOCK:
            return [{"path": path, "kind": kind, **dict(zip(IDENTITY_KEYS, ident))}
                    for kind, items in self.current.items() for path, (ident, ok) in sorted(items.items()) if ok]


OBSERVED = Observed()
STATES = IdentityCache("state")


def _driven(path) -> list[str]:
    return list(dict.fromkeys(entry["session"] for entry in engine_session_binding._read(Path(path))))


def fingerprint(items) -> int:
    return hash(tuple(items))


class BindingScan:
    """``engine_session_binding.harness_index`` over the binding files, each read once per identity."""

    def __init__(self):
        self.files = IdentityCache("binding")
        self.memo: tuple | None = None

    def directory(self) -> Path:
        return config_root.agentctl_dir() / engine_session_binding.BINDINGS_DIRNAME

    def read(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """``(harness_index, driven)``: the harness sessions bound to each engine session in file-name order,
        and each harness session's engine sessions in drive order."""
        read_ns = now_ns()
        found = scan(self.directory(), ".json")
        OBSERVED.scanned("binding", {path: (st, read_ns) for path, st in found.values()})
        driven = {stem: self.files.fetch(path, st, _driven) for stem, (path, st) in sorted(found.items())}
        self.files.prune({path for path, _ in found.values()})
        key = fingerprint((str(self.directory()), *((stem, identity_of(st)) for stem, (_, st) in sorted(found.items()))))
        if self.memo is None or self.memo[0] != key:
            index: dict[str, list[str]] = {}
            for stem, engines in driven.items():
                for engine in engines:
                    index.setdefault(engine, []).append(stem)
            self.memo = (key, index, driven)
        return self.memo[1], self.memo[2]

    def driven(self) -> dict[str, list[str]]:
        """The engine sessions each harness session drives, as of the last ``read``."""
        memo = self.memo
        return memo[2] if memo is not None else {}

    def clear(self) -> None:
        self.files.clear()
        self.memo = None


BINDINGS = BindingScan()


def export_values(cache: IdentityCache, paths) -> dict:
    """The values of ``cache`` for the ``paths`` whose identity the snapshot records as settled."""
    return {path: value for path, (_, value) in cache.entries().items() if path in paths}


def seed_values(cache: IdentityCache, values, identities: dict[str, tuple]) -> None:
    """Adopt snapshot ``values`` (path → value) under the identities the snapshot recorded for their files."""
    if not isinstance(values, dict):
        return
    for path, value in values.items():
        if path in identities and cache.get(path, identities[path]) is MISS:
            cache.put(path, identities[path], value)


def identities_of(sources) -> dict[str, tuple]:
    return {item["path"]: tuple(item[key] for key in IDENTITY_KEYS) for item in sources}


def harness_index() -> dict[str, list[str]]:
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
            with os.scandir(root) as listing:
                for entry in listing:
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
            with os.scandir(directory) as listing:
                return [Path(entry.path) for entry in sorted(listing, key=lambda e: e.name)
                        if entry.name.endswith(".jsonl") and not entry.is_dir()]
        except OSError:
            return []

    def clear(self) -> None:
        self.dirs.clear()


TRANSCRIPTS = TranscriptListing()


def transcript_roots(transcripts_root) -> list[Path]:
    return [Path(transcripts_root)] if transcripts_root is not None else config_root.projects_roots()


def reset() -> None:
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


def selection_key(include_all: bool, window_hours, sessions) -> str:
    return f"{'all' if include_all else 'live'}:{float(window_hours):g}:{','.join(sorted(map(str, sessions or ())))}"


def default_path(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CACHE_HOME") or str(Path(environ.get("HOME") or Path.home()) / ".cache")
    return Path(base) / CACHE_DIRNAME / FILE_NAME


def body_sha256(sections) -> str:
    body = json.dumps(sections, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def write(path, sections: dict, sources: list[dict]) -> Path:
    """Write the snapshot atomically: a temporary name beside it, flushed to disk, then renamed over it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"version": VERSION, "body_sha256": body_sha256(sections), "sections": sections, "sources": sources}
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(document, handle, separators=(",", ":"))
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
    its ``sections`` do not match ``body_sha256``."""
    try:
        document = json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("version") != VERSION or isinstance(document.get("version"), bool):
        return None
    sections, sources = document.get("sections"), document.get("sources")
    if not isinstance(sections, dict) or not isinstance(sources, list) or not all(map(_valid_source, sources)):
        return None
    if document.get("body_sha256") != body_sha256(sections):
        return None
    return document
