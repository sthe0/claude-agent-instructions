"""What a suite-gate run leaves behind: sandboxes removed on their owner's death, the kept store bounded.

Difficulty removed: the gate removed its sandbox in a ``finally``, which a SIGKILL or a cap-run timeout
skips, and nothing bounded what the gate keeps (baselines, per-run files). A killed run left a
repository-sized sandbox in a shared /tmp for good.

Sandboxes. Every gate sandbox has an owner file ``<sandbox>.owner`` beside it, named like the sandbox,
on which its creator holds an ``flock`` for the sandbox's whole life; the kernel releases it when the
holder dies, however it dies, so a free lock means a dead owner. (The owner file sits beside the root
because ``instruction-sandbox.sh`` accepts only an empty root.) A run, before it builds its own
sandbox, removes every sandbox of the gate's own naming whose owner file it can lock; it never judges by
age, and never removes a sandbox whose lock is held. A sandbox without an owner file (made by the code
before this module, or by another tool) is removed only when old, as below. The owner file is created under a temporary name, locked, and only then
renamed to ``<sandbox>.owner``, so an owner file a sweeper can see is always already locked, whichever
repository's run sweeps (the temp directory is shared by all repositories). Accepted residual: a creator
killed between making the directory and the rename leaves a ``.owner.creating`` file (and, if it died
before that, nothing but the directory): the sweep removes a ``.creating`` file whose lock is free and
that is older than a minute, with its directory. A directory with neither file, of this user, older than
``suite-gate-timeout-s`` (a gate run lives no longer) is a leftover of the code before this module and is
removed too; a younger one may belong to a session still on the old gate.

A removal that fails (a read-only or ``chmod 000`` directory is opened up and retried) keeps the owner
file, unlocked, so the next sweep retries, and warns.

Failing runs keep their evidence (log, JUnit XML, failure list, the sandbox's core tree without its
tmp/) under ``<common dir>/suite-gate/evidence/<candidate>/`` before the sandbox goes.

Retention. After a run, ``enforce`` keeps ``suite-gate-failure-evidence-runs`` newest failing runs'
evidence and evicts the least recently used of everything else evictable (the tip baselines, older
evidence, the files kept per candidate run, and every store registered with ``register_store``) until
the kept store is at most ``suite-gate-keep-max-mb``. The bound wins: the newest failing runs' evidence
goes last, oldest first, and only when nothing else is left and the total still exceeds the bound. The
files of the candidate and of the tip being decided (record, log, JUnit XML, per-test report) are never
evicted; any other run's are unless used less than ``suite-gate-timeout-s`` ago (another session's
landing may be reading that record), and evicting another baseline only forces its recomputation. A group of
files is evicted only while holding its ``.lock`` exclusively (a run holding it keeps its files) and
only if no file of it is newer than when it was listed; lock files themselves are never unlinked, since
unlinking one lets two runs hold "the" lock on different inodes. A store still over the bound when
nothing evictable is left says so on stderr. Last use is the newest mtime among an entry's files (a
baseline cache hit touches its record). Partial evidence copies of dead processes, or older than
``suite-gate-timeout-s``, are removed.

``SUITE_GATE_KEEP_SANDBOX=1`` leaves the sandbox of that run in place (for inspection, and for the
negative control of the disk series); the next run's sweep removes it, its lock being free by then.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Collection, Iterator

STORE_DIR = "suite-gate"
RETENTION_LOCK = f"{STORE_DIR}/retention.lock"
EVIDENCE_DIR = f"{STORE_DIR}/evidence"
BASELINE_DIR = f"{STORE_DIR}/baseline"
OWNER_SUFFIX = ".owner"
PENDING_SUFFIX = ".creating"
PENDING_MIN_AGE_S = 60
KEEP_ENV = "SUITE_GATE_KEEP_SANDBOX"
EVIDENCE_TMP_PREFIX = ".tmp-"

KEEP_MAX_KEY = "suite-gate-keep-max-mb"
EVIDENCE_RUNS_KEY = "suite-gate-failure-evidence-runs"
TIMEOUT_KEY = "suite-gate-timeout-s"
DEFAULT_KEEP_MAX_MB = 2048  # config.md: suite-gate-keep-max-mb
DEFAULT_EVIDENCE_RUNS = 3  # config.md: suite-gate-failure-evidence-runs
DEFAULT_TIMEOUT_S = 7200  # config.md: suite-gate-timeout-s
MB = 1024 * 1024


# ── configuration ─────────────────────────────────────────────────────────

def _configured(key: str, default: int, config_path: str | Path | None) -> int:
    try:
        scripts = str(Path(__file__).resolve().parents[1])
        if scripts not in sys.path:
            sys.path.append(scripts)
        from agentctl.config import parse_config_md

        value = int(parse_config_md(Path(config_path) if config_path else None).get(key, ""))
    except (ImportError, OSError, ValueError):
        return default
    return value if value > 0 else default


def keep_max_mb(config_path: str | Path | None = None) -> int:
    return _configured(KEEP_MAX_KEY, DEFAULT_KEEP_MAX_MB, config_path)


def evidence_runs(config_path: str | Path | None = None) -> int:
    return _configured(EVIDENCE_RUNS_KEY, DEFAULT_EVIDENCE_RUNS, config_path)


# ── sandboxes: owner lock, sweep ──────────────────────────────────────────

@contextlib.contextmanager
def _locked(path: Path, mode: int) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, mode)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def owner_path(root: Path) -> Path:
    return root.with_name(root.name + OWNER_SUFFIX)


_UNLINKERS = (os.unlink, os.rmdir, os.remove)


def _make_writable(function: Callable, path: str, _error: object, retry: bool = True) -> None:
    """rmtree's error handler: a directory without write or search permission blocks its children; retry once.

    A failed unlink/rmdir is repeated once its parent is writable. A failure to open or list a directory
    (``chmod 000``) is answered by opening the directory up and removing its tree again, once. Never raises:
    a removal that cannot be finished is reported by the caller's check that the root is gone."""
    try:
        for target in (os.path.dirname(path), path):
            with contextlib.suppress(OSError):
                mode = os.lstat(target).st_mode
                if stat.S_ISDIR(mode):
                    os.chmod(target, mode | 0o700)
        if function in _UNLINKERS:
            function(path)
        elif retry:
            _remove_tree(Path(path), retry=False)
    except Exception:
        pass


