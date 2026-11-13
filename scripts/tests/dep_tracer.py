"""Per-test dependency tracer: which repository files did a test depend on?

Difficulty removed: reusing an earlier pass for a test whose code the change does not touch needs
to know what "its code" is. pytest-testmon records only the in-process Python that coverage.py
sees, so a script the test runs in a subprocess, a data file it reads or a directory it lists is
invisible to it and a stored pass would be reused after a change to exactly that file. This plugin
records those too, and treats whatever it cannot observe as a reason to rerun, never as a reason
to reuse. It only records; keying, storing and reusing results belong to the suite gate.

Enabling
    The tracer is off unless ``CORE_DEP_TRACER=1`` is set (the suite gate sets it), so a plain
    developer run is unchanged. ``CORE_DEP_TRACER_DIR`` names the record directory; unset, it is
    ``<repo>/.pytest_cache/core-dep-tracer``. ``CORE_DEP_TRACER_ROOT`` overrides the repository
    root (default: two levels above this file). ``CORE_DEP_TRACER_NO_SUBPROCESS=1`` is a test-only
    switch that turns the child-process tracing off, to prove a test goes red without it.
    conftest.py registers the plugin; ``-p dep_tracer`` loads it where that conftest does not
    apply. It needs Python 3.12 (``sys.monitoring``); on an older interpreter, or when the monitoring
    tool id is taken, every test is marked always-rerun with ``tracer-unavailable``.

What is observed, per test (setup, call and teardown together)
    * executed code: ``sys.monitoring`` PY_START, re-armed per test with ``restart_events``;
    * the import closure of every executed file, the test module and its conftest chain, built
      statically from each file's AST (every Import and ImportFrom anywhere in the file; the
      ``import`` audit event is not used because it fires once per session, so a module another
      importer already loaded would lose its edge). A ``importlib.import_module`` or ``__import__``
      call with a string literal adds that edge, one with a computed name marks the test
      ``dynamic-import``;
    * files opened (audit event ``open``), except a file the test itself created with a
      truncating write before reading it;
    * directories listed (``os.listdir``, ``os.scandir``), keyed on the sorted name set, so a new
      file in a listed directory changes the key; ``__pycache__`` and ``.pytest_cache`` names are
      left out of it;
    * paths probed with ``os.stat``, ``os.lstat`` or ``os.access`` (CPython raises no audit event
      for these, so the plugin wraps them): an absent path is ``absent``, a directory ``dir``,
      a regular file its content hash exactly like a read, a symlink ``symlink:<target>``;
    * the same for every Python child: a child inherits ``PYTHONPATH`` with
      ``dep_tracer_site/sitecustomize.py`` first and ``CORE_DEP_TRACER_CHILD_DIR``, and writes a
      log there at exit that the parent merges.
    Only paths inside the repository are recorded, repository-relative; ``__pycache__``,
    ``.pytest_cache``, ``*.pyc`` and the pytest base temp directory are dropped. Reads during
    the setup of a session-, package-, module- or class-scoped fixture go to a set kept per fixture
    that every test using the fixture depends on. Interpreter and plugin versions are not recorded
    here; the suite gate puts them in the key.

Always-rerun reason codes (``REASON_CODES``): whenever tracing is incomplete the record carries
    the codes below and the test is rerun whatever else matches. Directory listings that the
    import system itself makes are not a reason: the static import closure covers them.

    * ``non-python-subprocess``: a non-Python child touches the repository (script, binary, path
      argument or working directory inside it);
    * ``git-repo``: git run against the repository;
    * ``child-env-dropped``: a Python child started without the injected variables;
    * ``child-log-missing``: fewer child logs than traced Python children;
    * ``untraced-exec``: os.system on a command that touches the repository;
    * ``untraced-fork``: os.fork, a multiprocessing fork child or an os.spawn* child;
    * ``repo-wide-listing``: the repository root was listed (os.walk, rglob, listdir of the root);
    * ``sqlite3``: sqlite3.connect on a file path inside the repository (``:memory:`` is not);
    * ``dynamic-import``: import_module or __import__ with a computed name in an executed file;
    * ``unanalysable-source``: an executed file did not parse;
    * ``tracer-unavailable``: sys.monitoring is missing or its tool id is taken;
    * ``tracer-error``: a tracer hook raised.

Record format
    One file per xdist worker (``gw0.jsonl``..., ``main.jsonl`` without xdist) in the record
    directory, JSON lines, truncated when the run starts (the controller also removes stale
    worker files). Two kinds of line::

        {"kind": "file", "id": 7, "path": "scripts/lib/x.py", "digest": "sha256:<hex>"}
        {"kind": "test", "id": "<nodeid>", "files": [7, 8], "listings": {"scripts/lib": "sha256:<hex>"},
         "probes": {"scripts/data.txt": "sha256:<hex>", "gone.txt": "absent"}, "always_rerun": ["git-repo"]}

    A ``file`` line interns a (path, digest) pair once per worker; a ``test`` line refers to them
    by id. A test seen twice (a rerun) keeps its last line. ``load_records`` reads a directory
    back into ``{nodeid: {"files": {path: digest}, "listings": ..., "probes": ..., "always_rerun": [...]}}``.
    To recompute whether a record still holds, apply ``file_digest`` to each ``files`` path,
    ``listing_digest`` to each ``listings`` path and ``probe_digest`` to each ``probes`` path
    (all take an absolute path) and compare with the stored value. Digest values: ``sha256:<hex>``
    (file content; listing: of the sorted names joined with newlines), ``absent``, ``dir`` (a
    directory where a file was read), ``other`` (neither), ``unreadable``, ``symlink:<target>``.

Accepted residuals: reads made inside C extensions other than sqlite3 (mmap, dbm), a teardown
    of a session fixture that runs after the last test, and a non-Python child started through a
    path the tracer cannot connect to the repository (a command that builds the repository path at
    run time) are not seen.
"""
from __future__ import annotations

