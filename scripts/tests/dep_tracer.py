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
    tool id is taken, the plugin degrades: every test is marked always-rerun with
    ``tracer-unavailable`` (the suite's own ``test_requires_python_312`` is the loud signal).

What is observed, per test (setup, call and teardown together)
    * executed code: ``sys.monitoring`` PY_START, re-armed per test with ``restart_events``;
    * the import closure of every executed file, the test module and its conftest chain, built
      statically from each file's AST (every Import and ImportFrom anywhere in the file; the
      ``import`` audit event is not used because it fires once per session, so a module another
      importer already loaded would lose its edge). A ``importlib.import_module`` or ``__import__``
      call with a string literal adds that edge, one with a computed name marks the test
      ``dynamic-import``. The search for each imported name records the candidate paths it passed
      over, or all of them when nothing was found, as ``absent`` probes (names of the standard
      library that no repository file provides are left out), so a new or shadowing module changes
      the key;
    * files opened (audit event ``open``), except a file the test itself created with a
      truncating write before reading it;
    * directories listed (``os.listdir``, ``os.scandir``), keyed on the sorted name set, so a new
      file in a listed directory changes the key; ``__pycache__`` and ``.pytest_cache`` names are
      left out of it. A listing whose immediate caller is the import system's path finder is
      dropped (the probes above cover it); a listing made by module code that runs during an
      import is kept;
    * paths probed with ``os.stat``, ``os.lstat`` or ``os.access`` (CPython raises no audit event
      for these, so the plugin wraps them): an absent path is ``absent``, a directory ``dir``,
      a regular file its content hash exactly like a read, a symlink ``symlink:<target>``;
    * the same for every Python child: a child inherits ``PYTHONPATH`` with
      ``dep_tracer_site/sitecustomize.py`` first and ``CORE_DEP_TRACER_CHILD_DIR``, and writes a
      log there at exit that the parent merges. A log counts for a spawn only when its parent
      process is the test process or an already counted child (the log names its pid and ppid).
    Only paths inside the repository are recorded, repository-relative; ``__pycache__``,
    ``.pytest_cache`` (as path components), ``*.pyc`` and the pytest base temp directory are
    dropped. Reads during the setup of a session-, package-, module- or class-scoped fixture go to
    a set kept per fixture that every test using the fixture depends on, including a fixture
    requested only through ``request.getfixturevalue``. Interpreter and plugin versions are not
    recorded here; the suite gate puts them in the key.

Import-time work belongs to the file whose top-level code did it
    A read, listing, probe or subprocess made while a repository file's top level runs (a
    ``Path(...).read_text()`` at module level, parametrize data read from a file, a conftest hook,
    a module loaded by path with ``spec_from_file_location``) is kept in a set per file, outside
    any test, whether it happens during collection, between tests, or in a lazy ``import`` inside
    a test (the module is then cached and a later test that imports it again sees no read). A
    test depends on the set of every file in its closure: the static import closure plus the
    files that closure's files executed at import (recorded as code edges from the executing file,
    which is what links a module loaded by path). Work done by pytest, pluggy or other code
    outside the repository, with no repository frame above it on the stack, has no owner and is
    dropped.

Always-rerun reason codes (``REASON_CODES``): whenever tracing is incomplete the record carries
    the codes below and the test is rerun whatever else matches. A non-Python child started
    without a ``cwd`` inherits the working directory, which under the suite gate is the
    repository root, so it also counts as touching the repository.

<reason-codes>

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
    of a session fixture that runs after the last test, a non-Python child started through a
    path the tracer cannot connect to the repository (a command that builds the repository path at
    run time), top-level reads of the conftest files that are imported before the plugin is
    registered (the conftest file itself is a dependency of every test below it, the data it reads
    at import is not), a ``fork`` made at import time, and a repository file that shadows a
    standard-library module are not seen.
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
TOOL_ID = 3  # CPython assigns 0, 1, 2 and 5 (debugger, coverage, profiler, optimizer); 3 and 4 are free
DEFAULT_ROOT = Path(__file__).resolve().parents[2]

