"""Standing-mandate persistence: the I/O half of the background debt cycle.

Difficulty removed: the bounds in `mandate.py` are pure so a test can pin each one, but a
mandate must also survive between cycles, serialise two cycles that would race, and be
stoppable while a cycle is mid-flight. Everything that touches a file, a lock, the clock or
a process lives here and nowhere else, so `mandate.py` keeps its purity contract.

Layout, one directory per mandate under ``mandates_root()``::

    mandate.json     the Mandate record (atomic replace)
    events.jsonl     append-only audit log: {ts, event, by, detail}
    labels.jsonl     one row per label applied: {ts, issue, label, by: user|cycle, cycle_id, reason}
    items.jsonl      one row per item taken: {ts, cycle_id, issue, outcome, branch, pr_url,
                     cost_usd, duration_s, tests, review, gates, ...}; outcome is one of
                     `rules.OUTCOMES`, tests is pass|fail|not-run, review accept|reject|not-run
    cycles.jsonl     one row per cycle: {cycle_id, started_at, ended_at, status, spend_24h,
                     spend_7d, taken, triaged, digest: {notifier, ok, delivered_at}, ...};
                     rows with one cycle_id merge, later fields winning (`read_cycle_records`)
    cycles/<cycle_id>/commands.jsonl
                     one row per external command the driver ran: {ts, argv, cwd, exit}
    cycle.lock       flock held for a whole cycle; its text is the holder's pid

Item, cycle and command rows may carry fields beyond those named; the named ones are
validated. ``$AGENTCTL_MANDATE_DIR`` overrides the root; the test suite sets it so no test
touches the real agent home.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
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
ITEMS_FILE = "items.jsonl"
CYCLES_FILE = "cycles.jsonl"
COMMANDS_FILE = "commands.jsonl"
LOCK_FILE = "cycle.lock"
FINGERPRINT_FILES = (MANDATE_FILE, EVENTS_FILE, LABELS_FILE)

LABEL_AUTHORS = ("user", "cycle")
TEST_VERDICTS = ("pass", "fail", "not-run")
REVIEW_VERDICTS = ("accept", "reject", "not-run")
PID_READ_ATTEMPTS = 10
PID_READ_DELAY_S = 0.02


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
    return mandates_root() / rules.require_slug(mandate_id, "mandate id")


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


def mandate_exists(mandate_id: str) -> bool:
    """Whether a record file is present, without parsing it -- a corrupt record still
    exists, and the verbs that must work on one (stop) need to tell it from a missing one."""
    return path_of(mandate_id, MANDATE_FILE).is_file()


def list_mandate_ids() -> "list[str]":
    root = mandates_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / MANDATE_FILE).is_file())


# --- logs ------------------------------------------------------------------------------

def append_event(
    mandate_id: str, event: str, detail: "dict | None" = None, *, by: str = "", now: "datetime | None" = None,
) -> dict:
    row = {"ts": rules.format_ts(now or utcnow()), "event": event, "by": by, "detail": detail or {}}
    append_jsonl(path_of(mandate_id, EVENTS_FILE), row)
    return row


def open_breaker(
    mandate_id: str, reason: str, *, by: str = "cycle", now: "datetime | None" = None,
) -> rules.Mandate:
    """Open the breaker on the persisted record and log it with the reason, in that order,
    so a reader that sees the event can rely on the record being tripped."""
    mandate = load_mandate(mandate_id)
    if mandate is None:
        raise rules.MandateError(f"no mandate {mandate_id!r}")
    tripped = rules.breaker_opened(mandate, reason)
    save_mandate(tripped)
    append_event(mandate_id, "breaker-open", {"reason": reason}, by=by, now=now)
    return tripped


def read_events(mandate_id: str) -> "list[dict]":
    return read_jsonl(path_of(mandate_id, EVENTS_FILE))


def append_label_row(
    mandate_id: str, issue: int, label: str, cycle_id: str, *,
    by: str = "cycle", reason: str = "", now: "datetime | None" = None,
) -> dict:
    """Log a label applied to `issue`. `by` says who set it -- `cycle` for the driver, `user`
    for one it observed -- because the owner's account applies both and only this log can
    tell them apart."""
    if by not in LABEL_AUTHORS:
        raise rules.MandateError(f"label author must be one of {LABEL_AUTHORS}, got {by!r}")
    row = {
        "ts": rules.format_ts(now or utcnow()),
        "issue": int(issue),
        "label": label,
        "by": by,
        "cycle_id": cycle_id,
        "reason": str(reason),
    }
    append_jsonl(path_of(mandate_id, LABELS_FILE), row)
    return row


def read_label_rows(mandate_id: str) -> "list[dict]":
    return read_jsonl(path_of(mandate_id, LABELS_FILE))


def _require_keys(row: dict, keys: Sequence[str], what: str) -> None:
    missing = [key for key in keys if key not in row]
    if missing:
        raise rules.MandateError(f"{what} row lacks {', '.join(missing)}")


def _stamped(row: dict, now: "datetime | None") -> dict:
    stamped = dict(row)
    stamped.setdefault("ts", rules.format_ts(now or utcnow()))
    return stamped


def _check_choice(row: dict, key: str, allowed: Sequence[str], what: str) -> None:
    if key in row and row[key] not in allowed:
        raise rules.MandateError(f"{what} {key} must be one of {tuple(allowed)}, got {row[key]!r}")


def append_item_row(mandate_id: str, row: dict, *, now: "datetime | None" = None) -> dict:
    """Log the outcome of one item. Requires cycle_id, issue and outcome; the enumerated
    fields (outcome, tests, review) are checked, the rest is stored as given."""
    _require_keys(row, ("cycle_id", "issue", "outcome"), "item")
    rules.require_slug(row["cycle_id"], "cycle id")
    _check_choice(row, "outcome", rules.OUTCOMES, "item")
    _check_choice(row, "tests", TEST_VERDICTS, "item")
    _check_choice(row, "review", REVIEW_VERDICTS, "item")
    stamped = _stamped(row, now)
    stamped["issue"] = int(stamped["issue"])
    append_jsonl(path_of(mandate_id, ITEMS_FILE), stamped)
    return stamped


def read_item_rows(mandate_id: str) -> "list[dict]":
    return read_jsonl(path_of(mandate_id, ITEMS_FILE))


def _check_digest(digest) -> None:
    if not isinstance(digest, dict):
        raise rules.MandateError(f"cycle digest must be an object, got {digest!r}")
    _require_keys(digest, ("notifier", "ok", "delivered_at"), "digest")
    if not isinstance(digest["notifier"], str) or not isinstance(digest["ok"], bool):
        raise rules.MandateError(f"digest needs a string notifier and a boolean ok, got {digest!r}")
    rules.parse_ts(digest["delivered_at"], strict=True)


def append_cycle_row(mandate_id: str, row: dict, *, now: "datetime | None" = None) -> dict:
    """Log a cycle (or a later correction to one: rows of one cycle_id merge). Requires
    cycle_id; a `digest` must be {notifier, ok, delivered_at}."""
    _require_keys(row, ("cycle_id",), "cycle")
    rules.require_slug(row["cycle_id"], "cycle id")
    if "digest" in row:
        _check_digest(row["digest"])
    stamped = _stamped(row, now)
    append_jsonl(path_of(mandate_id, CYCLES_FILE), stamped)
    return stamped


def read_cycle_rows(mandate_id: str) -> "list[dict]":
    return read_jsonl(path_of(mandate_id, CYCLES_FILE))


def read_cycle_records(mandate_id: str) -> "dict[str, dict]":
    """cycle_id -> the cycle's rows folded in file order, a later field replacing an earlier."""
    records: "dict[str, dict]" = {}
    for row in read_cycle_rows(mandate_id):
        cycle_id = row.get("cycle_id")
        if isinstance(cycle_id, str):
            records.setdefault(cycle_id, {}).update(row)
    return records


