"""Status digest of the live tasks, pushed to one message that is edited in place.

Core knows a text digest, a stable key per machine, an opaque handle and a plugin seam; it names
no messenger. The machine-local plugin ``<plugin dir>/publishers/<name>.py`` exposes ``NAME``,
``publish(key, text, handle) -> handle`` (send when ``handle`` is None, else edit that message and
return its handle) and ``health() -> bool | (bool, detail)``, which must stay local: it runs with
the network and process spawning stubbed to raise. Exactly one plugin is used per run.

Sending is change-driven on the sha256 of the digest text (``model.content_digest`` moves with
``last_activity``, which the text does not show) and rate-bounded by ``MIN_INTERVAL_S``, so a
watch loop on a busy machine edits the message at most about twice a minute, well inside the
messenger's per-group budget. The only file written is the publish state (a different file from
anything under the engine's state directory); a dry run loads no plugin and writes nothing.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path

from lib import plugin_dir as plugin_dirs
from plan_dashboard import model, tui

TEXT_LIMIT = 4096
MIN_INTERVAL_S = 30.0
DEFAULT_INTERVAL_S = 60.0
GOAL_WIDTH = 60
PLUGIN_ENV = "PLAN_DASHBOARD_PLUGIN_DIR"
PLUGIN_DIRNAME = "plan-dashboard-plugins"
PUBLISHER_SUBDIR = "publishers"
MORE_TASKS = "… and {} more"
MAX_CONSECUTIVE_FAILURES = 5
MAX_BACKOFF_S = 900.0


class PublisherError(Exception):
    """No plugin, several plugins, a plugin that does not load or that lacks the interface."""


class PublisherBusy(Exception):
    """Another process holds the publish lock, so a second send could duplicate the message."""


class StatePersistError(Exception):
    """The plugin returned but the state could not be saved: the next pass would send a duplicate."""


class PublishAborted(Exception):
    """``MAX_CONSECUTIVE_FAILURES`` passes in a row failed."""


def default_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "plan-dashboard" / "publish-state.json"


def key_for(hostname: str) -> str:
    return f"plan-dashboard:{hostname}"


def _task_line(task: dict) -> str:
    session = task["session_id"][: tui.SESSION_ID_WIDTH]
    if not task["readable"]:
        return f"{session} {task['liveness']} unreadable"
    totals = task["totals"]
    stage = f"{task['stages_done']}/{task['stages_total']}" if task["stages_total"] else "no plan"
    wall = tui.fmt_duration(totals["wall_clock_s"])
    bang = " !" if task["open_difficulty"] else ""
    goal = tui.one_line(task["goal"], GOAL_WIDTH)
    return f"{session} {task['liveness']} {task['node'] or '?'} {stage}{bang} {wall} {tui.cost_text(totals)} {goal}".rstrip()


def digest(snapshot: dict) -> str:
    """The message text: one header and one line per task, within ``TEXT_LIMIT`` characters.
    It carries no clock reading, so it changes only when a shown figure does."""
    tasks = tui.by_recency_live_first(snapshot["tasks"])
    header = f"{snapshot['hostname']}: {len(tasks)} task{'s' if len(tasks) != 1 else ''}"
    lines = [header] if tasks else [header, "no live tasks"]
    for index, task in enumerate(tasks):
        line = _task_line(task)
        remaining = len(tasks) - index
        reserve = len(MORE_TASKS.format(remaining)) + 1 if remaining > 1 else 0
        if len("\n".join([*lines, line])) + reserve > TEXT_LIMIT:
            lines.append(MORE_TASKS.format(remaining))
            break
        lines.append(line)
    return "\n".join(lines)[:TEXT_LIMIT]


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def should_send(entry: dict | None, text: str, now: float, *, min_interval_s: float = MIN_INTERVAL_S) -> bool:
    """Send when nothing was tried for this key yet, or the text changed and neither the rate bound
    (counted from the last attempt, failed or not) nor a plugin's retry-after holds it back."""
    if not entry:
        return True
    if now < float(entry.get("retry_until", 0)):
        return False
    if entry.get("sha") == text_sha(text):
        return False
    last = max(float(entry.get("sent_at", 0)), float(entry.get("attempt_at", 0)))
    return now - last >= min_interval_s


