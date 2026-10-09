"""Standing-mandate persistence: the I/O half of the background debt cycle.

Difficulty removed: the bounds in `mandate.py` are pure so a test can pin each one, but a
mandate must also survive between cycles, serialise two cycles that would race, and be
stoppable while a cycle is mid-flight. Everything that touches a file, a lock, the clock or
a process lives here and nowhere else, so `mandate.py` keeps its purity contract.

Layout, one directory per mandate under ``mandates_root()``::

    mandate.json     the Mandate record (atomic replace)
    events.jsonl     append-only audit log
    labels.jsonl     which label the cycle (not the user) applied, per issue
    digests.json     cycle_id -> delivery record of that cycle's digest
    cycle.lock       flock held for a whole cycle; its text is the holder's pid

``$AGENTCTL_MANDATE_DIR`` overrides the root; the test suite sets it so no test touches
the real agent home.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from lib import config_root

from . import cost
from . import mandate as rules
from .dispatch import Runner, subprocess_runner

TIMER_UNIT = "agent-debt-cycle.timer"
KILL_TREE = Path(__file__).resolve().parents[1] / "kill-tree.py"

MANDATE_FILE = "mandate.json"
EVENTS_FILE = "events.jsonl"
LABELS_FILE = "labels.jsonl"
DIGESTS_FILE = "digests.json"
LOCK_FILE = "cycle.lock"
FINGERPRINT_FILES = (MANDATE_FILE, EVENTS_FILE, LABELS_FILE, DIGESTS_FILE)


class CycleBusy(RuntimeError):
    """Another cycle already holds the lock."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def mandates_root() -> Path:
    override = os.environ.get("AGENTCTL_MANDATE_DIR")
    if override:
        return Path(override).expanduser()
    return config_root.agentctl_dir() / "mandates"


def mandate_dir(mandate_id: str) -> Path:
    return mandates_root() / mandate_id


def path_of(mandate_id: str, name: str) -> Path:
    return mandate_dir(mandate_id) / name


# --- primitives ------------------------------------------------------------------------

def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_jsonl(path: Path) -> "list[dict]":
    rows: "list[dict]" = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# --- the mandate record ----------------------------------------------------------------

def load_mandate(mandate_id: str) -> "rules.Mandate | None":
    data = read_json(path_of(mandate_id, MANDATE_FILE))
    if data is None:
        return None
    return rules.Mandate.from_dict(data)


def save_mandate(mandate: rules.Mandate) -> None:
    atomic_write_json(path_of(mandate.id, MANDATE_FILE), mandate.to_dict())


def list_mandate_ids() -> "list[str]":
    root = mandates_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / MANDATE_FILE).is_file())


# --- logs ------------------------------------------------------------------------------

def append_event(mandate_id: str, event: str, detail: "dict | None" = None, *, now: "datetime | None" = None) -> dict:
    row = {"ts": rules.format_ts(now or utcnow()), "event": event, "detail": detail or {}}
    append_jsonl(path_of(mandate_id, EVENTS_FILE), row)
    return row


def read_events(mandate_id: str) -> "list[dict]":
    return read_jsonl(path_of(mandate_id, EVENTS_FILE))


def append_label_row(
    mandate_id: str, issue: int, label: str, cycle_id: str, *, now: "datetime | None" = None,
) -> dict:
    row = {
        "ts": rules.format_ts(now or utcnow()),
        "issue": int(issue),
        "label": label,
        "by": "cycle",
        "cycle_id": cycle_id,
    }
    append_jsonl(path_of(mandate_id, LABELS_FILE), row)
    return row


def read_label_rows(mandate_id: str) -> "list[dict]":
    return read_jsonl(path_of(mandate_id, LABELS_FILE))


def read_digests(mandate_id: str) -> dict:
    data = read_json(path_of(mandate_id, DIGESTS_FILE), {})
    return data if isinstance(data, dict) else {}