def record_digest(
    mandate_id: str, cycle_id: str, *, ok: bool, notifier: str, delivered_at: "datetime | None" = None,
) -> dict:
    """Record how a cycle's digest was delivered, as a cycles.jsonl row of its own."""
    digest = {
        "ok": bool(ok),
        "notifier": notifier,
        "delivered_at": rules.format_ts(delivered_at or utcnow()),
    }
    append_cycle_row(mandate_id, {"cycle_id": cycle_id, "digest": digest}, now=delivered_at)
    return digest


def read_digests(mandate_id: str) -> "dict[str, dict]":
    """cycle_id -> {notifier, ok, delivered_at}: the input `rules.evaluate_candidate` takes.
    A cycle with no delivered digest is absent, which the rule reads as not delivered."""
    return {
        cycle_id: record["digest"]
        for cycle_id, record in read_cycle_records(mandate_id).items()
        if isinstance(record.get("digest"), dict)
    }


def cycle_dir(mandate_id: str, cycle_id: str) -> Path:
    return mandate_dir(mandate_id) / "cycles" / rules.require_slug(cycle_id, "cycle id")


def append_command_row(mandate_id: str, cycle_id: str, row: dict, *, now: "datetime | None" = None) -> dict:
    """Log one external command: {ts, argv, cwd, exit}."""
    _require_keys(row, ("argv", "cwd", "exit"), "command")
    if not isinstance(row["argv"], list) or not all(isinstance(a, str) for a in row["argv"]):
        raise rules.MandateError(f"command argv must be a list of strings, got {row['argv']!r}")
    stamped = _stamped(row, now)
    append_jsonl(cycle_dir(mandate_id, cycle_id) / COMMANDS_FILE, stamped)
    return stamped