REASON_CODES = {
    "non-python-subprocess": "a child that is not Python touches the repository (a script or binary of the repository, a path argument, a working directory inside it, an env value naming the repository); its reads are invisible",
    "git-repo": "git run against the repository",
    "child-env-dropped": "a Python child started with an env that lacks the injected variables, or with -I, -S or -E, so its sitecustomize does not run",
    "child-log-missing": "fewer child logs, linked to the test process through their ppid, than traced Python children (a killed child, os._exit)",
    "untraced-exec": "os.system on a command that touches the repository",
    "untraced-fork": "os.fork, a multiprocessing fork child or an os.spawn* child, none of which runs the child hooks",
    "mp-spawn": "multiprocessing started a helper process outside subprocess (a spawn or forkserver context, or the resource tracker), whose children are not counted",
    "repo-wide-listing": "the repository root was listed (os.walk of the repository, rglob, listdir of the root)",
    "sqlite3": "sqlite3.connect on a path inside the repository (the reads happen in C code)",
    "dynamic-import": "importlib.import_module or __import__ with a computed name in an executed file",
    "unanalysable-source": "an executed file did not parse, so its imports are unknown",
    "tracer-unavailable": "sys.monitoring is missing (Python < 3.12) or its tool id is taken",
    "tracer-error": "a tracer hook raised, or the record of the test could not be built",
}
__doc__ = __doc__.replace("<reason-codes>", "\n".join(f"    * ``{code}``: {text};" for code, text in REASON_CODES.items()))

ABSENT = "absent"
_IGNORED_PARTS = ("__pycache__", ".pytest_cache")
_PATH_FINDER_FILE = "<frozen importlib._bootstrap_external>"


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

    def merge(self, other: "DepSet", *, code: bool = True) -> None:
        if code:
            self.code |= other.code
            self.expected_children += other.expected_children
        for mine, theirs in ((self.files, other.files), (self.listings, other.listings), (self.probes, other.probes)):
            for key, value in theirs.items():
                mine.setdefault(key, value)
        self.reasons |= other.reasons


class _Local(threading.local):
    busy = False


_tl = _Local()
_current: DepSet | None = None
_import_time: dict[str, DepSet] = {}
_idle_sink = False
_machinery_dirs: tuple[str, ...] = ()
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


def set_machinery_dirs(dirs) -> None:
    """Directories of pytest and pluggy: work they do with no repository frame below them has no owner."""
    global _machinery_dirs
    _machinery_dirs = tuple(d.rstrip("/") + "/" for d in dirs)


def _owners(frame):
    """(nearest repository file, innermost repository file whose top level is running) on the stack.

    The walk goes outward from ``frame`` and stops at a pytest or pluggy frame, so work they do
    themselves (collecting directories, rewriting a test module) has no owner.
    """
    nearest = None
    while frame is not None:
        name = frame.f_code.co_filename
        if _machinery_dirs and name.startswith(_machinery_dirs):
            break
        if name[:1] != "<":
            found = _repo_abs(name)
            if found is not None:
                if nearest is None:
                    nearest = found[0]
                if frame.f_code.co_name == "<module>":
                    return nearest, found[0]
        frame = frame.f_back
    return nearest, None


def _targets(frame) -> list[DepSet]:
    """The sets an event raised below ``frame`` is recorded in: the running test's, and its owner's import-time set."""
    cur = _current
    if cur is None and not _idle_sink:
        return []
    nearest, body = _owners(frame)
    out = [cur] if cur is not None else []
    owner = body if cur is not None else (body or nearest)
    if owner is not None:
        out.append(_import_time.setdefault(owner, DepSet()))
    return out


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
            if rel.endswith(".pyc") or any(part in _IGNORED_PARTS for part in rel.split("/")):
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


def _python_skips_site(tokens) -> bool:
    """True when the interpreter options in ``tokens`` (-I, -S, -E) keep the injected sitecustomize from running."""
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token == "--" or token == "-" or not token.startswith("-"):
            return False
        i += 1
        if token.startswith("--"):
            continue
        for pos, letter in enumerate(token[1:], 1):
            if letter in "ISE":
                return True
            if letter in "cm":
                return False
            if letter in "WX":
                i += pos == len(token) - 1
                break
    return False