def _remove_tree(root: Path, retry: bool = True) -> None:
    def handler(function, path, error):
        _make_writable(function, path, error, retry)

    if sys.version_info >= (3, 12):
        shutil.rmtree(root, onexc=handler)
    else:
        shutil.rmtree(root, onerror=handler)


def _remove_sandbox(root: Path, owner: Path) -> bool:
    """Remove ``root`` and then its owner file; a root that stays keeps the owner file (and says so)."""
    if root.is_dir() and not root.is_symlink():
        _remove_tree(root)
        if root.exists():
            print(f"suite-gate: cannot remove the sandbox {root}; the next run retries", file=sys.stderr)
            return False
    owner.unlink(missing_ok=True)
    return True


@contextlib.contextmanager
def sandbox(parent: str | Path, prefix: str) -> Iterator[Path]:
    """A fresh sandbox directory under ``parent`` whose owner lock is held until the block ends.

    On exit the directory and its owner file are removed, unless ``SUITE_GATE_KEEP_SANDBOX`` is set."""
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    owner = owner_path(root)
    pending = owner.with_name(owner.name + PENDING_SUFFIX)
    fd = -1
    try:
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.rename(pending, owner)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        pending.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)
        raise
    try:
        yield root
    finally:
        try:
            if not os.environ.get(KEEP_ENV):
                _remove_sandbox(root, owner)
        finally:
            os.close(fd)


def _sweep_one(entry: os.DirEntry, root: Path) -> bool:
    """Remove ``root`` and its marker file ``entry`` when the marker is ours and its lock is free."""
    try:
        info = entry.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return False
        fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.stat(entry.path).st_ino != os.fstat(fd).st_ino:
                return False
        except (BlockingIOError, OSError):
            return False
        return _remove_sandbox(root, Path(entry.path))
    finally:
        os.close(fd)