def read_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def single_instance(state_path: Path):
    """Hold an exclusive lock next to the state file for one pass; a second holder gets ``PublisherBusy``."""
    lock_path = Path(state_path).with_name(Path(state_path).name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise PublisherBusy(f"another publish process holds {lock_path}") from None
        yield


def retry_after_of(exc: BaseException) -> float:
    """Seconds a plugin asked to wait, from a ``retry_after`` attribute on its exception."""
    try:
        return max(float(getattr(exc, "retry_after", 0) or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def record_attempt(state_path: Path, state: dict, key: str, now: float, exc: BaseException) -> None:
    """Remember a failed attempt, so the rate bound and any retry-after hold the next one back."""
    entry = dict(state.get(key) or {})
    entry["attempt_at"] = now
    wait = retry_after_of(exc)
    entry["retry_until"] = now + wait if wait else 0
    state[key] = entry
    with contextlib.suppress(OSError):
        write_state(state_path, state)


def discover(plugin_dir: Path | None = None) -> list:
    root = plugin_dir if plugin_dir is not None else plugin_dirs.resolve_plugin_dir(PLUGIN_ENV, PLUGIN_DIRNAME)
    directory = Path(root) / PUBLISHER_SUBDIR
    found = []
    for path in sorted(directory.glob("*.py")) if directory.is_dir() else []:
        try:
            module = plugin_dirs.load_plugin_module(Path(root), f"{PUBLISHER_SUBDIR}/{path.name}", "plan_dashboard_plugin")
        except Exception as exc:
            raise PublisherError(f"{path.name} does not load: {type(exc).__name__}: {exc}") from exc
        if module is None or not callable(getattr(module, "publish", None)) or not callable(getattr(module, "health", None)):
            raise PublisherError(f"{path.name} lacks publish() or health()")
        found.append(module)
    return found


def select_publisher(plugin_dir: Path | None = None):
    found = discover(plugin_dir)
    if not found:
        raise PublisherError("no publisher plugin found")
    if len(found) > 1:
        names = ", ".join(getattr(m, "NAME", m.__name__) for m in found)
        raise PublisherError(f"several publisher plugins found ({names}); keep exactly one")
    return found[0]


def _refuse(*_args, **_kwargs):
    raise OSError("health() must stay local: network and process spawning are off")


@contextlib.contextmanager
def network_blocked():
    """Best effort, not a guarantee: it replaces the common network and process entry points, so a
    plugin that reaches them by another route (ctypes, a pre-imported alias) is not stopped."""
    names = [(socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
             (subprocess, "Popen"), (os, "system")]
    saved = [(owner, name, getattr(owner, name)) for owner, name in names]
    for owner, name in names:
        setattr(owner, name, _refuse)
    try:
        yield
    finally:
        for owner, name, original in saved:
            setattr(owner, name, original)


def check_health(plugin) -> tuple[bool, str]:
    try:
        with network_blocked():
            result = plugin.health()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    ok, detail = result if isinstance(result, tuple) else (result, "")
    return bool(ok), str(detail)


def run_once(state_root, plugin, state_path, *, now: dt.datetime, clock=time.time, min_interval_s: float = MIN_INTERVAL_S,
             **collect_options) -> str:
    """One pass: ``sent``, ``edited`` or ``unchanged`` (``skipped`` while the rate bound holds).
    The handle and hash are saved only after the plugin returned; a failed attempt is recorded too.
    The pass holds the single-instance lock; a plugin error is re-raised after it is recorded, and a
    state that cannot be saved after a successful send raises ``StatePersistError``."""
    snapshot = model.collect(state_root, now=now, **collect_options)
    text = digest(snapshot)
    key = key_for(snapshot["hostname"])
    with single_instance(state_path):
        state = read_state(state_path)
        entry = state.get(key)
        if not should_send(entry, text, clock(), min_interval_s=min_interval_s):
            return "unchanged" if entry and entry.get("sha") == text_sha(text) else "skipped"
        try:
            handle = plugin.publish(key, text, entry.get("handle") if entry else None)
        except Exception as exc:
            record_attempt(state_path, state, key, clock(), exc)
            raise
        state[key] = {"sha": text_sha(text), "handle": handle, "sent_at": clock()}
        try:
            write_state(state_path, state)
        except OSError as exc:
            raise StatePersistError(
                f"the message was sent but {state_path} cannot be written ({exc}); "
                "stopping so the next pass does not send a duplicate") from exc
        return "edited" if entry and entry.get("handle") else "sent"


def backoff_s(interval: float, failures: int, retry_after: float = 0.0) -> float:
    """Wait after ``failures`` consecutive failures: doubling from ``interval``, capped, never below a retry-after."""
    return max(min(interval * 2 ** (failures - 1), MAX_BACKOFF_S), retry_after)


def watch(state_root, plugin, state_path, *, interval: float = DEFAULT_INTERVAL_S, sleep=time.sleep,
          stop=lambda: False, now=lambda: dt.datetime.now(dt.timezone.utc), report=lambda outcome: None,
          **options) -> None:
    """``run_once`` every ``interval`` seconds until ``stop()``. A failing pass is reported and retried
    with backoff; ``MAX_CONSECUTIVE_FAILURES`` in a row raise ``PublishAborted``. A ``StatePersistError``
    ends the loop at once."""
    failures = 0
    while not stop():
        wait = interval
        try:
            report(run_once(state_root, plugin, state_path, now=now(), **options))
            failures = 0
        except StatePersistError:
            raise
        except PublisherBusy as exc:
            report(f"busy: {exc}")
        except Exception as exc:
            failures += 1
            report(f"failed: {type(exc).__name__}: {exc}")
            if failures >= MAX_CONSECUTIVE_FAILURES:
                raise PublishAborted(f"{failures} consecutive failed passes; last: {type(exc).__name__}: {exc}") from exc
            wait = backoff_s(interval, failures, retry_after_of(exc))
        if stop():
            break
        sleep(wait)