def _env_names_repo(env) -> bool:
    """True when an env passed explicitly carries a value, different from this process's, that names the repository."""
    if env is None:
        return False
    try:
        items = list(env.items())
    except Exception:
        return False
    for key, value in items:
        key, value = os.fsdecode(key), os.fsdecode(value)
        if value != os.environ.get(key) and any(root in value for root in _roots):
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
        injected = all(_env_get(env, name) for name in (CHILD_DIR_ENV, ROOT_ENV)) and _env_get(env, ENABLE_ENV) == "1"
        if injected and SITE_DIR in paths and not _python_skips_site(tokens):
            cur.expected_children += 1
        else:
            cur.reasons.add("child-env-dropped")
    elif _touches_repo(tokens, cwd) or _env_names_repo(env):
        cur.reasons.add("git-repo" if os.path.basename(exe) == "git" else "non-python-subprocess")


def _h_open(cur: DepSet, args, frame) -> None:
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


def _h_listing(cur: DepSet, args, frame) -> None:
    target = args[0] if args else None
    if target is None:
        target = os.getcwd()
    found = _repo_abs(target)
    if found is None or (frame is not None and frame.f_code.co_filename == _PATH_FINDER_FILE):
        return
    abs_path, rel = found
    if rel == ".":
        cur.reasons.add("repo-wide-listing")
    if rel not in cur.listings:
        cur.listings[rel] = listing_digest(abs_path)


def _h_popen(cur: DepSet, args, frame) -> None:
    executable, argv, cwd, env = args[:4]
    _note_spawn(cur, executable, argv, cwd, env)


def _h_posix_spawn(cur: DepSet, args, frame) -> None:
    path, argv, env = args[:3]
    _note_spawn(cur, path, argv, None, env)


def _h_system(cur: DepSet, args, frame) -> None:
    command = os.fsdecode(args[0])
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if _touches_repo(tokens or [command], None):
        cur.reasons.add("untraced-exec")


def _h_sqlite(cur: DepSet, args, frame) -> None:
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
    if handler is None or _tl.busy:
        return
    _tl.busy = True
    try:
        try:
            frame = sys._getframe(1)
        except ValueError:
            frame = None
        for target in _targets(frame):
            try:
                handler(target, args, frame)
            except Exception:
                target.reasons.add("tracer-error")
    except Exception:
        pass
    finally:
        _tl.busy = False


def _probing(real, follow):
    def probe(path, *args, **kwargs):
        if (_current is not None or _idle_sink) and not _tl.busy and not kwargs.get("dir_fd"):
            _tl.busy = True
            try:
                found = _repo_abs(path)
                if found is not None:
                    for target in _targets(sys._getframe(1)):
                        if found[1] not in target.probes and found[1] not in target.written:
                            target.probes[found[1]] = probe_digest(
                                found[0], follow and kwargs.get("follow_symlinks", True))
            except Exception:
                if _current is not None:
                    _current.reasons.add("tracer-error")
            finally:
                _tl.busy = False
        return real(path, *args, **kwargs)

    probe.__name__ = real.__name__
    probe.__doc__ = real.__doc__
    return probe


def _on_py_start(code, offset):
    name = code.co_filename
    cur = _current
    if cur is not None:
        if name.startswith(_root_slash):
            cur.code.add(name)
        elif name and name[0] != "/" and name[0] != "<":
            cur.code.add(os.path.join(os.getcwd(), name))
    elif _idle_sink and name.startswith(_root_slash):
        executing = sys._getframe(1)
        nearest, _ = _owners(executing.f_back)
        if nearest is not None and nearest != name:
            _import_time.setdefault(nearest, DepSet()).code.add(name)
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
    _wrap_multiprocessing_helpers()
    return _monitoring_ok


def _wrap_multiprocessing_helpers() -> None:
    """Flag ``mp-spawn``: multiprocessing starts spawn, forkserver and resource-tracker processes through
    ``util.spawnv_passfds``, which raises no ``subprocess.Popen`` event, so those children are not counted."""
    try:
        from multiprocessing import util
    except ImportError:
        return
    real = util.spawnv_passfds

    def spawnv_passfds(path, args, passfds):
        for target in _targets(sys._getframe(1)):
            target.reasons.add("mp-spawn")
        return real(path, args, passfds)

    util.spawnv_passfds = spawnv_passfds


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
_resolved: dict[tuple, tuple[tuple[str, ...], tuple[str, ...]]] = {}


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


