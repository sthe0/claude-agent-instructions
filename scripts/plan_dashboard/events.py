"""Moment stream of the console dashboard, written only under ``tui --events PATH``.

One JSON line per event, ``{"ev": name, "t_ns": time.monotonic_ns(), ...payload}``, written whole, under a
lock, to the named pipe at PATH, which the timing harness makes and reads. Not stderr: Textual draws
the screen on file descriptor 2. With the flag off ``emit`` is one branch and writes nothing.

``scripts/tests/fixtures/plan_dashboard_contract.json`` lists the vocabulary and each event's payload.
"""
from __future__ import annotations

import json
import os
import select
import threading
import time

_fd: int | None = None
_lock = threading.Lock()


def enable(fd: int) -> None:
    global _fd
    _fd = fd


def enable_path(path) -> None:
    """Open ``path`` (the harness's named pipe) for writing and send the stream to it."""
    enable(os.open(path, os.O_WRONLY))


def disable() -> None:
    global _fd
    _fd = None


def enabled() -> bool:
    return _fd is not None


def emit(ev: str, **payload) -> None:
    if _fd is None:
        return
    line = json.dumps({"ev": ev, "t_ns": time.monotonic_ns(), **payload}, separators=(",", ":")) + "\n"
    with _lock:
        write_whole(_fd, line.encode("utf-8"))


def write_whole(fd: int, data: bytes) -> None:
    """Write all of ``data``. A blocked write that a signal interrupts after some bytes returns that count,
    not an error, so a single ``os.write`` would drop the line's tail and leave the reader a cut line.
    A broken pipe or any other error ends the write."""
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(fd, view):]
        except BlockingIOError:
            select.select([], [fd], [])
        except OSError:
            return