def _ownerless_root_is_stale(entry: os.DirEntry, names: Collection[str], limit_s: float, now: float) -> bool:
    if entry.name + OWNER_SUFFIX in names or entry.name + OWNER_SUFFIX + PENDING_SUFFIX in names:
        return False
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and now - info.st_mtime > limit_s


def sweep_dead_sandboxes(parent: str | Path, prefix: str, *, ownerless_after_s: float | None = None,
                         now: float | None = None) -> list[str]:
    """Remove the dead sandboxes of this user under ``parent``; returns their names.

    Dead means: an owner file whose lock is free; a ``.owner.creating`` file whose lock is free and that is
    older than ``PENDING_MIN_AGE_S`` (its creator died before the rename); or, for the sandboxes of the code
    before the owner file, a directory with neither file whose mtime is older than ``ownerless_after_s``
    (default ``suite-gate-timeout-s``: a gate run lives at most that long). A held lock, a sandbox with an
    owner file the sweeper cannot lock, and another user's files stay."""
    removed: list[str] = []
    limit_s = ownerless_after_s if ownerless_after_s is not None else _configured(TIMEOUT_KEY, DEFAULT_TIMEOUT_S, None)
    clock = time.time() if now is None else now
    try:
        entries = sorted(os.scandir(parent), key=lambda e: e.name)
    except OSError:
        return removed
    names = {e.name for e in entries}
    for entry in entries:
        name = entry.name
        if not name.startswith(prefix):
            continue
        if name.endswith(OWNER_SUFFIX):
            stem = name[: -len(OWNER_SUFFIX)]
            dead = len(stem) > len(prefix) and _sweep_one(entry, Path(parent) / stem)
        elif name.endswith(OWNER_SUFFIX + PENDING_SUFFIX):
            stem = name[: -len(OWNER_SUFFIX + PENDING_SUFFIX)]
            dead = (len(stem) > len(prefix) and _old_enough(entry, PENDING_MIN_AGE_S, clock)
                    and _sweep_one(entry, Path(parent) / stem))
        else:
            stem = name
            dead = (_ownerless_root_is_stale(entry, names, limit_s, clock)
                    and _remove_sandbox(Path(entry.path), Path(entry.path + OWNER_SUFFIX)))
        if dead:
            removed.append(stem)
    return removed


def _old_enough(entry: os.DirEntry, age_s: float, now: float) -> bool:
    try:
        return now - entry.stat(follow_symlinks=False).st_mtime > age_s
    except OSError:
        return False


# ── failing runs keep their evidence ──────────────────────────────────────

def keep_failure_evidence(common_dir: str | Path, candidate_sha: str, *, log_tail: str, junit_path: str,
                          failures: Collection[str], core_tree: Path) -> Path | None:
    """Copy a failing run's log, JUnit XML, failure list and core tree into the evidence directory.

    Best effort: evidence that cannot be copied (disk full) costs the evidence, never the verdict."""
    target = Path(common_dir) / EVIDENCE_DIR / candidate_sha
    partial = target.with_name(f"{EVIDENCE_TMP_PREFIX}{candidate_sha}-{os.getpid()}")
    try:
        partial.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir()
        (partial / "log.txt").write_text(log_tail, encoding="utf-8")
        (partial / "failures.txt").write_text("".join(f"{i}\n" for i in sorted(failures)), encoding="utf-8")
        if junit_path and Path(junit_path).is_file():
            shutil.copyfile(junit_path, partial / "junit.xml")
        if core_tree.is_dir():
            shutil.copytree(core_tree, partial / "core", symlinks=True, ignore_dangling_symlinks=True,
                            ignore=lambda directory, names: ["tmp"] if Path(directory) == core_tree and "tmp" in names else [])
        shutil.rmtree(target, ignore_errors=True)
        partial.rename(target)
    except OSError as exc:
        shutil.rmtree(partial, ignore_errors=True)
        print(f"suite-gate: cannot keep the evidence of {candidate_sha[:12]}: {exc}", file=sys.stderr)
        return None
    return target


# ── retention ─────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Store:
    """A kept store registered for eviction: ``path`` (or ``path(common_dir)``) is a directory whose direct
    children are its entries; ``evict(child)`` removes one."""
    name: str
    path: Path | Callable[[Path], Path]
    evict: Callable[[Path], None]


