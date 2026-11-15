"""Content-keyed store of passing test results, for the suite gate (R3 of the landing-speedup plan).

Difficulty removed: a gate run executes every test although most of them depend on files a change
does not touch. The dependency tracer (``scripts/tests/dep_tracer.py``) records what each test read;
this store keeps a PASS together with that list and the gate reuses it when nothing the test read, nor
anything in the environment, has changed.

Key of a stored result: sha256 over the test id; for each recorded dependency its kind, repository-relative
path and content hash (a listed directory by its name set, a probed path by its state); the interpreter
version (``sys.version_info``); the name and version of every distribution importable under the suite's
environment (pytest and pytest-xdist among them); and the git version. ``compute_key`` is pure.

What is stored. ``put`` takes the tracer records of a run in a clean sandbox clone of a committed
candidate; the gate gives it only tests the report shows passing in a full run, and never one the tracer
marked always-rerun (an incompletely traced test). A FAIL is therefore never stored. A test with no
stored pass, or whose recomputed key matches no stored row, always runs.

A row keeps its dependency list as ``[path id, digest id]`` pairs: a path and a digest are stored once
in their own tables, however many rows name them (stored inline, the lists of one full run were
~700 MB, over the store's bound, and the bound's trim then dropped two thirds of the passes).

Lookup. ``lookup`` recomputes, for each wanted test, the key of every stored row (at most
``ROWS_PER_TEST``, newest first) from the dependency list of that row and the *current* contents under
the candidate's sandbox, and reports a hit when it equals the stored key. Equal keys mean identical
inputs, so trying older rows too is sound.

Where it lives. ``<git common dir>/suite-gate/results/store/results.sqlite`` (sqlite3, WAL): the
directory ``results`` is registered with ``gate_retention`` and its one entry ``store`` is evicted as a
whole, least recently used first, within the 2048 MB bound shared with the other kept files. A run
that has the store open when it is evicted keeps writing to the unlinked file, which costs reuse and
never a verdict. ``CORE_RESULT_STORE`` names another database file; the controls use it so the live
store, which the real gate protecting main reads and writes, is never touched by an experiment.

Environment fingerprint. Distributions are listed by a child interpreter started under the suite's
own environment (``fingerprint``), so a distribution on the suite's ``PYTHONPATH`` or user site is
seen exactly as pytest sees it.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import zlib
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from . import gate_retention

STORE_ENV = "CORE_RESULT_STORE"
RESULTS_DIR = f"{gate_retention.STORE_DIR}/results"
STORE_ENTRY = "store"
DB_NAME = "results.sqlite"
ROWS_PER_TEST = 3
MAX_STORE_BYTES = 512 * 1024 * 1024
SCHEMA_VERSION = 2
BUSY_TIMEOUT_S = 60
FILE, LISTING, PROBE = "file", "listing", "probe"
KINDS = (FILE, LISTING, PROBE)
SOURCE_RAN, SOURCE_REUSED = "ran", "reused"
ALWAYS_RERUN_META, FAILED_META = "always_rerun", "failed"  # ids the last settled full run could not store, by reason

Dep = tuple[str, str, str]  # (kind, repository-relative path, digest)

FINGERPRINT_CODE = (
    "import importlib.metadata as m, json, subprocess\n"
    "dists = sorted({(str(d.metadata['Name']), str(d.version)) for d in m.distributions()\n"
    "                if d.metadata['Name']})\n"
    "try:\n"
    "    git = subprocess.run(['git', '--version'], capture_output=True, text=True).stdout.strip()\n"
    "except OSError:\n"
    "    git = ''\n"
    "print(json.dumps({'dists': dists, 'git': git}))\n"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coord (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL, UNIQUE (kind, path));
CREATE TABLE IF NOT EXISTS digest (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS result (
    id INTEGER PRIMARY KEY,
    test_id TEXT NOT NULL,
    key TEXT NOT NULL,
    env TEXT NOT NULL,
    deps BLOB NOT NULL,
    candidate TEXT NOT NULL,
    stored_at REAL NOT NULL,
    last_used REAL NOT NULL,
    UNIQUE (test_id, key));
CREATE INDEX IF NOT EXISTS result_by_test ON result (test_id, stored_at);
CREATE INDEX IF NOT EXISTS result_by_use ON result (last_used);
"""


@dataclasses.dataclass(frozen=True)
class Environment:
    """What outside the repository decides whether a stored pass still holds."""
    interpreter: str
    distributions: tuple[tuple[str, str], ...]
    git: str

    @property
    def digest(self) -> str:
        return _sha(["environment", self.interpreter, [list(d) for d in self.distributions], self.git])


@dataclasses.dataclass(frozen=True)
class Hit:
    candidate_sha: str
    key: str


def _sha(parts) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def current_interpreter() -> str:
    """The interpreter component of the key, read from ``sys.version_info`` at call time."""
    return f"{sys.implementation.name} {'.'.join(str(part) for part in tuple(sys.version_info))}"


