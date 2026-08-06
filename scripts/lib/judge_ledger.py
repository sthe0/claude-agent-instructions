"""Append-only execution ledger for the three judge-calling hooks (hook-
escalation-diagnosis-gate.py, hook-deferring-disposition-gate.py, hook-turn-
end-gate.py), all funneled through agentctl.advisor.subprocess_runner.

Difficulty removed: a judge call that fails open is, by construction,
invisible on every existing observable — the hook still exits 0, the harness
sees no error, and a fabricated False looks byte-identical to an honest NO.
Without a ledger there is no way to tell "the judge said no" from "the judge
never ran" from "the judge ran and the hook discarded the answer" — three
outcomes with the same net effect (allow) and radically different meanings
for whether the gate is doing anything at all.

Every ledger line is one JSON object, newline-terminated, written with
O_APPEND + flush + fsync so a line that lands is durable across a hook
process being killed immediately after (the harness's own registration-
timeout kill is exactly this case). Lines never carry free-text payload or
turn content — only outcome metadata — both to keep every line comfortably
under PIPE_BUF (so a concurrent writer's line can never interleave with an
in-flight one) and because the ledger's job is counting outcomes, not
reproducing what was asked.

Ambient state (invocation_id / source / current judge) exists because the
five production ``runner(...)`` call sites inside the four judge functions
and hook-turn-end-gate.py's ``_judged()`` helper are frozen by a concurrent
change and cannot grow a ledger-context parameter — so each judge function
sets ``set_current_judge(name)`` on the one line immediately before its
existing (untouched) ``runner(...)`` call, and ``subprocess_runner`` reads it
back rather than receiving it as an argument.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from lib import config_root

# PIPE_BUF on Linux — the ceiling that keeps a single line's write atomic even
# under concurrent writers to the same fd. No field here ever holds free text
# long enough to approach this, but a line that somehow would is truncated
# rather than risking a torn write.
_MAX_LINE_BYTES = 2048

_lock = threading.Lock()
_state: dict = {"invocation_id": None, "source": None, "hook": None, "judge": None}


def _ledger_path() -> Path:
    override = os.environ.get("AGENTCTL_JUDGE_LEDGER")
    if override:
        return Path(override).expanduser()
    return config_root.agentctl_judge_ledger_log()


def reset_invocation() -> str:
    """Mint a NEW invocation_id and make it current. Reset-THEN-generate,
    never get-or-create: every call returns a fresh id, so a hook that (mis-)
    calls this twice never collapses two invocations into one id — a bug
    this function's own contract would otherwise hide."""
    new_id = uuid.uuid4().hex
    with _lock:
        _state["invocation_id"] = new_id
    return new_id


def current_invocation_id() -> str:
    """The current invocation_id. Mints one via reset_invocation() the first
    time it is called with none set yet — a manual/test call site that writes
    a ledger line without ever calling hook_start()."""
    with _lock:
        existing = _state["invocation_id"]
    if existing is not None:
        return existing
    return reset_invocation()


def set_source(source: str) -> None:
    """Record this invocation's source signature — a session_id, or an
    explicit manual-run tag a test/operator sets by hand."""
    with _lock:
        _state["source"] = source


def current_source() -> str:
    with _lock:
        return _state["source"] or "unknown"


def set_current_judge(name: str | None) -> None:
    """Ambient carrier the four judge functions set to their own name on the
    line immediately before their frozen ``runner(...)`` call, so
    ``subprocess_runner`` — which receives no judge-name argument — can
    attribute the call it is about to make."""
    with _lock:
        _state["judge"] = name


def current_judge() -> str | None:
    with _lock:
        return _state["judge"]


def _source_from_payload(payload) -> str:
    if isinstance(payload, dict):
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return "manual"


def _write(kind: str, **fields) -> None:
    record = {
        "ts": time.time(),
        "kind": kind,
        "invocation_id": current_invocation_id(),
        "hook": _state.get("hook"),
        "source": current_source(),
    }
    record.update(fields)
    line = json.dumps(record, ensure_ascii=True, sort_keys=True)
    encoded = (line + "\n").encode("utf-8")
    if len(encoded) > _MAX_LINE_BYTES:
        # Drop the reason text first — it is the only field with unbounded
        # length — before giving up on the write entirely.
        record.pop("reason", None)
        line = json.dumps(record, ensure_ascii=True, sort_keys=True)
        encoded = (line + "\n").encode("utf-8")
    if len(encoded) > _MAX_LINE_BYTES:
        encoded = encoded[: _MAX_LINE_BYTES - 1] + b"\n"
    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # A ledger write must never be the reason a hook fails open in a NEW
        # way — the hooks this module instruments already fail open on their
        # own account; losing one ledger line is strictly better than adding
        # a second failure mode on top of the one being observed.
        pass