_STORES: dict[str, Store] = {}


def register_store(name: str, path: Path | Callable[[Path], Path], evict: Callable[[Path], None]) -> None:
    _STORES[name] = Store(name, path, evict)


def unregister_store(name: str) -> None:
    _STORES.pop(name, None)


@dataclasses.dataclass(frozen=True)
class Entry:
    kind: str
    name: str
    size: int
    last_used: float
    protected: bool
    evict: Callable[[], bool | None]


@dataclasses.dataclass(frozen=True)
class Retained:
    before_mb: float
    after_mb: float
    evicted: tuple[str, ...]
    over_bound: bool = False


def tree_bytes(path: Path) -> int:
    """Disk blocks used under ``path`` (symlinks not followed), as ``du`` counts them."""
    try:
        info = os.lstat(path)
    except OSError:
        return 0
    total = info.st_blocks * 512
    if stat.S_ISDIR(info.st_mode):
        try:
            children = list(os.scandir(path))
        except OSError:
            return total
        total += sum(tree_bytes(Path(child.path)) for child in children)
    return total


def _mtime(path: Path) -> float:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return 0.0


def _newest_mtime(path: Path) -> float:
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        return 0.0
    if path.is_dir() and not path.is_symlink():
        for child in os.scandir(path):
            newest = max(newest, _newest_mtime(Path(child.path)))
    return newest


def touch(path: str | Path) -> None:
    """Mark a kept file as just used; a file that is gone is not an error."""
    with contextlib.suppress(OSError):
        os.utime(path)


def _evict_group(lock: Path, files: list[Path], listed_at: float) -> bool:
    """Remove ``files`` (never the lock itself) while holding ``lock``; False when it was not safe.

    Not safe: a run holds the lock (it is building or reading these files), or a file is newer than when
    the group was listed (a run finished since). The lock file stays, so every run keeps locking the one
    inode. A group without a lock file (a waiver record) has no run in progress, since a run makes its
    lock before its files; the lock is not created here, so no empty one appears."""
    try:
        fd = os.open(lock, os.O_RDWR)
    except FileNotFoundError:
        fd = -1
    except OSError:
        return False
    try:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        if any(_newest_mtime(file) > listed_at for file in files):
            return False
        for file in files:
            _remove_path(file)
        return True
    finally:
        if fd >= 0:
            os.close(fd)


def _sha_groups(directory: Path) -> dict[str, list[Path]]:
    """The files of ``directory`` named ``<40-hex sha>.<suffix>``, grouped by sha."""
    groups: dict[str, list[Path]] = {}
    try:
        for child in os.scandir(directory):
            sha, dot, _ = child.name.partition(".")
            if dot and _is_sha(sha):
                groups.setdefault(sha, []).append(Path(child.path))
    except OSError:
        return {}
    return groups


def _is_sha(text: str) -> bool:
    return len(text) == 40 and all(c in "0123456789abcdef" for c in text)


def _group_entries(kind: str, directory: Path, protect: Collection[str], recent_s: float = 0) -> list[Entry]:
    """The sha-named file groups of ``directory``; protected: named in ``protect`` or used less than ``recent_s`` ago."""
    entries = []
    now = time.time()
    for sha, files in _sha_groups(directory).items():
        lock = directory / f"{sha}.lock"
        data = [file for file in files if file != lock]
        if not data:
            continue
        last_used = max(_newest_mtime(file) for file in data)

        def evict(lock=lock, data=data, last_used=last_used) -> bool:
            return _evict_group(lock, data, last_used)

        protected = sha in protect or now - last_used < recent_s
        entries.append(Entry(kind, sha, sum(tree_bytes(file) for file in data), last_used, protected, evict))
    return entries