import ast
import atexit
import hashlib
import json
import os
import posix
import shlex
import shutil
import stat as _stat
import sys
import threading
from pathlib import Path

ENABLE_ENV = "CORE_DEP_TRACER"
DIR_ENV = "CORE_DEP_TRACER_DIR"
ROOT_ENV = "CORE_DEP_TRACER_ROOT"
CHILD_DIR_ENV = "CORE_DEP_TRACER_CHILD_DIR"
NO_SUBPROCESS_ENV = "CORE_DEP_TRACER_NO_SUBPROCESS"
PLUGIN_NAME = "core-dep-tracer-impl"
SITE_DIR = str(Path(__file__).resolve().parent / "dep_tracer_site")
TOOL_ID = 2  # sys.monitoring.PROFILER_ID
DEFAULT_ROOT = Path(__file__).resolve().parents[2]

REASON_CODES = {
    "non-python-subprocess": "a child that is not Python touches the repository (a script or binary of the repository, a path argument, a working directory inside it); its reads are invisible",
    "git-repo": "git run against the repository",
    "child-env-dropped": "a Python child started with an env that lacks the injected variables",
    "child-log-missing": "fewer child logs than traced Python children (python -I or -S, a PYTHONPATH override, a killed child)",
    "untraced-exec": "os.system on a command that touches the repository",
    "untraced-fork": "os.fork, a multiprocessing fork child or an os.spawn* child, none of which runs the child hooks",
    "repo-wide-listing": "the repository root was listed (os.walk of the repository, rglob, listdir of the root)",
    "sqlite3": "sqlite3.connect on a path inside the repository (the reads happen in C code)",
    "dynamic-import": "importlib.import_module or __import__ with a computed name in an executed file",
    "unanalysable-source": "an executed file did not parse, so its imports are unknown",
    "tracer-unavailable": "sys.monitoring is missing (Python < 3.12) or its tool id is taken",
    "tracer-error": "a tracer hook raised",
}

ABSENT = "absent"
_IGNORED_PARTS = ("__pycache__", ".pytest_cache")


