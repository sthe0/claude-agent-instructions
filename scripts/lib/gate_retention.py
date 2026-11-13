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
before this module, or by another tool) is never removed: a concurrent session still on the old gate
has live sandboxes without one. Creation is serialised against sweeping by a shared ``flock`` on
``<git common dir>/suite-gate/sandbox-sweep.lock`` (outside the swept directory, so a clean run leaves
nothing there): a creator holds it from before it makes the directory until it holds the owner lock,
a sweeper holds it exclusively, so a sweeper never sees an owner file whose lock is not yet taken.
Accepted residual: a creator killed between making the directory and creating its owner file leaves an
ownerless directory.

Failing runs keep their evidence (log, JUnit XML, failure list, the sandbox's core tree without its
tmp/) under ``<common dir>/suite-gate/evidence/<candidate>/`` before the sandbox goes.

Retention. After a run, ``enforce`` keeps ``suite-gate-failure-evidence-runs`` newest failing runs'
evidence and evicts the least recently used of everything else evictable (the tip baselines, older
evidence, and every store registered with ``register_store``) until the kept store is at most
``suite-gate-keep-max-mb``. The bound wins: the newest failing runs' evidence goes last, oldest first,
and only when nothing else is left and the total still exceeds the bound. Candidate records are never
evicted, and neither is the baseline of the tip or candidate being decided; evicting any other baseline
only forces its recomputation. Last use is the newest mtime among an entry's files (a baseline cache hit
touches its record).

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
from pathlib import Path
from typing import Callable, Collection, Iterator

STORE_DIR = "suite-gate"
SWEEP_LOCK = f"{STORE_DIR}/sandbox-sweep.lock"
RETENTION_LOCK = f"{STORE_DIR}/retention.lock"
EVIDENCE_DIR = f"{STORE_DIR}/evidence"
BASELINE_DIR = f"{STORE_DIR}/baseline"
OWNER_SUFFIX = ".owner"
KEEP_ENV = "SUITE_GATE_KEEP_SANDBOX"
EVIDENCE_TMP_PREFIX = ".tmp-"

KEEP_MAX_KEY = "suite-gate-keep-max-mb"
EVIDENCE_RUNS_KEY = "suite-gate-failure-evidence-runs"
DEFAULT_KEEP_MAX_MB = 2048  # config.md: suite-gate-keep-max-mb
DEFAULT_EVIDENCE_RUNS = 3  # config.md: suite-gate-failure-evidence-runs
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