def hook_start(hook: str) -> str:
    """First statement of a hook's main(), before stdin is even parsed: mint
    a fresh invocation_id, record the hook name, and write the hook_start
    line. An invocation killed between this call and its first `decided` (or
    `final`) line is exactly outcome 6 (registration-timeout kill) — visible
    only as an unpaired hook_start, never written by this function itself."""
    invocation_id = reset_invocation()
    with _lock:
        _state["hook"] = hook
        _state["judge"] = None
        _state["source"] = None
    _write("hook_start", hook=hook)
    return invocation_id


def source_from_payload(payload) -> None:
    """Call once the hook has successfully parsed its stdin payload, to
    upgrade the source signature from "unknown" to the payload's session_id
    (or "manual" when the payload carries none)."""
    set_source(_source_from_payload(payload))


def entered(judge: str, *, prefilter_fired: bool) -> None:
    """This judge's decision point was reached. ``prefilter_fired=False`` is
    the terminal state for this opportunity (outcome 2) — no `decided` line
    follows it."""
    _write("entered", judge=judge, prefilter_fired=bool(prefilter_fired))


def decided(
    judge: str,
    *,
    stage: str,
    verdict,
    reason: str = "",
    timed_out: bool = False,
    malformed: bool = False,
    remaining=None,
    threshold=None,
    ceiling=None,
    duration=None,
) -> None:
    """One judge decision point's terminal outcome.

    ``stage`` names where the decision stopped: "prefilter" (outcome 2),
    "budget" (outcome 3), "disabled" (outcome 7c), or "call" (outcomes 4, 5,
    7, 7a, 7b — disambiguated by ``timed_out``/``malformed``/``reason``).

    ``remaining``/``threshold``/``ceiling`` are the triple this stage's plan
    requires on every call decision: the budget remainder at entry, the
    active per-call threshold, and the ceiling passed to the call (or to the
    budget check that denied one). ``duration`` is the call's wall-clock
    length in seconds, set only when ``stage == "call"``."""
    _write(
        "decided",
        judge=judge,
        stage=stage,
        verdict=verdict,
        reason=reason,
        timed_out=bool(timed_out),
        malformed=bool(malformed),
        remaining=remaining,
        threshold=threshold,
        ceiling=ceiling,
        duration=duration,
    )


def started(judge: str) -> None:
    """Written by subprocess_runner immediately before the actual subprocess
    call, so a kill DURING the call (as opposed to during hook setup, which
    hook_start already covers) still leaves a trace: an unpaired `started`
    with no following `call` line is a call-site instance of outcome 6."""
    _write("started", judge=judge)


def call(judge: str, *, timed_out: bool, duration: float, returncode, raised: str | None = None) -> None:
    """Mechanical fact about one subprocess_runner call, written from inside
    subprocess_runner itself (the true source of truth for duration and
    timed_out) rather than reconstructed by the judge function that invoked
    it. ``raised`` carries the exception's repr when subprocess_runner's own
    try/except caught something other than TimeoutExpired (outcome 7b) —
    None on every other path."""
    _write(
        "call",
        judge=judge,
        timed_out=bool(timed_out),
        duration=duration,
        returncode=returncode,
        raised=raised,
    )


def final(*, has_directive: bool) -> None:
    """decide() returned, successfully, with or without a directive to
    emit. Written BEFORE the emission attempt, so a kill between this line
    and `emitted` is outcome 10 (verdict rendered, hook killed before
    emission) when ``has_directive`` was True, distinguishable from outcome
    11 (no verdict at all) by the absence of any `final` line in that case."""
    _write("final", has_directive=bool(has_directive))


def emitted(*, ok: bool, had_directive: bool) -> None:
    """Written UNCONDITIONALLY after the delivery step, whether or not there
    was anything to deliver (``had_directive=False`` on a plain allow) and
    whether or not the delivery attempt itself raised (``ok=False`` — outcome
    9: verdict rendered but not emitted, e.g. a json.dumps/print failure)."""
    _write("emitted", ok=bool(ok), had_directive=bool(had_directive))


def discarded(reason: str) -> None:
    """The hook's outer exception handler caught something after decide()
    was entered — the verdict, if any was computed, is DISCARDED rather than
    delivered (outcome 8). ``reason`` is a short repr of what was caught, not
    a stack trace."""
    _write("discarded", reason=reason)


def read_records(path: Path | None = None) -> list[dict]:
    """Read every well-formed record from the ledger, in file order. A
    malformed line (partial write, corruption) is skipped rather than
    raising — the ledger is a best-effort observability aid, not a
    transactional store, and one bad line must never hide every other."""
    target = path if path is not None else _ledger_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records