class DepSet:
    """What one scope (a test, a scoped fixture, a child process) depended on."""

    def __init__(self):
        self.code: set[str] = set()
        self.files: dict[str, str] = {}
        self.listings: dict[str, str] = {}
        self.probes: dict[str, str] = {}
        self.written: set[str] = set()
        self.reasons: set[str] = set()
        self.expected_children = 0
        self.fork0 = 0

    def merge(self, other: "DepSet") -> None:
        self.code |= other.code
        for mine, theirs in ((self.files, other.files), (self.listings, other.listings), (self.probes, other.probes)):
            for key, value in theirs.items():
                mine.setdefault(key, value)
        self.reasons |= other.reasons
        self.expected_children += other.expected_children


class _Local(threading.local):
    busy = False


_tl = _Local()
_current: DepSet | None = None
_roots: tuple[str, ...] = ()
_root_slash: tuple[str, ...] = ()
_ignore_prefixes: tuple[str, ...] = ()
_forks = [0]
_installed = False
_monitoring_ok = False
_digests: dict[str, tuple[tuple, str]] = {}

_real_stat = posix.stat
_real_lstat = posix.lstat
_real_listdir = posix.listdir


class _quiet:
    """Hooks ignore every event raised by the tracer's own file and directory access."""

    def __enter__(self):
        self.was = _tl.busy
        _tl.busy = True

    def __exit__(self, *exc):
        _tl.busy = self.was


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1" and not os.environ.get(CHILD_DIR_ENV)


def set_root(root) -> None:
    global _roots, _root_slash
    real = os.path.realpath(root)
    plain = os.path.abspath(root)
    _roots = tuple(dict.fromkeys((real, plain)))
    _root_slash = tuple(r + "/" for r in _roots)


def set_ignored_prefixes(prefixes) -> None:
    global _ignore_prefixes
    _ignore_prefixes = tuple(p.rstrip("/") + "/" for p in prefixes)


def _repo_abs(path):
    """(absolute path, repository-relative path) for a path inside the repository, else None."""
    if isinstance(path, int):
        return None
    try:
        s = os.fspath(path)
    except TypeError:
        return None
    if isinstance(s, bytes):
        s = os.fsdecode(s)
    if not s:
        return None
    if s[0] != "/":
        s = os.path.join(os.getcwd(), s)
    if "/." in s or "//" in s:
        s = os.path.normpath(s)
    for root, prefix in zip(_roots, _root_slash):
        if s == root:
            return s, "."
        if s.startswith(prefix):
            rel = s[len(prefix):]
            if rel.endswith(".pyc") or any(part in rel for part in _IGNORED_PARTS):
                return None
            if _ignore_prefixes and any(s.startswith(p) for p in _ignore_prefixes):
                return None
            return s, rel
    return None


def file_digest(path: str) -> str:
    """Content digest of a file read: ``sha256:<hex>``, or ``absent`` / ``dir`` / ``other`` / ``unreadable``."""
    try:
        st = _real_stat(path)
    except OSError:
        return ABSENT
    if _stat.S_ISDIR(st.st_mode):
        return "dir"
    if not _stat.S_ISREG(st.st_mode):
        return "other"
    sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    hit = _digests.get(path)
    if hit is not None and hit[0] == sig:
        return hit[1]
    digest = hashlib.sha256()
    try:
        with _quiet(), open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return "unreadable"
    value = "sha256:" + digest.hexdigest()
    _digests[path] = (sig, value)
    return value


def listing_digest(path: str) -> str:
    """Digest of a directory's sorted entry names (``__pycache__`` and ``.pytest_cache`` left out)."""
    try:
        names = _real_listdir(path)
    except OSError:
        return ABSENT
    names = sorted(n for n in names if n not in _IGNORED_PARTS)
    return "sha256:" + hashlib.sha256("\n".join(names).encode("utf-8", "surrogateescape")).hexdigest()


