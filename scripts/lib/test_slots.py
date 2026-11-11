#!/usr/bin/env python3
"""Machine-wide limit on concurrent pytest-xdist workers: a queue of runs, each holding N slots.

Difficulty removed: every suite run fanned out one worker per CPU, so two or three concurrent
runs (a landing gate, a reviewer's run, an interactive session's) oversubscribed the machine
and each other's timeouts expired. A run now takes worker slots before it fans out and waits
in a queue when they are taken.

A slot is an ``flock`` on ``slot-<i>`` in a directory at a fixed per-user path (config key
``test-slots-dir``; never derived from HOME or TMPDIR, which the suite gate redirects for its
sandbox). Slots are held by open file descriptors, so the kernel frees them when the holder
dies; no stale-lock cleanup exists. Only the holder of the ``queue`` lock collects slots, so
waiting runs never split the free slots between them.

A process started inside a run that already holds slots never acquires again: the run says so
in ``CORE_TEST_SLOTS_HELD``, and an ancestor holding a slot (seen in /proc/locks) counts too,
for a nested pytest whose environment was scrubbed. Without that a run could wait on slots
its own process tree holds. The ancestor lookup is Linux-only (/proc/locks, /proc/<pid>/stat);
where /proc/locks is absent only the environment variable is consulted.

A run waits at most ``test-slots-max-wait-s`` for its slots, then ``acquire`` raises
``SlotWaitExpired`` naming the holders; while it waits it reports them every minute.

  test_slots.py held [--dir DIR] [--json]
      Print who holds which slot (from /proc/locks). Read-only; exit 0.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

TOTAL_KEY = "test-slots-total"
PER_RUN_KEY = "test-slots-per-run"
DIR_KEY = "test-slots-dir"
MAX_WAIT_KEY = "test-slots-max-wait-s"
DEFAULT_TOTAL = 8  # config.md: test-slots-total
DEFAULT_PER_RUN = 4  # config.md: test-slots-per-run
DEFAULT_DIR = "/tmp/core-test-slots-<uid>"  # config.md: test-slots-dir
DEFAULT_MAX_WAIT_S = 14400  # config.md: test-slots-max-wait-s
WAIT_REPORT_S = 60.0

HELD_ENV = "CORE_TEST_SLOTS_HELD"
DIR_ENV = "CORE_TEST_SLOTS_DIR"
SLOT_PREFIX = "slot-"
QUEUE_FILE = "queue"
POLL_S = 0.25
PROC_LOCKS = "/proc/locks"

ACQUIRED, INHERITED = "acquired", "inherited"


@dataclass(frozen=True)
class Limits:
    total: int
    per_run: int
    directory: Path
    max_wait_s: int = DEFAULT_MAX_WAIT_S


class SlotWaitExpired(Exception):
    """No slots came free within the wait limit; ``holders`` are (slot, pid) at expiry."""

    def __init__(self, waited_s: float, holders: list[tuple[str, int]]):
        pids = ", ".join(str(p) for p in sorted({pid for _, pid in holders})) or "-"
        super().__init__(f"no test slots came free within {waited_s:.0f} s (held by pid {pids})")
        self.waited_s = waited_s
        self.holders = holders


def _positive(raw: str | None, default: int) -> int:
    try:
        value = int(raw or "")
    except ValueError:
        return default
    return value if value > 0 else default


def configured(config_path: str | Path | None = None, env: dict[str, str] | None = None) -> Limits:
    """The limits from config.md (else the equal code constants); ``CORE_TEST_SLOTS_DIR`` overrides the dir."""
    environ = os.environ if env is None else env
    scripts = str(Path(__file__).resolve().parents[1])
    if scripts not in sys.path:
        sys.path.append(scripts)
    try:
        from agentctl.config import parse_config_md

        rows = parse_config_md(Path(config_path) if config_path else None)
    except (ImportError, OSError):
        rows = {}
    raw_dir = environ.get(DIR_ENV) or rows.get(DIR_KEY) or DEFAULT_DIR
    return Limits(
        total=_positive(rows.get(TOTAL_KEY), DEFAULT_TOTAL),
        per_run=_positive(rows.get(PER_RUN_KEY), DEFAULT_PER_RUN),
        directory=Path(raw_dir.replace("<uid>", str(os.getuid()))),
        max_wait_s=_positive(rows.get(MAX_WAIT_KEY), DEFAULT_MAX_WAIT_S),
    )


# ── who holds what ────────────────────────────────────────────────────────

def _file_key(st: os.stat_result) -> tuple[int, int, int]:
    return os.major(st.st_dev), os.minor(st.st_dev), st.st_ino


def _lock_table(path: str = PROC_LOCKS) -> list[tuple[int, tuple[int, int, int]]]:
    """(pid, (major, minor, inode)) of every granted FLOCK in /proc/locks; waiters (``->``) excluded."""
    try:
        text = Path(path).read_text(encoding="ascii", errors="replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] == "->" or fields[1] != "FLOCK":
            continue
        dev = fields[5].split(":")
        try:
            rows.append((int(fields[4]), (int(dev[0], 16), int(dev[1], 16), int(dev[2]))))
        except (ValueError, IndexError):
            continue
    return rows


def _slot_files(directory: Path) -> dict[tuple[int, int, int], str]:
    found = {}
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return found
    for entry in entries:
        if entry.name.startswith(SLOT_PREFIX):
            try:
                found[_file_key(entry.stat(follow_symlinks=False))] = entry.name
            except OSError:
                continue
    return found


def held(directory: str | Path, locks_path: str = PROC_LOCKS) -> list[tuple[str, int]]:
    """(slot file name, holder pid) for every held slot in ``directory``, sorted by name.

    /proc/locks is read in several syscalls, so a lock passed from one holder to the next
    between two reads can appear twice; an exclusive flock has one holder, so the last row wins.
    """
    files = _slot_files(Path(directory))
    holders = {files[key]: pid for pid, key in _lock_table(locks_path) if key in files}
    return sorted(holders.items())


def _parent(pid: int) -> int:
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        return int(stat_line.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _ancestors(pid: int) -> set[int]:
    found = set()
    pid = _parent(pid)
    while pid > 1 and pid not in found:
        found.add(pid)
        pid = _parent(pid)
    return found


def inherited(directory: str | Path, env: dict[str, str] | None = None, pid: int | None = None,
              locks_path: str = PROC_LOCKS) -> int | None:
    """The slot count this process already runs under, or None when it must acquire its own.

    ``CORE_TEST_SLOTS_HELD`` (set by a run for its process tree) wins; else, on Linux, the
    number of slots held by this process's ancestors. Without ``locks_path`` the environment
    variable is the only source.
    """
    environ = os.environ if env is None else env
    if environ.get(HELD_ENV):
        return _positive(environ.get(HELD_ENV), 1)
    if not os.path.exists(locks_path):
        return None
    ancestors = _ancestors(os.getpid() if pid is None else pid)
    count = sum(1 for _, holder in held(directory, locks_path) if holder in ancestors)
    return count or None


# ── acquisition ───────────────────────────────────────────────────────────

def _private_dir(directory: Path) -> Path:
    """Create the slot dir 0700, and refuse one that is a symlink or belongs to another user."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(directory)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"slot directory {directory} is not a directory owned by uid {os.getuid()}")
    return directory