def record_digest(
    mandate_id: str, cycle_id: str, *, ok: bool, notifier: str, delivered_at: "datetime | None" = None,
) -> dict:
    digests = read_digests(mandate_id)
    record = {
        "ok": bool(ok),
        "notifier": notifier,
        "delivered_at": rules.format_ts(delivered_at or utcnow()),
    }
    digests[cycle_id] = record
    atomic_write_json(path_of(mandate_id, DIGESTS_FILE), digests)
    return record


def spend_now(mandate_id: str, now: "datetime | None" = None) -> rules.SpendWindows:
    moment = now or utcnow()
    rows = cost.read_rows(cost.COST_LOG)
    return rules.compute_spend(rows, read_events(mandate_id), str(mandate_dir(mandate_id)), moment)


def state_fingerprint(mandate_id: str, names: Sequence[str] = FINGERPRINT_FILES) -> str:
    """sha256 over the mandate's state files, so a caller can tell whether anything it was
    not meant to change moved between two points."""
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8") + b"\0")
        try:
            digest.update(path_of(mandate_id, name).read_bytes())
        except OSError:
            digest.update(b"<absent>")
        digest.update(b"\0")
    return digest.hexdigest()


# --- the cycle lock --------------------------------------------------------------------

@contextmanager
def cycle_lock(mandate_id: str) -> Iterator[int]:
    """Hold the mandate's cycle lock for the duration; raise CycleBusy if another holder
    has it. The lock file's text is the holder's pid, which `probe_cycle` and the stop
    verb read."""
    path = path_of(mandate_id, LOCK_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CycleBusy(f"mandate {mandate_id}: a cycle already holds {path}") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        try:
            yield fd
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def probe_cycle(mandate_id: str) -> "tuple[bool, int | None]":
    """Is a cycle running right now? A non-blocking try on the lock: acquiring it means
    nobody holds it, and it is released again at once."""
    path = path_of(mandate_id, LOCK_FILE)
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False, None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raw = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
            return True, int(raw) if raw.isdigit() else None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False, None
    finally:
        os.close(fd)


# --- stop / resume ---------------------------------------------------------------------

def _systemctl(run: Runner, action: str) -> dict:
    argv = ["systemctl", "--user", action, "--now", TIMER_UNIT]
    result = run(argv)
    return {"argv": argv, "returncode": result.returncode, "stderr": (result.stderr or "").strip()[:200]}


def stop_cycle(mandate_id: str, *, runner: "Runner | None" = None) -> dict:
    """Pause the mandate, disable the timer, and kill a running cycle's process tree.

    The pause lands first, so a cycle that begins after this point is refused by its own
    gate; an idle mandate (nobody holds the lock) is never killed.
    """
    run = runner or subprocess_runner
    mandate = load_mandate(mandate_id)
    if mandate is None:
        raise rules.MandateError(f"no mandate {mandate_id!r}")
    save_mandate(rules.stopped(mandate))
    timer = _systemctl(run, "disable")
    held, pid = probe_cycle(mandate_id)
    killed = None
    if held and pid:
        argv = [sys.executable, str(KILL_TREE), str(pid)]
        result = run(argv)
        killed = {"pid": pid, "returncode": result.returncode}
    detail = {"timer": timer, "cycle_running": held, "killed": killed}
    append_event(mandate_id, "stopped", detail)
    return detail


def resume_cycle(mandate_id: str, *, runner: "Runner | None" = None) -> dict:
    """Unpause, close the breaker, and re-enable the timer (a missing timer is reported,
    not fatal)."""
    run = runner or subprocess_runner
    mandate = load_mandate(mandate_id)
    if mandate is None:
        raise rules.MandateError(f"no mandate {mandate_id!r}")
    save_mandate(rules.resumed(mandate))
    timer = _systemctl(run, "enable")
    detail = {"timer": timer}
    append_event(mandate_id, "resumed", detail)
    return detail