def probe_digest(path: str, follow: bool = True) -> str:
    """State of a path probed with stat/lstat/access: ``absent``, ``dir``, ``symlink:<target>``, a file digest."""
    try:
        st = _real_stat(path) if follow else _real_lstat(path)
    except OSError:
        return ABSENT
    mode = st.st_mode
    if _stat.S_ISLNK(mode):
        try:
            return "symlink:" + os.readlink(path)
        except OSError:
            return "symlink:"
    if _stat.S_ISDIR(mode):
        return "dir"
    if _stat.S_ISREG(mode):
        return file_digest(path)
    return "other"


def _truncates(mode, flags) -> bool:
    if isinstance(mode, str) and mode:
        return "w" in mode or "x" in mode
    return bool(isinstance(flags, int) and flags & (os.O_TRUNC | os.O_EXCL))


def _env_get(env, name):
    if env is None:
        return os.environ.get(name)
    try:
        value = env.get(name)
        if value is None:
            value = env.get(name.encode())
    except Exception:
        return None
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    return value


def _is_python(executable: str) -> bool:
    base = os.path.basename(executable)
    if base.startswith("python") or base.startswith("pypy"):
        return True
    try:
        return os.path.realpath(executable) == os.path.realpath(sys.executable)
    except OSError:
        return False


def _touches_repo(tokens, cwd) -> bool:
    base = os.fsdecode(cwd) if cwd is not None else os.getcwd()
    if _repo_abs(base) is not None:
        return True
    for token in tokens[:64]:
        if any(root in token for root in _roots):
            return True
        for piece in {token, *token.split()}:
            if not piece or piece.startswith("-") or len(piece) > 4096:
                continue
            candidate = piece if piece.startswith("/") else os.path.join(base, piece)
            found = _repo_abs(candidate)
            if found is not None and os.path.lexists(found[0]):
                return True
    return False


def _note_spawn(cur: DepSet, executable, argv, cwd, env) -> None:
    exe = os.fsdecode(executable) if executable is not None else ""
    tokens = [os.fsdecode(a) for a in argv] if argv is not None and not isinstance(argv, (str, bytes)) else [os.fsdecode(argv)]
    exe = exe or (tokens[0] if tokens else "")
    if _is_python(exe):
        if os.environ.get(NO_SUBPROCESS_ENV):
            return
        paths = (_env_get(env, "PYTHONPATH") or "").split(os.pathsep)
        if _env_get(env, CHILD_DIR_ENV) and SITE_DIR in paths:
            cur.expected_children += 1
        else:
            cur.reasons.add("child-env-dropped")
    elif _touches_repo(tokens, cwd):
        cur.reasons.add("git-repo" if os.path.basename(exe) == "git" else "non-python-subprocess")


def _h_open(cur: DepSet, args) -> None:
    found = _repo_abs(args[0])
    if found is None:
        return
    abs_path, rel = found
    if rel in cur.written:
        return
    if _truncates(args[1], args[2]):
        cur.written.add(rel)
        return
    if rel not in cur.files:
        cur.files[rel] = file_digest(abs_path)


def _from_import_machinery() -> bool:
    """Directory listings the import system makes to find a module are covered by the static import closure."""
    frame = sys._getframe(2)
    for _ in range(6):
        if frame is None:
            return False
        if frame.f_code.co_filename.startswith("<frozen importlib"):
            return True
        frame = frame.f_back
    return False


def _h_listing(cur: DepSet, args) -> None:
    target = args[0] if args else None
    if target is None:
        target = os.getcwd()
    found = _repo_abs(target)
    if found is None or _from_import_machinery():
        return
    abs_path, rel = found
    if rel == ".":
        cur.reasons.add("repo-wide-listing")
    if rel not in cur.listings:
        cur.listings[rel] = listing_digest(abs_path)


def _h_popen(cur: DepSet, args) -> None:
    executable, argv, cwd, env = args[:4]
    _note_spawn(cur, executable, argv, cwd, env)


def _h_posix_spawn(cur: DepSet, args) -> None:
    path, argv, env = args[:3]
    _note_spawn(cur, path, argv, None, env)


def _h_system(cur: DepSet, args) -> None:
    command = os.fsdecode(args[0])
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if _touches_repo(tokens or [command], None):
        cur.reasons.add("untraced-exec")


