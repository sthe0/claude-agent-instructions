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
O_APPEND + fsync so a line that lands is durable across a hook process being
killed immediately after (the harness's own registration-timeout kill is
exactly this case). What keeps two concurrent hook processes from interleaving
is O_APPEND itself — the kernel picks the write offset and appends under the
inode lock, so one write() lands whole at the end of the file. Lines never
carry free-text payload or turn content — only outcome metadata — because the
ledger's job is counting outcomes, not reproducing what was asked.

Ambient state (invocation_id / source / current judge) exists because the
five production ``runner(...)`` call sites inside the four judge functions
and hook-turn-end-gate.py's ``_judged()`` helper are frozen by a concurrent
change and cannot grow a ledger-context parameter — so each judge function
sets ``set_current_judge(name)`` on the one line immediately before its
existing (untouched) ``runner(...)`` call, and ``subprocess_runner`` reads it
back with ``take_current_judge()`` — consume-once, so the carrier attributes
one call and never leaks a stale name onto the next.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from lib import config_root

# Ceiling on one ledger line. NOT an atomicity mechanism — O_APPEND is what
# keeps concurrent writers from interleaving, and PIPE_BUF (4096) governs pipes,
# not this regular file. The cap exists to bound the one field with unbounded
# length (`reason`, which carries a judge's own answer text) so a runaway line
# cannot make the ledger costlier to read than the outcomes it records.
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


def take_current_judge() -> str | None:
    """Read the ambient judge name AND clear it, so the carrier attributes
    exactly one call. Consume-once matters because the carrier is process-wide:
    a second ``runner(...)`` call made later in the same process without a judge
    function in front of it (an engine-path advisory call from cli.py) would
    otherwise be filed under whichever judge ran last."""
    with _lock:
        name = _state["judge"]
        _state["judge"] = None
    return name


def _source_from_payload(payload) -> str:
    if isinstance(payload, dict):
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return "manual"


def _write(kind: str, **fields) -> None:
    # EVERYTHING is inside the try, serialization and path resolution included:
    # a ledger write must never be the reason a hook fails open in a NEW way —
    # the hooks this module instruments already fail open on their own account,
    # and losing one ledger line is strictly better than adding a second failure
    # mode on top of the one being observed. hook_start() in particular runs
    # BEFORE each hook's own try, so an exception escaping here would take the
    # whole hook down before it has any handler of its own.
    try:
        record = {
            "ts": time.time(),
            "kind": kind,
            "invocation_id": current_invocation_id(),
            "hook": _state.get("hook"),
            "source": current_source(),
        }
        record.update(fields)
        encoded = _encode(record)
        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            _write_all(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def _encode(record: dict) -> bytes:
    line = json.dumps(record, ensure_ascii=True, sort_keys=True, default=repr)
    encoded = (line + "\n").encode("utf-8")
    if len(encoded) > _MAX_LINE_BYTES:
        # Drop the reason text first — it is the only field with unbounded
        # length — before giving up on the write entirely.
        record.pop("reason", None)
        line = json.dumps(record, ensure_ascii=True, sort_keys=True, default=repr)
        encoded = (line + "\n").encode("utf-8")
    if len(encoded) > _MAX_LINE_BYTES:
        encoded = encoded[: _MAX_LINE_BYTES - 1] + b"\n"
    return encoded


def _write_all(fd: int, encoded: bytes) -> None:
    """Write every byte, since os.write may write fewer than asked (a signal
    arriving mid-write, a filesystem returning short). A resumed write appends
    at the file's new end, so a line torn this way stays torn — but it lands as
    ONE malformed line read_records() skips, which is what ignoring the return
    value could not guarantee: a silently dropped tail merges the next record
    into this one and corrupts a well-formed line too."""
    written = 0
    while written < len(encoded):
        count = os.write(fd, encoded[written:])
        if count <= 0:
            break
        written += count


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
    timed_out: bool | None = False,
    malformed: bool = False,
    runner_legacy: bool = False,
    remaining=None,
    threshold=None,
    ceiling=None,
    duration=None,
) -> None:
    """One judge decision point's terminal outcome.

    ``stage`` names where the decision stopped: "prefilter" (outcome 2),
    "budget" (outcome 3), "disabled" (outcome 7c and its siblings — no runner
    injected, or no text to judge, each named by its own ``reason``), or "call"
    (outcomes 4, 5, 7, 7a, 7b — disambiguated by
    ``timed_out``/``malformed``/``reason``).

    ``timed_out`` is three-valued: True/False when the runner reported it, and
    None when the runner carried no such field at all — a runner predating the
    flag cannot tell a timeout from a fast failure, and writing False there
    would record an unknown as a fact. ``runner_legacy`` marks that case, so a
    reader can separate "no timeout" from "no answer about timeouts".

    ``malformed`` is narrow ON PURPOSE: the call returned, and its answer could
    not be parsed into a verdict (outcome 7a). A timeout (5), a non-zero exit
    (7) and an exception (7b) each produced no answer to be malformed about and
    are named by ``timed_out``/``reason`` instead — marking them malformed too
    would count one fail-open outcome under two headings.

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
        timed_out=None if timed_out is None else bool(timed_out),
        malformed=bool(malformed),
        runner_legacy=bool(runner_legacy),
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
    transactional store, and one bad line must never hide every other.

    Decoding is byte-level with ``errors="replace"`` for the same reason: a
    torn multi-byte sequence must cost its own line, not the whole file, which
    is what read_text(encoding="utf-8") did by raising UnicodeDecodeError past
    the OSError guard.

    The whole file is read at once. That is adequate while nothing rotates it
    and no reader ships — the bound on ledger growth is stage 6's decision (see
    scripts/README.md § Judge execution ledger), deliberately not made here."""
    target = path if path is not None else _ledger_path()
    try:
        raw = target.read_bytes()
    except OSError:
        return []
    text = raw.decode("utf-8", errors="replace")
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