def fingerprint(env: Mapping[str, str] | None = None, timeout_s: int = 120) -> dict:
    """``{"dists": [[name, version], ...], "git": "git version ..."}`` as a child under ``env`` sees them."""
    proc = subprocess.run([sys.executable, "-P", "-c", FINGERPRINT_CODE], capture_output=True, text=True,
                          env=dict(os.environ if env is None else env), timeout=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError(f"cannot fingerprint the environment: {proc.stderr.strip()[-300:]}")
    return json.loads(proc.stdout)


def current_environment(fingerprint_: Mapping | None = None, env: Mapping[str, str] | None = None) -> Environment:
    found = fingerprint_ if fingerprint_ is not None else fingerprint(env)
    return Environment(current_interpreter(), tuple((str(n), str(v)) for n, v in found["dists"]), str(found["git"]))


def compute_key(test_id: str, deps: Iterable[Dep], environment: Environment) -> str:
    ordered = sorted((kind, path, digest) for kind, path, digest in deps)
    return _sha(["result", test_id, ordered, environment.digest])


def deps_of(record: Mapping) -> list[Dep]:
    """The dependency list of one tracer record (``dep_tracer.load_records`` shape), sorted."""
    deps = [(FILE, path, digest) for path, digest in record["files"].items()]
    deps += [(LISTING, path, digest) for path, digest in record["listings"].items()]
    deps += [(PROBE, path, digest) for path, digest in record["probes"].items()]
    return sorted(deps)


def _tracer():
    """scripts/tests/dep_tracer.py, whose digest functions define what a recorded digest means."""
    cached = sys.modules.get("result_store_dep_tracer")
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parents[1] / "tests" / "dep_tracer.py"
    spec = importlib.util.spec_from_file_location("result_store_dep_tracer", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["result_store_dep_tracer"] = module
    spec.loader.exec_module(module)
    return module


def tree_resolver(core_root: str | Path) -> Callable[[str, str], str]:
    """``resolve(kind, path)``: the digest ``path`` (repository-relative) has now under ``core_root``."""
    tracer = _tracer()
    functions = {FILE: tracer.file_digest, LISTING: tracer.listing_digest, PROBE: tracer.probe_digest}
    root = os.fspath(core_root)

    def resolve(kind: str, path: str) -> str:
        return functions[kind](os.path.join(root, path))
    return resolve


def results_dir(common_dir: Path) -> Path:
    return Path(common_dir) / RESULTS_DIR


def store_path(common_dir: Path) -> Path:
    """The database of the gate: ``CORE_RESULT_STORE`` when set, else the live store."""
    override = os.environ.get(STORE_ENV)
    return Path(override) if override else results_dir(common_dir) / STORE_ENTRY / DB_NAME


class ResultStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._drop_other_schema()
        self._db.executescript(_SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        self._coord_by_pair: dict[tuple[str, str], int] = {}
        self._pair_by_coord: dict[int, tuple[str, str]] = {}
        self._digest_by_value: dict[str, int] = {}
        self._value_by_digest: dict[int, str] = {}

    def _drop_other_schema(self) -> None:
        """A database of another schema version holds results in a layout this code cannot read: it starts empty."""
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone() is None:
            return
        row = self._db.execute("SELECT value FROM meta WHERE name='schema'").fetchone()
        if row is not None and row[0] == str(SCHEMA_VERSION):
            return
        for table in ("result", "coord", "digest", "meta"):
            self._db.execute(f"DROP TABLE IF EXISTS {table}")
        self._db.execute("VACUUM")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "ResultStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _interned(self, deps: Sequence[Dep]) -> list[list[int]]:
        """Each dependency as ``[coordinate id, digest id]``: a path and a digest are kept once, however many rows name them."""
        ids = []
        for kind, path, digest in deps:
            coord = self._coord_by_pair.get((kind, path))
            if coord is None:
                self._db.execute("INSERT OR IGNORE INTO coord (kind, path) VALUES (?, ?)", (kind, path))
                coord = self._db.execute("SELECT id FROM coord WHERE kind=? AND path=?", (kind, path)).fetchone()[0]
                self._coord_by_pair[(kind, path)] = coord
            value = self._digest_by_value.get(digest)
            if value is None:
                self._db.execute("INSERT OR IGNORE INTO digest (value) VALUES (?)", (digest,))
                value = self._db.execute("SELECT id FROM digest WHERE value=?", (digest,)).fetchone()[0]
                self._digest_by_value[digest] = value
            ids.append([coord, value])
        return ids

    def put(self, candidate_sha: str, environment: Environment,
            results: Mapping[str, Sequence[Dep]], now: float | None = None) -> int:
        """Store the passes of one run; returns how many rows were written. The caller filters (PASS only)."""
        stamp = time.time() if now is None else now
        written = 0
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for test_id, deps in results.items():
                ordered = sorted(deps)
                key = compute_key(test_id, ordered, environment)
                blob = zlib.compress(json.dumps(self._interned(ordered), separators=(",", ":")).encode())
                self._db.execute(
                    "INSERT INTO result (test_id, key, env, deps, candidate, stored_at, last_used) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (test_id, key) DO UPDATE SET "
                    "candidate=excluded.candidate, stored_at=excluded.stored_at, last_used=excluded.last_used",
                    (test_id, key, environment.digest, blob, candidate_sha, stamp, stamp))
                written += 1
                self._db.execute(
                    "DELETE FROM result WHERE test_id=? AND id NOT IN "
                    "(SELECT id FROM result WHERE test_id=? ORDER BY stored_at DESC, id DESC LIMIT ?)",
                    (test_id, test_id, ROWS_PER_TEST))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return written

    def _rows(self, test_id: str) -> list[tuple[str, str, bytes]]:
        return self._db.execute(
            "SELECT key, candidate, deps FROM result WHERE test_id=? ORDER BY stored_at DESC, id DESC LIMIT ?",
            (test_id, ROWS_PER_TEST)).fetchall()

    def stored_deps(self, blob: bytes) -> list[Dep]:
        found = []
        for coord, digest in json.loads(zlib.decompress(blob)):
            if coord not in self._pair_by_coord:
                self._pair_by_coord.update(
                    {row[0]: (row[1], row[2]) for row in self._db.execute("SELECT id, kind, path FROM coord")})
            if digest not in self._value_by_digest:
                self._value_by_digest.update(row for row in self._db.execute("SELECT id, value FROM digest"))
            kind, path = self._pair_by_coord[coord]
            found.append((kind, path, self._value_by_digest[digest]))
        return found

    def lookup(self, test_ids: Iterable[str], environment: Environment,
               resolve: Callable[[str, str], str]) -> dict[str, Hit]:
        """The tests whose stored row still has its key: the stored dependencies at their current digests."""
        hits: dict[str, Hit] = {}
        current: dict[tuple[str, str], str] = {}
        for test_id in test_ids:
            for key, candidate, blob in self._rows(test_id):
                nowdeps = []
                for kind, path, _old in self.stored_deps(blob):
                    if (kind, path) not in current:
                        current[(kind, path)] = resolve(kind, path)
                    nowdeps.append((kind, path, current[(kind, path)]))
                if compute_key(test_id, nowdeps, environment) == key:
                    hits[test_id] = Hit(candidate, key)
                    break
        if hits:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.executemany("UPDATE result SET last_used=? WHERE test_id=? AND key=?",
                                 [(time.time(), t, h.key) for t, h in hits.items()])
            self._db.execute("COMMIT")
            gate_retention.touch(self.path)
        return hits

    def set_meta(self, name: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (name, value))

    def get_meta(self, name: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM result").fetchone()[0]

    def test_ids(self) -> list[str]:
        return [row[0] for row in self._db.execute("SELECT DISTINCT test_id FROM result ORDER BY test_id")]

    def rows_of(self, test_id: str) -> list[dict]:
        return [{"key": key, "candidate": candidate, "deps": self.stored_deps(blob)}
                for key, candidate, blob in self._rows(test_id)]

    def replace_deps(self, test_id: str, deps: Sequence[Dep], environment: Environment) -> None:
        """Rewrite the newest row of ``test_id`` to stand for ``deps`` (a control forges what a blind tracer stored)."""
        row = self._db.execute(
            "SELECT id, candidate, stored_at FROM result WHERE test_id=? ORDER BY stored_at DESC, id DESC LIMIT 1",
            (test_id,)).fetchone()
        if row is None:
            raise KeyError(test_id)
        self._db.execute("DELETE FROM result WHERE id=?", (row[0],))
        self.put(row[1], environment, {test_id: deps}, now=row[2])

    def trim(self, max_bytes: int = MAX_STORE_BYTES) -> int:
        """Drop the least recently used rows until the database is at most ``max_bytes``; returns rows dropped."""
        dropped = 0
        while self.size_bytes() > max_bytes and self.count():
            cut = max(1, self.count() // 10)
            self._db.execute("DELETE FROM result WHERE id IN (SELECT id FROM result ORDER BY last_used LIMIT ?)",
                             (cut,))
            dropped += cut
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.execute("VACUUM")
        return dropped

    def size_bytes(self) -> int:
        return sum(candidate.stat().st_size for suffix in ("", "-wal", "-shm")
                   if (candidate := Path(f"{self.path}{suffix}")).exists())


def copy_store(source: str | Path, target: str | Path) -> None:
    """Copy a database file with its content flushed (a private store for a control)."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not Path(source).is_file():
        return
    with contextlib.closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_S)) as src, \
            contextlib.closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def evict(child: Path) -> None:
    """``gate_retention`` eviction of the one entry of the registered directory: the whole store."""
    shutil.rmtree(child, ignore_errors=True)


def register() -> None:
    gate_retention.register_store("results", results_dir, evict)


register()
