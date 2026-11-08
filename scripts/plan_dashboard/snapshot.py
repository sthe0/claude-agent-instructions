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
"""
from __future__ import annotations

import ast
import functools
import hashlib
import json
import os
import stat as stat_module
import threading
import time
from pathlib import Path

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
        with os.scandir(root) as listing:
            for entry in listing:
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
        memo = self.memo
        if memo is None or memo[0] != key:
            index: dict[str, list[str]] = {}
            for stem, engines in driven.items():
                for engine in engines:
                    index.setdefault(engine, []).append(stem)
            memo = self.memo = (key, index, driven)
        return memo[1], memo[2]

    def driven(self) -> dict[str, list[str]]:
        """The engine sessions each harness session drives, as of the last ``read``."""
        memo = self.memo
        return memo[2] if memo is not None else {}

    def clear(self) -> None:
        self.files.clear()
        self.memo = None


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


def _repo_module_file(dotted: str) -> Path | None:
    base = SCRIPTS.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


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
    found: dict[Path, None] = {}
    pending = [_repo_module_file(name) or SCRIPTS.joinpath(*name.split(".")).with_suffix(".py") for name in CODE_ROOTS]
    while pending:
        path = pending.pop()
        if path in found:
            continue
        found[path] = None
        for name in _imported_names(path):
            module = _repo_module_file(name)
            if module is not None and module not in found:
                pending.append(module)
    return tuple(sorted(found))


@functools.lru_cache(maxsize=1)
def code_version() -> str:
    """A hash of the modules that compute what the snapshot stores (state digests, tails, sums, row shape).
    A snapshot written by other code is read as absent: its values are kept for as long as a file's identity
    holds, so values another computation made would otherwise outlive the fix of that computation. An
    unreadable or unparsable module gives a version no other process shares."""
    digest = hashlib.sha256()
    try:
        for path in code_files():
            name = path.relative_to(SCRIPTS).as_posix()
            digest.update(name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    except (OSError, SyntaxError, ValueError):
        return f"unreadable-{os.urandom(8).hex()}"
    return digest.hexdigest()


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
    document = {"version": VERSION, "code": code_version(), "body_sha256": body_sha256(sections),
                "sources_sha256": body_sha256(sources), "sections": sections, "sources": sources}
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        _remove_stale_temporaries(path)
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, separators=(",", ":")))
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
    if document.get("code") != code_version():
        return None
    sections, sources = document.get("sections"), document.get("sources")
    if not isinstance(sections, dict) or not isinstance(sources, list) or not all(map(_valid_source, sources)):
        return None
    if document.get("body_sha256") != body_sha256(sections) or document.get("sources_sha256") != body_sha256(sources):
        return None
    return document