@contextlib.contextmanager
def sandbox(common_dir: str | Path, parent: str | Path, prefix: str) -> Iterator[Path]:
    """A fresh sandbox directory under ``parent`` whose owner lock is held until the block ends.

    On exit the directory and its owner file are removed, unless ``SUITE_GATE_KEEP_SANDBOX`` is set."""
    with _locked(Path(common_dir) / SWEEP_LOCK, fcntl.LOCK_SH):
        root = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
        owner = owner_path(root)
        try:
            fd = os.open(owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError:
            root.rmdir()
            raise
        fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
        yield root
    finally:
        try:
            if not os.environ.get(KEEP_ENV):
                shutil.rmtree(root, ignore_errors=True)
                owner.unlink(missing_ok=True)
        finally:
            os.close(fd)


def sweep_dead_sandboxes(common_dir: str | Path, parent: str | Path, prefix: str) -> list[str]:
    """Remove every sandbox under ``parent`` that has an owner file of this user whose lock is free.

    Returns the removed sandbox names. A sandbox without an owner file, or with a held lock, stays."""
    removed: list[str] = []
    uid = os.getuid()
    with _locked(Path(common_dir) / SWEEP_LOCK, fcntl.LOCK_EX):
        try:
            entries = sorted(os.scandir(parent), key=lambda e: e.name)
        except OSError:
            return removed
        for entry in entries:
            name = entry.name
            if not (name.startswith(prefix) and name.endswith(OWNER_SUFFIX)):
                continue
            if len(name) == len(prefix) + len(OWNER_SUFFIX):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
                    continue
                fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                root = Path(parent) / name[: -len(OWNER_SUFFIX)]
                if root.is_dir() and not root.is_symlink():
                    shutil.rmtree(root, ignore_errors=True)
                os.unlink(entry.path)
                removed.append(root.name)
            finally:
                os.close(fd)
    return removed


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
            shutil.copytree(core_tree, partial / "core", symlinks=True, ignore_dangling_symlinks=True)
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
    evict: Callable[[], None]


@dataclasses.dataclass(frozen=True)
class Retained:
    before_mb: float
    after_mb: float
    evicted: tuple[str, ...]


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


def _baseline_entries(common_dir: Path, protect: Collection[str]) -> list[Entry]:
    directory = common_dir / BASELINE_DIR
    groups: dict[str, list[Path]] = {}
    try:
        for child in os.scandir(directory):
            if not child.name.startswith("."):
                groups.setdefault(child.name.split(".", 1)[0], []).append(Path(child.path))
    except OSError:
        return []
    entries = []
    for sha, files in groups.items():
        def evict(files=files) -> None:
            for file in files:
                file.unlink(missing_ok=True)

        entries.append(Entry("baseline", sha, sum(tree_bytes(f) for f in files),
                             max(_newest_mtime(f) for f in files), sha in protect or _lock_busy(files), evict))
    return entries


def _lock_busy(files: list[Path]) -> bool:
    """Whether a run holds the baseline's ``.lock`` (it is being built or read)."""
    for file in files:
        if file.suffix == ".lock":
            try:
                fd = os.open(file, os.O_RDONLY)
            except OSError:
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            finally:
                os.close(fd)
    return False


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


def enforce(common_dir: str | Path, *, max_mb: int | None = None, runs: int | None = None,
            protect: Collection[str] = ()) -> Retained:
    """Bring the kept store to at most ``max_mb`` MB by the order in the module docstring.

    ``protect`` names commits (the candidate and the tip being decided) whose baselines are never evicted.
    Only evictable entries count toward what can be freed; a total still over the bound after
    everything evictable is gone is reported, not hidden."""
    common_dir = Path(common_dir)
    limit = (max_mb if max_mb is not None else keep_max_mb()) * MB
    keep_runs = runs if runs is not None else evidence_runs()
    with _locked(common_dir / RETENTION_LOCK, fcntl.LOCK_EX):
        stores = list(_STORES.values())
        outside = []
        for store in stores:
            path = store.path(common_dir) if callable(store.path) else store.path
            outside.append((store, Path(path)))
        entries = _baseline_entries(common_dir, set(protect))
        evidence = sorted(_directory_entries("evidence", common_dir / EVIDENCE_DIR, _remove_path,
                                             EVIDENCE_TMP_PREFIX, deep=False), key=_by_last_use)
        newest = {e.name for e in evidence[max(0, len(evidence) - keep_runs):]} if keep_runs > 0 else set()
        entries += [dataclasses.replace(e, protected=e.name in newest) for e in evidence]
        total = tree_bytes(common_dir / STORE_DIR)
        for store, path in outside:
            if not _inside(path, common_dir / STORE_DIR):
                total += tree_bytes(path)
            entries += _directory_entries(store.name, path, store.evict)
        before = total
        newest_failing = [e for e in entries if e.kind == "evidence" and e.protected]
        evictable = [e for e in entries if not e.protected]
        evicted: list[str] = []
        for entry in sorted(evictable, key=_by_last_use) + sorted(newest_failing, key=_by_last_use):
            if total <= limit:
                break
            entry.evict()
            total -= entry.size
            evicted.append(f"{entry.kind}:{entry.name}")
    return Retained(before / MB, max(total, 0) / MB, tuple(evicted))


def _by_last_use(entry: Entry) -> float:
    return entry.last_used


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True