def _h_sqlite(cur: DepSet, args) -> None:
    database = args[0]
    if isinstance(database, str) and database.startswith("file:"):
        database = database[5:].split("?", 1)[0]
        if database.startswith("//"):
            database = database[2:]
    if isinstance(database, (str, bytes, os.PathLike)) and os.fsdecode(database) not in ("", ":memory:") \
            and _repo_abs(database) is not None:
        cur.reasons.add("sqlite3")


_HANDLERS = {
    "open": _h_open,
    "os.listdir": _h_listing,
    "os.scandir": _h_listing,
    "subprocess.Popen": _h_popen,
    "os.posix_spawn": _h_posix_spawn,
    "os.system": _h_system,
    "sqlite3.connect": _h_sqlite,
}


def _audit(event, args):
    handler = _HANDLERS.get(event)
    if handler is None:
        return
    cur = _current
    if cur is None or _tl.busy:
        return
    _tl.busy = True
    try:
        handler(cur, args)
    except Exception:
        cur.reasons.add("tracer-error")
    finally:
        _tl.busy = False


def _probing(real, follow):
    def probe(path, *args, **kwargs):
        cur = _current
        if cur is not None and not _tl.busy and not kwargs.get("dir_fd"):
            _tl.busy = True
            try:
                found = _repo_abs(path)
                if found is not None and found[1] not in cur.probes and found[1] not in cur.written:
                    cur.probes[found[1]] = probe_digest(found[0], follow and kwargs.get("follow_symlinks", True))
            except Exception:
                cur.reasons.add("tracer-error")
            finally:
                _tl.busy = False
        return real(path, *args, **kwargs)

    probe.__name__ = real.__name__
    probe.__doc__ = real.__doc__
    return probe


def _on_py_start(code, offset):
    cur = _current
    if cur is not None:
        name = code.co_filename
        if name.startswith(_root_slash):
            cur.code.add(name)
        elif name and name[0] != "/" and name[0] != "<":
            cur.code.add(os.path.join(os.getcwd(), name))
    return _DISABLE


_DISABLE = None


def _start_monitoring() -> bool:
    global _DISABLE
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is None:
        return False
    try:
        monitoring.use_tool_id(TOOL_ID, "core-dep-tracer")
    except ValueError:
        return False
    _DISABLE = monitoring.DISABLE
    monitoring.register_callback(TOOL_ID, monitoring.events.PY_START, _on_py_start)
    monitoring.set_events(TOOL_ID, monitoring.events.PY_START)
    return True


def install(root) -> bool:
    """Install the process-wide hooks once; False when code execution cannot be observed."""
    global _installed, _monitoring_ok
    if _installed:
        return _monitoring_ok
    _installed = True
    set_root(root)
    _monitoring_ok = _start_monitoring()
    sys.addaudithook(_audit)
    os.register_at_fork(after_in_parent=lambda: _forks.__setitem__(0, _forks[0] + 1))
    for name, follow in (("stat", True), ("lstat", False)):
        real = getattr(os, name)
        wrapper = _probing(real, follow)
        setattr(os, name, wrapper)
        for supported in (os.supports_follow_symlinks, os.supports_dir_fd, os.supports_fd):
            if real in supported:
                supported.add(wrapper)
    real_access = os.access
    access = _probing(real_access, True)
    os.access = access
    for supported in (os.supports_follow_symlinks, os.supports_dir_fd, os.supports_effective_ids):
        if real_access in supported:
            supported.add(access)
    return _monitoring_ok


def activate(cur: DepSet | None) -> None:
    global _current
    _current = cur
    if cur is not None:
        cur.fork0 = _forks[0]
    if _monitoring_ok:
        sys.monitoring.restart_events()


def deactivate() -> DepSet | None:
    global _current
    cur = _current
    _current = None
    if cur is not None and _forks[0] > cur.fork0:
        cur.reasons.add("untraced-fork")
    return cur


# --- static import closure -------------------------------------------------------------------

_parsed: dict[tuple[str, str], tuple[tuple, bool]] = {}
_resolved: dict[tuple, tuple[str, ...]] = {}