def _open_lock_file(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@dataclass
class Holder:
    """Slots held by this process until ``release`` (or the process's death)."""
    count: int
    source: str
    queued_at: float
    acquired_at: float
    slots: tuple[str, ...] = ()
    _fds: list[int] = field(default_factory=list, repr=False)

    @property
    def wait_s(self) -> float:
        return max(0.0, self.acquired_at - self.queued_at)

    @property
    def fds(self) -> tuple[int, ...]:
        """The slot descriptors, to hand to a child (``pass_fds``) that must keep the slots if this process dies."""
        return tuple(self._fds)

    def release(self) -> None:
        for fd in self._fds:
            os.close(fd)
        self._fds.clear()

    def __enter__(self) -> "Holder":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def share(want: int, limits: Limits) -> int:
    """The slots a run asking for ``want`` takes: at least one, at most its per-run share."""
    return max(1, min(want, limits.per_run, limits.total))


class _Wait:
    """The deadline of one acquisition and its periodic report of the holders."""

    def __init__(self, directory: Path, max_wait_s: float, poll_s: float, on_wait, report_s: float):
        self.directory, self.poll_s, self.on_wait, self.report_s = directory, poll_s, on_wait, report_s
        self.started = time.monotonic()
        self.deadline = self.started + max_wait_s
        self.next_report: float | None = None

    def tick(self) -> None:
        """One poll interval of waiting: report when due, raise past the deadline, then sleep."""
        now = time.monotonic()
        if now >= self.deadline:
            raise SlotWaitExpired(now - self.started, held(self.directory))
        if self.on_wait is not None and (self.next_report is None or now >= self.next_report):
            self.on_wait(held(self.directory))
            self.next_report = now + self.report_s
        time.sleep(self.poll_s)


def acquire(want: int, *, limits: Limits | None = None, poll_s: float = POLL_S,
            on_wait=None, report_s: float = WAIT_REPORT_S, clock=time.time) -> Holder:
    """Hold ``min(want, per_run, total)`` slots, waiting in the queue until they are free.

    A process that already runs under slots (``inherited``) gets a holder of that count that
    holds nothing. ``on_wait`` is called with the current holders when the run starts to wait
    and every ``report_s`` after; past ``limits.max_wait_s`` it raises ``SlotWaitExpired``.
    """
    limits = limits or configured()
    queued_at = clock()
    already = inherited(limits.directory)
    if already is not None:
        return Holder(already, INHERITED, queued_at, queued_at)
    want = share(want, limits)
    directory = _private_dir(limits.directory)
    queue_fd = _open_lock_file(directory / QUEUE_FILE)
    fds: list[int] = []
    names: list[str] = []
    wait = _Wait(directory, limits.max_wait_s, poll_s, on_wait, report_s)
    try:
        while not _try_lock(queue_fd):
            wait.tick()
        while True:
            for i in range(limits.total):
                name = f"{SLOT_PREFIX}{i}"
                if len(fds) == want or name in names:
                    continue
                fd = _open_lock_file(directory / name)
                if _try_lock(fd):
                    fds.append(fd)
                    names.append(name)
                else:
                    os.close(fd)
            if len(fds) == want:
                break
            wait.tick()
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise
    finally:
        os.close(queue_fd)
    for fd in fds:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    return Holder(want, ACQUIRED, queued_at, clock(), tuple(names), fds)


def describe_wait(want: int, limits: Limits, holders: list[tuple[str, int]]) -> str:
    pids = sorted({pid for _, pid in holders})
    return (f"test slots: waiting for {share(want, limits)} of {limits.total} in {limits.directory} "
            f"({len(holders)} held by pid {', '.join(map(str, pids)) or '-'})")


# ── CLI ───────────────────────────────────────────────────────────────────

def _cmd_held(args: argparse.Namespace) -> int:
    limits = configured()
    directory = Path(args.dir) if args.dir else limits.directory
    rows = held(directory)
    if args.json:
        print(json.dumps({"dir": str(directory), "total": limits.total, "held": len(rows),
                          "holders": [{"slot": s, "pid": p} for s, p in rows]}))
        return 0
    print(f"{len(rows)} of {limits.total} slots held in {directory}")
    for slot, pid in rows:
        print(f"  {slot} pid {pid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="test_slots.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("held", help="print who holds which slot")
    show.add_argument("--dir", default=None, help="slot directory (default: config.md test-slots-dir)")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_cmd_held)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
