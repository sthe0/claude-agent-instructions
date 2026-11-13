"""The agentctl command recorder: what `cli.main` writes around every verb.

Difficulty removed: the engine's acts had to reach the record without per-handler emission code
and without slowing the command. `begin` and `finish` bracket one verb: a command event (verb,
outcome, node before and after, duration, recorder time), a transition event when the node moved,
the task identity events of a depth-0 start or reset, the check events of `verify-final`, then one
replay of the session's spool under the store's single 250 ms deadline and, when other spools wait,
one detached `telemetry.py sync --stale` under the sweep lock. No git, harness or transcript read
happens here; the detached sync does those.

Task identity: a task row opens only at depth 0, its epoch the id of the opening `task` event. The
session's open epoch lives in the `<session>.epoch` sidecar (live.py), so a harness resume of the
same engine session carries on under it and every later event names it in `task_epoch`. A reset
to another task, or to the same task without `--continue-epoch` while the epoch is unresolved,
abandons the old epoch; an epoch that reached RESOLVED is never reopened.

A verb cut short by SIGTERM or SIGINT after it saved its state still gets its command row
(`record_cut_short`, outcome `interrupted`, from `cli.main`), once and without the replay; a
SIGKILL or an OOM kill after the save still loses it.

Everything here is fail-open and a no-op under AGENTCTL_TELEMETRY=off.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from lib import config_root
from lib.telemetry import emit, live, store

SWEEP_LOCK = ".sweep.lock"
SWEEP_STAMP = ".sweep.stamp"
SWEEP_MIN_INTERVAL_S = 60.0
SCRIPTS_DIR = Path(__file__).resolve().parents[2]
RESOLVED = "RESOLVED"
OPENING_VERBS = ("start", "reset")


SYNC_BEFORE_SAVE_ENV = "AGENTCTL_TEST_SYNC_BEFORE_SAVE"
SYNC_AFTER_SAVE_ENV = "AGENTCTL_TEST_SYNC_AFTER_SAVE"
SYNC_AFTER_ROW_ENV = "AGENTCTL_TEST_SYNC_AFTER_ROW"


@dataclass
class Context:
    verb: str
    session: str
    started: float
    recorder_base: float
    node_before: "str | None"
    row_emitted: bool = False


def _under_tmp(path) -> bool:
    if path is None:
        return False
    try:
        return Path(path).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, TypeError, ValueError):
        return False


def sync_point(env: str, state_store) -> None:
    """Block on the FIFO named by `env` until its writer closes it, so a test can deliver a signal
    at this exact point of a verb. Honoured only while both the record and the state root are
    temporary, so no live run can be made to wait here."""
    fifo = os.environ.get(env)
    if not fifo or not (_under_tmp(config_root.telemetry_db()) and _under_tmp(getattr(state_store, "root", None))):
        return
    try:
        with open(fifo, "rb") as reader:
            reader.read()
    except OSError:
        pass


def _node_of(state_store, session: str) -> "str | None":
    try:
        state = state_store.load(session)
    except Exception:
        return None
    return state.node if state is not None else None


def _load_state(state_store, session: str):
    try:
        return state_store.load(session)
    except Exception:
        return None


def begin(verb: str, session, state_store) -> "Context | None":
    """Open the bracket for one verb; None when nothing is to be recorded."""
    if not live.enabled() or not isinstance(session, str) or emit.session_file_stem(session) is None:
        return None
    live.set_verb(verb)
    try:
        node_before = None if verb in OPENING_VERBS else _node_of(state_store, session)
        return Context(verb, session, time.perf_counter(), live.recorder_total(), node_before)
    except Exception:
        return None


def _synthetic() -> int:
    return 1 if os.environ.get(live.SYNTHETIC_ENV) == "1" else 0


def _record_identity(ctx: Context, args, state) -> None:
    """The events of a start or reset that built a fresh session state."""
    session, verb = ctx.session, ctx.verb
    source = live.act_key(verb)
    parent_link = state.parent_link if state is not None else None
    depth = int(state.recursion_depth or 0) if state is not None else 0
    task_id = getattr(args, "task", None) or (state.task_id if state is not None else None)
    live.emit_event(session, "session", source, {
        "depth": depth, "task_id": task_id, "linked": bool(parent_link),
        "parent_session": parent_link.get("parent_session") if parent_link else None,
    })
    if depth != 0 or parent_link is not None:
        return
    prior = live.read_epoch(session)
    continue_epoch = getattr(args, "continue_epoch", None)
    current = prior["epoch"] if prior and not prior.get("resolved") else None
    if continue_epoch:
        live.emit_event(session, "task", source, {"action": "continue", "task_id": task_id, "epoch": continue_epoch})
        epoch = continue_epoch
    else:
        epoch = live.emit_event(session, "task", source, {
            "action": "open", "task_id": task_id, "synthetic": _synthetic(), "task_epoch": None})
        if epoch is None:
            return
    if current is not None and current != epoch:
        live.emit_event(session, "task", source, {
            "action": "abandon", "task_id": prior["task_id"], "epoch": current, "task_epoch": current})
    live.write_epoch(session, task_id, epoch)


def _record_resolution(session: str) -> None:
    prior = live.read_epoch(session)
    if prior is not None and not prior.get("resolved"):
        live.write_epoch(session, prior["task_id"], prior["epoch"], resolved=True)


def _check_events(ctx: Context, state) -> None:
    """One check event per check the verify-final run just logged, with the plan digest and the
    code ref of the stage the check belongs to (the latest delivered head for a final check)."""
    history = state.history
    runs = [i for i, entry in enumerate(history) if entry.get("event") == "verify_final_run"]
    if not runs:
        return
    end = runs[-1]
    start = runs[-2] + 1 if len(runs) > 1 else 0
    heads = {stage.index: getattr(stage.outcome, "delivered_head", None) for stage in state.stages
             if getattr(stage, "outcome", None) is not None}
    latest = next((head for head in reversed(list(heads.values())) if head), None)
    for entry in history[start:end]:
        if entry.get("event") != "verify_final_check":
            continue
        check = str(entry.get("check", ""))
        code_ref = latest
        if check.startswith("stage:"):
            try:
                code_ref = heads.get(int(check.split(":", 1)[1])) or latest
            except ValueError:
                pass
        live.emit_event(ctx.session, "check", live.act_key(ctx.verb), {
            "check": check, "venue": entry.get("venue"), "decision": entry.get("decision"),
            "reason": entry.get("reason"), "duration_s": entry.get("duration_s"), "exit": entry.get("exit"),
            "plan_digest": state.accepted_plan_digest, "code_ref": code_ref})


def finish(ctx: "Context | None", args, state_store, directive) -> None:
    """Record the verb that just ran: its events, then one replay and the sweep. Never raises."""
    if ctx is None:
        return
    try:
        _finish(ctx, args, state_store, directive)
    except Exception:
        pass


def _command_fields(ctx: Context, outcome: str, node_after) -> dict:
    return {
        "command": ctx.verb, "outcome": outcome, "node_before": ctx.node_before, "node_after": node_after,
        "duration_s": round(time.perf_counter() - ctx.started, 6),
        "recorder_s": round(live.recorder_total() - ctx.recorder_base, 6),
    }


def record_cut_short(ctx: "Context | None", state_store, outcome: str, extra: dict) -> None:
    """The command row of a verb cut short after it saved its state, unless `finish` already wrote
    it. No replay and no sweep: the row waits in the spool for the next one. Never raises."""
    if ctx is None or ctx.row_emitted:
        return
    try:
        fields = {**_command_fields(ctx, outcome, _node_of(state_store, ctx.session)), **extra}
        live.emit_event(ctx.session, "command", live.act_key(ctx.verb), fields)
        ctx.row_emitted = True
    except Exception:
        pass


def _finish(ctx: Context, args, state_store, directive) -> None:
    verb, session = ctx.verb, ctx.session
    node_after = directive.node
    opened = verb in OPENING_VERBS and directive.ok and directive.action != "continue"
    needs_state = opened or verb in ("classify", "verify-final")
    state = _load_state(state_store, session) if needs_state else None
    if opened:
        _record_identity(ctx, args, state)
    if node_after == RESOLVED:
        _record_resolution(session)
    outcome = "error" if directive.action == "error" else ("ok" if directive.ok else "refused")
    fields = _command_fields(ctx, outcome, node_after)
    if verb == "classify" and state is not None:
        fields["weight_class"] = state.weight_class
    live.emit_event(session, "command", live.act_key(verb), fields)
    ctx.row_emitted = True
    sync_point(SYNC_AFTER_ROW_ENV, state_store)
    if ctx.node_before != node_after and ctx.node_before is not None:
        live.emit_event(session, "transition", live.act_key(verb), {
            "command": verb, "from": ctx.node_before, "to": node_after})
    if verb == "verify-final" and state is not None:
        _check_events(ctx, state)
    replay_on_command_path(session)
    maybe_sweep(session)


def replay_on_command_path(session: str) -> dict:
    """Apply the session's spool once under the store's single deadline and record how long it
    took as a recorder event, attributed to the session whose command ran it."""
    started = time.perf_counter()
    result = store.replay(session)
    live.emit_event(session, "recorder", "telemetry:recorder", {
        "replay_s": round(time.perf_counter() - started, 6), "inserted": result["inserted"],
        "deferred": result["deferred"], "drained": result["drained"], "failed": result["failed"]})
    return result


# --- the command-path sweep ----------------------------------------------------------

def _waiting_spools(own: str) -> "list[Path]":
    try:
        return [Path(entry.path) for entry in os.scandir(live.spool_dir())
                if entry.name.endswith(emit.SPOOL_SUFFIX) and entry.name != own + emit.SPOOL_SUFFIX]
    except OSError:
        return []


def _spawn_sweep(lock_fd: int) -> None:
    """Start `telemetry.py sync --stale` detached; the child inherits the sweep lock, so it is held
    until the sync exits."""
    subprocess.Popen(
        [sys.executable, str(SCRIPTS_DIR / "telemetry.py"), "sync", "--stale"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, pass_fds=(lock_fd,), cwd=str(SCRIPTS_DIR))


def maybe_sweep(session: str) -> bool:
    """Start a detached stale sync when another spool is waiting and the last sweep is older than
    SWEEP_MIN_INTERVAL_S or older than that spool; never waits for it. True when one was started."""
    spools = _waiting_spools(emit.session_file_stem(session) or session)
    if not spools:
        return False
    spool_dir = live.spool_dir()
    stamp = spool_dir / SWEEP_STAMP
    try:
        stamp_time = stamp.stat().st_mtime
    except OSError:
        stamp_time = 0.0
    newest = 0.0
    for path in spools:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass
    if newest <= stamp_time and time.time() - stamp_time < SWEEP_MIN_INTERVAL_S:
        return False
    try:
        fd = os.open(spool_dir / SWEEP_LOCK, os.O_RDWR | os.O_CREAT, emit.PRIVATE_FILE_MODE)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        _spawn_sweep(fd)
        stamp.touch()
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