def _remove_dead_partial_evidence(common_dir: Path, limit_s: float) -> None:
    """Remove ``.tmp-<candidate>-<pid>`` copies whose writer is gone (it was killed mid-copy).

    A pid that answers is not proof of life (pids are reused, and a pid namespace hides the writer), so a
    copy older than ``limit_s`` (no copy takes that long) goes whatever its pid does."""
    directory = common_dir / EVIDENCE_DIR
    try:
        children = list(os.scandir(directory))
    except OSError:
        return
    now = time.time()
    for child in children:
        if child.name.startswith(EVIDENCE_TMP_PREFIX):
            pid = child.name.rsplit("-", 1)[-1]
            stale = now - _newest_mtime(Path(child.path)) > limit_s
            if pid.isdigit() and (stale or not _process_alive(int(pid))):
                _remove_path(Path(child.path))


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _directory_entries(kind: str, directory: Path, evict: Callable[[Path], None], skip_prefix: str = "",
                       deep: bool = True) -> list[Entry]:
    try:
        children = [Path(c.path) for c in os.scandir(directory) if not (skip_prefix and c.name.startswith(skip_prefix))]
    except OSError:
        return []
    return [Entry(kind, child.name, tree_bytes(child), _newest_mtime(child) if deep else _mtime(child), False,
                  lambda child=child: evict(child)) for child in children]


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def enforce(common_dir: str | Path, *, max_mb: float | None = None, runs: int | None = None,
            protect: Collection[str] = (), recent_s: float | None = None) -> Retained:
    """Bring the kept store to at most ``max_mb`` MB by the order in the module docstring.

    ``protect`` names commits (the candidate and the tip being decided) whose baseline and run files are
    never evicted; so are the run files of any commit used less than ``recent_s`` ago (default
    ``suite-gate-timeout-s``), the record another session's landing may be reading right now. Only
    evictable entries count toward what can be freed; a total still over the bound after everything
    evictable is gone is reported on stderr and in ``over_bound``, not hidden."""
    if max_mb is not None and max_mb <= 0:
        raise ValueError(f"the kept-store bound must be positive, got {max_mb}")
    common_dir = Path(common_dir)
    limit = (max_mb if max_mb is not None else keep_max_mb()) * MB
    keep_runs = runs if runs is not None else evidence_runs()
    store_dir = common_dir / STORE_DIR
    recent = recent_s if recent_s is not None else _configured(TIMEOUT_KEY, DEFAULT_TIMEOUT_S, None)
    with _locked(common_dir / RETENTION_LOCK, fcntl.LOCK_EX):
        _remove_dead_partial_evidence(common_dir, recent)
        entries = _group_entries("baseline", common_dir / BASELINE_DIR, set(protect))
        entries += _group_entries("run", store_dir, set(protect), recent)
        evidence = sorted(_directory_entries("evidence", common_dir / EVIDENCE_DIR, _remove_path,
                                             EVIDENCE_TMP_PREFIX, deep=False), key=_by_last_use)
        newest = {e.name for e in evidence[max(0, len(evidence) - keep_runs):]} if keep_runs > 0 else set()
        entries += [dataclasses.replace(e, protected=e.name in newest) for e in evidence]
        total = tree_bytes(store_dir)
        own = {(store_dir / sub).resolve() for sub in ("", "baseline", "evidence")}
        for store in list(_STORES.values()):
            path = Path(store.path(common_dir) if callable(store.path) else store.path)
            if path.resolve() in own:
                continue
            if not _inside(path, store_dir):
                total += tree_bytes(path)
            entries += _directory_entries(store.name, path, store.evict)
        before = total
        newest_failing = [e for e in entries if e.kind == "evidence" and e.protected]
        evictable = [e for e in entries if not e.protected]
        evicted: list[str] = []
        for entry in sorted(evictable, key=_by_last_use) + sorted(newest_failing, key=_by_last_use):
            if total <= limit:
                break
            if entry.evict() is False:
                continue
            total -= entry.size
            evicted.append(f"{entry.kind}:{entry.name}")
    over = total > limit
    if over:
        print(f"suite-gate: the kept store holds {total / MB:.0f} MB, over the bound of {limit / MB:g} MB, "
              "with nothing left to evict (the files of the candidate and the tip, runs in progress)",
              file=sys.stderr)
    return Retained(before / MB, max(total, 0) / MB, tuple(evicted), over)


def _by_last_use(entry: Entry) -> float:
    return entry.last_used


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True