def _parse_imports(path: str, digest: str):
    """Import statements of a file as ((level, module, names), ...), and whether the file has a computed import."""
    key = (path, digest)
    hit = _parsed.get(key)
    if hit is not None:
        return hit
    dynamic = False
    imports = []
    try:
        with _quiet(), open(path, "rb") as handle:
            tree = ast.parse(handle.read(), filename=path)
    except (OSError, SyntaxError, ValueError):
        result = ((), None)
        _parsed[key] = result
        return result
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((0, alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.level, node.module or "", tuple(a.name for a in node.names if a.name != "*")))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if name in ("import_module", "__import__"):
                arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "name"), None)
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and not arg.value.startswith("."):
                    imports.append((0, arg.value, ()))
                else:
                    dynamic = True
    result = (tuple(imports), dynamic)
    _parsed[key] = result
    return result


def _resolve(name: str, dirs: tuple[str, ...]) -> tuple[str, ...]:
    """Repository files executed by importing the dotted ``name``, searching ``dirs`` in order."""
    key = (name, dirs)
    hit = _resolved.get(key)
    if hit is not None:
        return hit
    parts = name.split(".") if name else []
    result: tuple[str, ...] = ()
    for directory in dirs:
        found: list[str] = []
        current = directory
        for part in parts:
            pkg = os.path.join(current, part)
            if os.path.isfile(os.path.join(pkg, "__init__.py")):
                found.append(os.path.join(pkg, "__init__.py"))
                current = pkg
            elif os.path.isfile(pkg + ".py"):
                found.append(pkg + ".py")
                break
            elif os.path.isdir(pkg):
                current = pkg
            else:
                break
        if found:
            result = tuple(found)
            break
    _resolved[key] = result
    return result


def _edges(path: str, search: tuple[str, ...]):
    digest = file_digest(path)
    imports, dynamic = _parse_imports(path, digest)
    if dynamic is None:
        return (), "unanalysable-source"
    here = os.path.dirname(path)
    out: list[str] = []
    package = here
    while os.path.isfile(os.path.join(package, "__init__.py")):
        out.append(os.path.join(package, "__init__.py"))
        package = os.path.dirname(package)
    for level, module, names in imports:
        if level:
            base = here
            for _ in range(level - 1):
                base = os.path.dirname(base)
            dirs = (base,)
        else:
            dirs = search + (here,)
        out.extend(_resolve(module, dirs))
        for name in names:
            out.extend(_resolve(f"{module}.{name}" if module else name, dirs))
    return tuple(out), ("dynamic-import" if dynamic else None)


def search_dirs() -> tuple[str, ...]:
    """Directories of ``sys.path`` inside the repository, in order."""
    found = []
    for entry in sys.path:
        hit = _repo_abs(entry or os.getcwd())
        if hit is not None and hit[0] not in found:
            found.append(hit[0])
    return tuple(found)


def import_closure(roots, search: tuple[str, ...] | None = None):
    """(files, reasons): ``roots`` plus every repository file they import, transitively."""
    search = search_dirs() if search is None else search
    seen: set[str] = set()
    reasons: set[str] = set()
    queue = list(roots)
    with _quiet():
        while queue:
            path = queue.pop()
            if path in seen:
                continue
            seen.add(path)
            edges, reason = _edges(path, search)
            if reason:
                reasons.add(reason)
            queue.extend(e for e in edges if e not in seen)
    return seen, reasons


# --- records -----------------------------------------------------------------------------------

def load_records(directory) -> dict[str, dict]:
    """Read a record directory back: {nodeid: {files, listings, probes, always_rerun}}."""
    records: dict[str, dict] = {}
    for path in sorted(Path(directory).glob("*.jsonl")):
        interned: dict[int, tuple[str, str]] = {}
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry["kind"] == "file":
                interned[entry["id"]] = (entry["path"], entry["digest"])
            elif entry["kind"] == "test":
                records[entry["id"]] = {
                    "files": dict(interned[i] for i in entry["files"]),
                    "listings": entry["listings"],
                    "probes": entry["probes"],
                    "always_rerun": entry["always_rerun"],
                }
            else:
                raise ValueError(f"{path}: unknown record kind {entry['kind']!r}")
    return records