def _resolve(name: str, dirs: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(repository files executed by importing the dotted ``name``, paths probed on the way without a hit).

    ``dirs`` are searched in order. The probed paths are the candidates passed over before the hit, or
    all of them when nothing was found; a name of the standard library that no repository file
    provides records none.
    """
    key = (name, dirs)
    hit = _resolved.get(key)
    if hit is not None:
        return hit
    parts = name.split(".") if name else []
    result: tuple[str, ...] = ()
    probed: list[str] = []
    for directory in dirs:
        found: list[str] = []
        current = directory
        for part in parts:
            pkg = os.path.join(current, part)
            init = os.path.join(pkg, "__init__.py")
            if os.path.isfile(init):
                found.append(init)
                current = pkg
            elif os.path.isfile(pkg + ".py"):
                probed.append(init)
                found.append(pkg + ".py")
                break
            elif os.path.isdir(pkg):
                probed.extend((init, pkg + ".py", pkg))
                current = pkg
            else:
                probed.extend((init, pkg + ".py", pkg))
                break
        if found:
            result = tuple(found)
            break
    if not result and parts and parts[0] in sys.stdlib_module_names:
        probed = []
    answer = (result, tuple(dict.fromkeys(probed)))
    _resolved[key] = answer
    return answer


def _edges(path: str, search: tuple[str, ...]):
    """(imported repository files, candidate paths probed without a hit, reason or None) of one file."""
    digest = file_digest(path)
    imports, dynamic = _parse_imports(path, digest)
    if dynamic is None:
        return (), (), "unanalysable-source"
    here = os.path.dirname(path)
    out: list[str] = []
    probed: list[str] = []
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
        for wanted in (module, *(f"{module}.{name}" if module else name for name in names)):
            files, misses = _resolve(wanted, dirs)
            out.extend(files)
            probed.extend(misses)
    return tuple(out), tuple(probed), ("dynamic-import" if dynamic else None)


def search_dirs() -> tuple[str, ...]:
    """Directories of ``sys.path`` inside the repository, in order."""
    found = []
    for entry in sys.path:
        hit = _repo_abs(entry or os.getcwd())
        if hit is not None and hit[0] not in found:
            found.append(hit[0])
    return tuple(found)


def import_closure(roots, search: tuple[str, ...] | None = None, extra=None):
    """(files, reasons, probed): ``roots`` plus every repository file they import, transitively.

    ``extra(path)`` may name further files that ``path`` executed. ``probed`` are the candidate
    paths the import search tried without a hit.
    """
    search = search_dirs() if search is None else search
    seen: set[str] = set()
    reasons: set[str] = set()
    probed: dict[str, None] = {}
    queue = list(roots)
    with _quiet():
        while queue:
            path = queue.pop()
            if path in seen:
                continue
            seen.add(path)
            edges, misses, reason = _edges(path, search)
            if reason:
                reasons.add(reason)
            probed.update(dict.fromkeys(misses))
            queue.extend(e for e in edges if e not in seen)
            if extra is not None:
                queue.extend(e for e in extra(path) if e not in seen)
    return seen, reasons, list(probed)


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

    parent = os.getppid()

    def write_log() -> None:
        deactivate()
        payload = {
            "pid": os.getpid(), "ppid": parent,
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


def _read_child_logs(child_dir: str, root_abs: str, into: DepSet, own_pid: int) -> int:
    """Merge the child logs of one test into ``into``; return how many of them descend from ``own_pid``.

    A log whose parent chain does not reach the test process (a Python started through a shell,
    say) still contributes its reads but cannot stand in for a counted child that wrote none.
    """
    try:
        names = os.listdir(child_dir)
    except OSError:
        return 0
    parsed: list[tuple[int, int, DepSet]] = []
    for name in sorted(names):
        try:
            with open(os.path.join(child_dir, name)) as handle:
                data = json.load(handle)
            part = DepSet()
            part.code = {os.path.join(root_abs, c) if not os.path.isabs(c) else c for c in data["code"]}
            part.files, part.listings, part.probes = dict(data["files"]), dict(data["listings"]), dict(data["probes"])
            part.reasons = set(data["reasons"])
            part.expected_children = int(data["expected_children"])
            parsed.append((int(data["pid"]), int(data["ppid"]), part))
        except (OSError, KeyError, TypeError, ValueError):
            into.reasons.add("child-log-missing")
    linked = {own_pid}
    grew = True
    while grew:
        grew = False
        for pid, ppid, _ in parsed:
            if ppid in linked and pid not in linked:
                linked.add(pid)
                grew = True
    for pid, _, part in parsed:
        if pid not in linked:
            part.expected_children = 0
        into.merge(part)
    shutil.rmtree(child_dir, ignore_errors=True)
    return sum(1 for pid, _, _ in parsed if pid in linked)


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

        @pytest.hookimpl(trylast=True)
        def pytest_configure(self, config):
            global _idle_sink
            os.environ[ROOT_ENV] = self.root
            os.environ[DIR_ENV] = str(self.directory)
            self.directory.mkdir(parents=True, exist_ok=True)
            is_worker = "PYTEST_XDIST_WORKER" in os.environ
            controller = not is_worker and (
                config.getoption("numprocesses", None) not in (None, 0) or config.pluginmanager.hasplugin("dsession"))
            if not is_worker:
                for stale in list(self.directory.glob("gw*.jsonl")) + list(self.directory.glob("main.jsonl")):
                    stale.unlink()
                shutil.rmtree(self.directory / "children", ignore_errors=True)
            if controller:
                return
            self.traces = True
            basetemp = config.getoption("basetemp", None)
            ignored = [str(Path(basetemp).resolve())] if basetemp else []
            import _pytest
            import pluggy
            set_machinery_dirs([os.path.dirname(_pytest.__file__), os.path.dirname(pluggy.__file__)])
            install(self.root)
            set_ignored_prefixes(ignored)
            _idle_sink = True
            self.writer = _Writer(self.directory / f"{self.worker}.jsonl")

        def pytest_unconfigure(self, config):
            if self.writer is not None:
                self.writer.close()

        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            if not self.traces:
                return (yield)
            try:
                self._begin()
            except Exception:
                self._end_scope()
                self.cur.reasons.add("tracer-error")
            try:
                return (yield)
            finally:
                self._finish(item)

        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_teardown(self, item, nextitem):
            if self.traces:
                request = getattr(item, "_request", None)
                self.dynamic_defs = list(getattr(request, "_fixture_defs", {}).values()) if request else []
            return (yield)

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
            self.dynamic_defs = []
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

        def _end_scope(self):
            deactivate()
            for key, value in self.saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self.saved_env = {}

        def _fixture_sets_for(self, item):
            try:
                defs = {d for group in item._fixtureinfo.name2fixturedefs.values() for d in group}
            except AttributeError:
                return list(self.fixture_sets.values())
            defs.update(self.dynamic_defs)
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
            self._end_scope()
            with _quiet():
                try:
                    self._record(item, cur)
                except Exception:
                    failed = DepSet()
                    failed.reasons.add("tracer-error")
                    try:
                        self.writer.test(item.nodeid, {}, failed)
                    except Exception:
                        pass

        def _record(self, item, cur: DepSet) -> None:
            merged = DepSet()
            merged.merge(cur)
            for scoped in self._fixture_sets_for(item):
                merged.merge(scoped)
            logs = _read_child_logs(self.child_dir, self.root, merged, os.getpid())
            if logs < merged.expected_children:
                merged.reasons.add("child-log-missing")
            merged.code.update(self._static_roots(item))
            located = (_repo_abs(c) for c in merged.code)
            code_files = {found[0] for found in located if found is not None and found[1] != "."}
            closure, reasons, absent = import_closure(
                code_files, extra=lambda path: _import_time[path].code if path in _import_time else ())
            merged.reasons |= reasons
            for path in closure:
                if path in _import_time:
                    merged.merge(_import_time[path], code=False)
            for path in absent:
                found = _repo_abs(path)
                if found is not None:
                    merged.probes.setdefault(found[1], probe_digest(found[0]))
            files = dict(merged.files)
            for path in closure | code_files:
                found = _repo_abs(path)
                if found is not None and found[1] not in files:
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
