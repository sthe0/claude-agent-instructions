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


class PublisherError(Exception):
    """No plugin, several plugins, a plugin that does not load or that lacks the interface."""


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
    goal = tui._one_line(task["goal"], GOAL_WIDTH)
    return f"{session} {task['liveness']} {task['node'] or '?'} {stage}{bang} {wall} {tui._cost(totals)} {goal}".rstrip()


def digest(snapshot: dict) -> str:
    """The message text: one header and one line per task, within ``TEXT_LIMIT`` characters.
    It carries no clock reading, so it changes only when a shown figure does."""
    tasks = tui._by_recency_live_first(snapshot["tasks"])
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
    """Send when nothing was sent for this key yet, or the text changed and the rate bound allows."""
    if not entry:
        return True
    if entry.get("sha") == text_sha(text):
        return False
    return now - float(entry.get("sent_at", 0)) >= min_interval_s


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
    The handle and hash are saved only after the plugin returned."""
    snapshot = model.collect(state_root, now=now, **collect_options)
    text = digest(snapshot)
    key = key_for(snapshot["hostname"])
    state = read_state(state_path)
    entry = state.get(key)
    if not should_send(entry, text, clock(), min_interval_s=min_interval_s):
        return "unchanged" if entry and entry.get("sha") == text_sha(text) else "skipped"
    handle = plugin.publish(key, text, entry.get("handle") if entry else None)
    state[key] = {"sha": text_sha(text), "handle": handle, "sent_at": clock()}
    write_state(state_path, state)
    return "edited" if entry and entry.get("handle") else "sent"


def watch(state_root, plugin, state_path, *, interval: float = DEFAULT_INTERVAL_S, sleep=time.sleep,
          stop=lambda: False, now=lambda: dt.datetime.now(dt.timezone.utc), report=lambda outcome: None,
          **options) -> None:
    """``run_once`` every ``interval`` seconds until ``stop()``; a failing pass is reported and retried."""
    while not stop():
        try:
            report(run_once(state_root, plugin, state_path, now=now(), **options))
        except Exception as exc:
            report(f"failed: {type(exc).__name__}: {exc}")
        if stop():
            break
        sleep(interval)