class _Writer:
    def __init__(self, path: Path):
        self.handle = open(path, "w", buffering=1)
        self.ids: dict[tuple[str, str], int] = {}

    def file_id(self, rel: str, digest: str) -> int:
        key = (rel, digest)
        found = self.ids.get(key)
        if found is None:
            found = self.ids[key] = len(self.ids)
            self.handle.write(json.dumps({"kind": "file", "id": found, "path": rel, "digest": digest}) + "\n")
        return found

    def test(self, nodeid: str, files: dict[str, str], dep: DepSet) -> None:
        ids = sorted(self.file_id(rel, digest) for rel, digest in files.items())
        self.handle.write(json.dumps({
            "kind": "test", "id": nodeid, "files": ids, "listings": dep.listings,
            "probes": {rel: d for rel, d in dep.probes.items() if rel not in files},
            "always_rerun": sorted(dep.reasons),
        }) + "\n")

    def close(self) -> None:
        self.handle.close()


def _child_log_path(child_dir: str) -> str:
    return os.path.join(child_dir, f"{os.getpid()}-{os.urandom(4).hex()}.json")


def start_child() -> None:
    """Called by dep_tracer_site/sitecustomize.py in a child Python process of a traced test."""
    root = os.environ.get(ROOT_ENV)
    child_dir = os.environ.get(CHILD_DIR_ENV)
    if not root or not child_dir or os.environ.get(ENABLE_ENV) != "1":
        return
    ok = install(root)
    cur = DepSet()
    if not ok:
        cur.reasons.add("tracer-unavailable")
    activate(cur)

    def write_log() -> None:
        deactivate()
        payload = {
            "code": sorted(cur.code), "files": cur.files, "listings": cur.listings, "probes": cur.probes,
            "reasons": sorted(cur.reasons), "expected_children": cur.expected_children,
        }
        try:
            with _quiet():
                os.makedirs(child_dir, exist_ok=True)
                with open(_child_log_path(child_dir), "w") as handle:
                    json.dump(payload, handle)
        except OSError:
            pass

    atexit.register(write_log)


def _read_child_logs(child_dir: str, root_abs: str, into: DepSet) -> int:
    """Merge the child logs of one test into ``into``; return how many logs there were."""
    try:
        names = os.listdir(child_dir)
    except OSError:
        return 0
    count = 0
    for name in sorted(names):
        try:
            with open(os.path.join(child_dir, name)) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            into.reasons.add("child-log-missing")
            continue
        count += 1
        part = DepSet()
        part.code = {os.path.join(root_abs, c) if not os.path.isabs(c) else c for c in data["code"]}
        part.files, part.listings, part.probes = data["files"], data["listings"], data["probes"]
        part.reasons = set(data["reasons"])
        part.expected_children = data["expected_children"]
        into.merge(part)
    shutil.rmtree(child_dir, ignore_errors=True)
    return count


# --- the pytest plugin ---------------------------------------------------------------------