def read_command_rows(mandate_id: str, cycle_id: str) -> "list[dict]":
    return read_jsonl(cycle_dir(mandate_id, cycle_id) / COMMANDS_FILE)


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
    """Is a cycle running right now, and which pid holds it? A non-blocking try on the lock:
    acquiring it means nobody holds it, and it is released again at once. A holder takes
    the flock before it writes its pid, so an empty file under a held lock is read again
    briefly; if the pid never shows, the answer is (True, None)."""
    path = path_of(mandate_id, LOCK_FILE)
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False, None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            for attempt in range(PID_READ_ATTEMPTS):
                raw = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
                if raw.isdigit():
                    return True, int(raw)
                if attempt + 1 < PID_READ_ATTEMPTS:
                    time.sleep(PID_READ_DELAY_S)
            return True, None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False, None
    finally:
        os.close(fd)


# --- stop / resume ---------------------------------------------------------------------

def _systemctl(run: Runner, action: str) -> dict:
    argv = ["systemctl", "--user", action, "--now", TIMER_UNIT]
    result = run(argv)
    return {"argv": argv, "returncode": result.returncode, "stderr": (result.stderr or "").strip()[:200]}


def stop_cycle(mandate_id: str, *, by: str = "", runner: "Runner | None" = None) -> dict:
    """Pause the mandate, disable the timer, and kill a running cycle's process tree.

    The pause is tried first, so a cycle that begins after this point is refused by its own
    gate, but it is best-effort: a record that will not load must not stand between the
    user and the timer and the running cycle, so a failure is reported and the stop
    goes on. An idle mandate (nobody holds the lock) is never killed.
    """
    run = runner or subprocess_runner
    if not mandate_exists(mandate_id):
        raise rules.MandateError(f"no mandate {mandate_id!r}")
    paused, pause_error = False, None
    try:
        mandate = load_mandate(mandate_id)
        if mandate is None:
            pause_error = "mandate record present but not readable JSON"
        else:
            save_mandate(rules.stopped(mandate))
            paused = True
    except (rules.MandateError, OSError) as exc:
        pause_error = str(exc)
    timer = _systemctl(run, "disable")
    held, pid = probe_cycle(mandate_id)
    killed = None
    kill_skipped = None
    if held and pid:
        argv = [sys.executable, str(KILL_TREE), str(pid)]
        result = run(argv)
        killed = {"pid": pid, "returncode": result.returncode}
    elif held:
        kill_skipped = "holder pid unknown"
    detail = {
        "timer": timer, "paused": paused, "cycle_running": held, "pid": pid, "killed": killed,
    }
    if pause_error:
        detail["pause_error"] = pause_error
    if kill_skipped:
        detail["reason"] = kill_skipped
    append_event(mandate_id, "stopped", detail, by=by)
    return detail


def resume_cycle(mandate_id: str, *, by: str = "", runner: "Runner | None" = None) -> dict:
    """Unpause, close the breaker, and re-enable the timer (a missing timer is reported,
    not fatal)."""
    run = runner or subprocess_runner
    mandate = load_mandate(mandate_id)
    if mandate is None:
        raise rules.MandateError(f"no mandate {mandate_id!r}")
    save_mandate(rules.resumed(mandate))
    timer = _systemctl(run, "enable")
    detail = {"timer": timer}
    append_event(mandate_id, "resumed", detail, by=by)
    return detail
