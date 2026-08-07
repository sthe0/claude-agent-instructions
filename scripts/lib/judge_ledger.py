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
is O_APPEND itself — the kernel picks the write offset under the inode lock,
so no two writers' bytes ever land at the same offset. That guarantees
offset ordering, NOT that one write() call lands whole in one piece — a short
write is still possible and is handled by _write_all() below, not by
O_APPEND. Lines never carry free-text payload or turn content — only outcome
metadata — because the ledger's job is counting outcomes, not reproducing
what was asked.

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
from dataclasses import dataclass
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

# The ``hook`` field carries the SHORT name each hook passes to hook_start(),
# while every other table that reasons about these same three hooks
# (lib/judge_latency.HOOK_CALL_SEQUENCE, lib/hook_wiring.TIMEOUT_REQUIREMENTS)
# is keyed by script basename. A reader that has to cross from one keying to
# the other needs the translation, and this module owns the ``hook``
# vocabulary, so the translation lives here once instead of as an inline copy
# inside each reader. tests/test_dispatch_witness.py asserts the key set still
# equals HOOK_CALL_SEQUENCE's, so a fourth judge-calling hook cannot be added
# to one table and forgotten in the other.
HOOK_NAME_BY_BASENAME: "dict[str, str]" = {
    "hook-escalation-diagnosis-gate.py": "escalation_diagnosis",
    "hook-deferring-disposition-gate.py": "deferring_disposition",
    "hook-turn-end-gate.py": "turn_end",
}


def ledger_path() -> Path:
    """Where the ledger lives. PUBLIC, unlike agentctl/edit_ledger.py's
    equivalent, because a reader that reports on the ledger has to name the
    file it read — judge-usage-report.py prints the path and its size, and a
    reader reaching into a private to do so would make this module's default
    resolution look like an internal detail two shipped scripts depend on."""
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


def current_hook() -> str | None:
    """The hook name set by hook_start(), or None outside any hook (an
    engine-path call from cli.py). Lets a caller tell "no judge claimed this
    call because nothing has, ever" apart from "no judge claimed this call,
    but we are still inside a hook's own invocation"."""
    with _lock:
        return _state["hook"]


def reset_invocation_outside_hook() -> None:
    """Give the call about to be made its own invocation_id, but ONLY outside a
    hook. Inside a hook the call belongs to that hook's own invocation (set by
    hook_start()), and re-minting here would orphan every line the hook writes
    afterwards — the hook's hook_start would have no matching terminal line, and
    the terminal line would belong to an invocation that never started: one hook
    invocation read as two. Both callers that mint an id for an individual call
    go through here, so what "inside a hook" means is defined once."""
    if current_hook() is None:
        reset_invocation()


def begin_attributed_call(name: str) -> None:
    """Claim the next ``runner(...)`` call for ``name``, on its own
    invocation_id where that is correct (see reset_invocation_outside_hook)."""
    reset_invocation_outside_hook()
    set_current_judge(name)


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
        path = ledger_path()
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
    record = dict(record)
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
    `final`) line is outcome 11 (killed before any verdict, outside a call) —
    visible only as an unpaired hook_start, never written by this function
    itself. Outcome 6 is the neighbouring but distinct shape: killed DURING a
    judge call, which leaves an unpaired `started` too (see `started`). The
    two stay apart because only 6 says a judge was actually running when the
    registration timeout fired."""
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
    reason: str,
    timed_out: bool | None = False,
    malformed: bool = False,
    runner_legacy: bool = False,
    remaining=None,
    threshold=None,
    ceiling=None,
    duration=None,
) -> None:
    """One judge decision point's terminal outcome.

    ``stage`` names where the decision stopped: "budget" (outcome 3),
    "killswitch" (outcome 7c — disabled by the hook's own kill switch),
    "no_text" (no text was given to judge), "no_runner" (enabled, text
    present, but no runner injected), or "call" (outcomes 4, 5, 7, 7a, 7b —
    disambiguated by ``timed_out``/``malformed``/``reason``). Outcome 2
    (the prefilter declining to call the judge at all) is NOT a ``stage``
    here — it never reaches a decision point, so it is recorded on
    ``entered(prefilter_fired=False)`` instead, with no ``decided`` line
    following it.

    ``reason`` has NO default on purpose. The empty string is not a neutral
    placeholder here — judge-usage-report.py reads ``reason == ""`` as the
    signature of outcome 4, an honest verdict — so a caller that simply forgot
    the argument would have recorded the most flattering outcome in the
    taxonomy. Every caller already passes it; requiring it keeps the one value
    that means "the judge answered" reachable only deliberately.

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
    lands as outcome 11 — see `hook_start`) still leaves a trace: an unpaired
    `started` with no following `call` line is exactly outcome 6."""
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


@dataclass(frozen=True)
class LedgerRead:
    """One read of the ledger, WITH what the read itself lost or could not do.

    ``records`` alone cannot answer "is this everything?": a torn line and a
    ledger that could not be opened both arrive as a shorter list, and a
    reader counting outcomes off that list reports a total it has no grounds
    to call complete. The three companion fields say so out loud —
    ``dropped_lines`` counts the lines skipped as malformed, ``error`` carries
    the OSError text when the file could not be read at all, and ``missing``
    separates the ordinary "no hook has written yet" from that failure."""

    records: "list[dict]"
    dropped_lines: int = 0
    error: str = ""
    missing: bool = False


def read_ledger(path: Path | None = None) -> LedgerRead:
    """Read every well-formed record from the ledger, in file order, and
    report what the read lost. A malformed line (partial write, corruption)
    is skipped rather than raising — the ledger is a best-effort
    observability aid, not a transactional store, and one bad line must never
    hide every other — but it is COUNTED, so a reader can say how much of the
    file it is speaking for.

    Decoding is byte-level with ``errors="replace"`` for the same reason: a
    torn multi-byte sequence must cost its own line, not the whole file, which
    is what read_text(encoding="utf-8") did by raising UnicodeDecodeError past
    the OSError guard.

    The whole file is read at once, and nothing rotates it: growth is bounded
    by neither rotation nor a read-side window, on purpose, because rotation
    here would add a second failure mode to the one write path that must not
    have one. The reasoning and the size tripwire that keeps that choice honest
    are in scripts/README.md § Judge execution ledger."""
    target = path if path is not None else ledger_path()
    try:
        raw = target.read_bytes()
    except FileNotFoundError as exc:
        return LedgerRead([], error=str(exc), missing=True)
    except OSError as exc:
        return LedgerRead([], error=str(exc))
    text = raw.decode("utf-8", errors="replace")
    records = []
    dropped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            dropped += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            dropped += 1
    return LedgerRead(records, dropped_lines=dropped)


def read_records(path: Path | None = None) -> list[dict]:
    """Just the well-formed records, for the callers that only ever wanted
    the list. Kept as the plain shape because most of them — the test suite's
    assertions on what a hook wrote — have nothing to say about a torn line;
    a reader that REPORTS on the ledger calls read_ledger() instead, so that
    "the file could not be read" cannot reach the page as "the file is
    empty"."""
    return read_ledger(path).records