def _make_plugin():
    import pytest

    class DepTracerPlugin:
        def __init__(self):
            self.worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
            self.writer: _Writer | None = None
            self.fixture_sets: dict = {}
            self.seq = 0
            self.cur: DepSet | None = None
            self.root = os.environ.get(ROOT_ENV) or str(DEFAULT_ROOT)
            self.directory = Path(os.environ.get(DIR_ENV) or Path(self.root) / ".pytest_cache" / "core-dep-tracer")
            self.saved_env: dict[str, str | None] = {}
            self.traces = False

        def pytest_configure(self, config):
            os.environ[ROOT_ENV] = self.root
            os.environ[DIR_ENV] = str(self.directory)
            self.directory.mkdir(parents=True, exist_ok=True)
            is_worker = "PYTEST_XDIST_WORKER" in os.environ
            controller = not is_worker and config.getoption("numprocesses", None) not in (None, 0)
            if not is_worker:
                for stale in list(self.directory.glob("gw*.jsonl")) + list(self.directory.glob("main.jsonl")):
                    stale.unlink()
                shutil.rmtree(self.directory / "children", ignore_errors=True)
            if controller:
                return
            self.traces = True
            basetemp = config.getoption("basetemp", None)
            ignored = [str(Path(basetemp).resolve())] if basetemp else []
            install(self.root)
            set_ignored_prefixes(ignored)
            self.writer = _Writer(self.directory / f"{self.worker}.jsonl")

        def pytest_unconfigure(self, config):
            if self.writer is not None:
                self.writer.close()

        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            if not self.traces:
                return (yield)
            self._begin()
            try:
                return (yield)
            finally:
                self._finish(item)

        @pytest.hookimpl(wrapper=True)
        def pytest_fixture_setup(self, fixturedef, request):
            if not self.traces or fixturedef.scope == "function" or _current is None:
                return (yield)
            scoped = self.fixture_sets.setdefault(fixturedef, DepSet())
            outer = deactivate()
            activate(scoped)
            try:
                return (yield)
            finally:
                deactivate()
                activate(outer)

        def _begin(self):
            self.seq += 1
            cur = self.cur = DepSet()
            if not _monitoring_ok:
                cur.reasons.add("tracer-unavailable")
            child_dir = str(self.directory / "children" / f"{self.worker}-{self.seq}")
            self.child_dir = child_dir
            self.saved_env = {k: os.environ.get(k) for k in ("PYTHONPATH", CHILD_DIR_ENV)}
            if not os.environ.get(NO_SUBPROCESS_ENV):
                os.environ[CHILD_DIR_ENV] = child_dir
                old = os.environ.get("PYTHONPATH")
                os.environ["PYTHONPATH"] = SITE_DIR + (os.pathsep + old if old else "")
            activate(cur)

        def _fixture_sets_for(self, item):
            try:
                defs = {d for group in item._fixtureinfo.name2fixturedefs.values() for d in group}
            except AttributeError:
                return list(self.fixture_sets.values())
            return [self.fixture_sets[d] for d in defs if d in self.fixture_sets]

        def _static_roots(self, item) -> list[str]:
            roots = [str(item.path)]
            inipath = getattr(item.config, "inipath", None)
            if inipath is not None:
                roots.append(str(inipath))
            directory = Path(item.path).parent
            while True:
                conftest = directory / "conftest.py"
                if conftest.is_file():
                    roots.append(str(conftest))
                if directory == Path(self.root) or directory.parent == directory:
                    break
                directory = directory.parent
            return roots

        def _finish(self, item):
            cur = self.cur
            self.cur = None
            deactivate()
            for key, value in self.saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            with _quiet():
                merged = DepSet()
                merged.merge(cur)
                for scoped in self._fixture_sets_for(item):
                    merged.merge(scoped)
                logs = _read_child_logs(self.child_dir, self.root, merged)
                if logs < merged.expected_children:
                    merged.reasons.add("child-log-missing")
                if cur is not None and not _monitoring_ok:
                    merged.reasons.add("tracer-unavailable")
                merged.code.update(self._static_roots(item))
                located = (_repo_abs(c) for c in merged.code)
                code_files = {found[0] for found in located if found is not None and found[1] != "."}
                closure, reasons = import_closure(code_files)
                merged.reasons |= reasons
                files = dict(merged.files)
                for path in closure | code_files:
                    found = _repo_abs(path)
                    if found is not None and found[1] not in files and found[1] not in merged.written:
                        files[found[1]] = file_digest(path)
                self.writer.test(item.nodeid, files, merged)

    return DepTracerPlugin()


def register(pluginmanager) -> bool:
    """Register the plugin when ``CORE_DEP_TRACER=1``; idempotent."""
    if not enabled() or pluginmanager.has_plugin(PLUGIN_NAME):
        return False
    pluginmanager.register(_make_plugin(), PLUGIN_NAME)
    return True


def pytest_addhooks(pluginmanager):
    register(pluginmanager)
