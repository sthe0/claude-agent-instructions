"""Decision telemetry: one row per hook evaluation, silent allows included.

Difficulty removed: only refusals of some engine gates and six judge-calling hooks
left any trace, so a live mechanism that only ever passes silently looked
identical to a dead one. A hook adopts this with one line,

    if __name__ == "__main__": sys.exit(hook_telemetry.run(main, mechanism="hook-x"))

run() captures the hook's stdout at descriptor level, lets the hook finish,
re-emits stdout and the exit code unchanged (a crash re-raises), classifies the
decision and appends one JSON row to config_root.decision_log(). See
docs/operations/decision-telemetry.md for the row schema, the gate population and the
launcher classification.

The write is one O_APPEND write of a short line (judge_ledger's append
discipline): no fsync and no subprocess, because this runs on every tool call, and
fail-open, because telemetry must never change what a hook decides. An uncreatable
$AGENTCTL_DECISION_LOG switches telemetry off.

Stdlib and config_root only, like judge_ledger: a hook imports this first.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from lib import config_root

try:
    from lib.telemetry import live
except Exception:
    live = None

SILENT, NUDGE, DENY = "silent", "nudge", "deny"

_FIELD_MAX_CHARS = 256


def is_test_run(session: str) -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("AGENT_TELEMETRY_TEST") == "1" or session == "agentctl-test-session")


def decision_class(stdout: str, exit_code) -> str:
    if exit_code == 2:
        return DENY
    text = stdout.strip()
    if not text:
        return SILENT
    try:
        payload = json.loads(text)
    except ValueError:
        return NUDGE
    if isinstance(payload, dict):
        if payload.get("decision") == "block":
            return DENY
        specific = payload.get("hookSpecificOutput")
        if isinstance(specific, dict) and specific.get("permissionDecision") == "deny":
            return DENY
    return NUDGE


def _field(value) -> str:
    """Printable ASCII only, so the encoded line stays under 2048 bytes: three fields of
    at most 256 characters, each at worst doubled by JSON escaping of quote and backslash."""
    return "".join(c if " " <= c <= "~" else "?" for c in str(value)[:_FIELD_MAX_CHARS])


def decision_row(mechanism: str, event: str, decision: str, session: str, *, layer: str = "core") -> dict:
    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mechanism": _field(mechanism),
        "event": _field(event),
        "decision": decision,
        "session": _field(session),
        "test": is_test_run(session),
        "layer": layer,
    }


def decision_event_fields(row: dict) -> dict:
    """What a decision event keeps of its decision-log row: the verdict's identity, never a payload."""
    return {key: row[key] for key in ("mechanism", "event", "decision", "layer", "test")}


def append_decision_row(path, row: bytes, mode: int = 0o600) -> "tuple[str, int, str]":
    """Append one encoded row to the decision log and return its back-reference
    (path, offset, sha256 of `row`), in the form live.append_line returns.

    A write that failed on a full disk can leave a row without its newline; the next row
    would be glued onto it and both lost to a JSON reader. So when the file's last byte is
    not a newline, one newline goes in front of the row, inside the same single O_APPEND
    write. A short write raises OSError(ENOSPC) instead of passing as success. OSError
    propagates; each writer keeps its own fail-open."""
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_APPEND, mode)
    try:
        size = os.fstat(fd).st_size
        torn = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
        data = (b"\n" if torn else b"") + row
        if os.write(fd, data) < len(data):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(path))
        end = os.lseek(fd, 0, os.SEEK_CUR)
    finally:
        os.close(fd)
    return str(path), end - len(row), hashlib.sha256(row).hexdigest()


def record(mechanism: str, event: str, decision: str, session: str, *, layer: str = "core") -> None:
    row = decision_row(mechanism, event, decision, session, layer=layer)
    try:
        backref = append_decision_row(config_root.decision_log(), (json.dumps(row) + chr(10)).encode("utf-8"))
    except OSError:
        return
    if live is None:
        return
    try:
        live.emit_event(session, "decision", f"hook:{row['mechanism']}", decision_event_fields(row), backref)
    except Exception:
        pass


class _RecordingStdin:
    """Hands the hook its stdin unchanged while keeping the text it read, so the
    event and session are known without consuming the stream first (a CLI-mode
    invocation that never reads stdin must not block on it)."""

    def __init__(self, stream):
        self._stream = stream
        self.text = ""

    def read(self, *args):
        data = self._stream.read(*args)
        self.text += data
        return data

    def readline(self, *args):
        data = self._stream.readline(*args)
        self.text += data
        return data

    def __iter__(self):
        for line in self._stream:
            self.text += line
            yield line

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _payload(text: str) -> dict:
    try:
        payload = json.loads(text)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _emit(data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(1, view):]


def _exit_status(code) -> int:
    if code is None:
        return 0
    return code if isinstance(code, int) else 1


def _open_capture():
    """An in-memory file where the kernel has one: a full disk must not be able to
    swallow a hook's refusal. Without ``memfd_create`` (macOS) the fallback is a temp
    file, and a hook printing past Python's ~8 KB buffer onto a full disk then fails in
    its own ``print`` — a known, accepted gap of the fallback only."""
    if hasattr(os, "memfd_create"):
        try:
            return os.fdopen(os.memfd_create("hook-stdout"), "w+b")
        except OSError:
            pass
    return tempfile.TemporaryFile()


def _redirect_stdout():
    """Point descriptor 1 at a capture file; ``(capture, saved_fd)``, or None when that is
    not possible, in which case the hook runs unwrapped."""
    try:
        capture = _open_capture()
    except OSError:
        return None
    try:
        sys.stdout.flush()
        saved_fd = os.dup(1)
    except Exception:
        capture.close()
        return None
    try:
        os.dup2(capture.fileno(), 1)
    except OSError:
        os.close(saved_fd)
        capture.close()
        return None
    return capture, saved_fd


def _restore_stdout(saved_fd: int) -> None:
    """Descriptor 1 is always put back, whatever the capture did. Bytes a failed capture
    write left in Python's buffer reach the real stdout on the second flush."""
    try:
        sys.stdout.flush()
    except Exception:
        pass
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _read_capture(capture) -> bytes:
    try:
        capture.seek(0)
        return capture.read()
    except OSError:
        return b""
    finally:
        capture.close()


def run(main, *, mechanism: str, layer: str = "core"):
    """Run ``main`` and return its result; stdout and the exit code reach the caller
    exactly as ``main`` produced them. Use as ``sys.exit(run(main, mechanism=...))``."""
    redirected = _redirect_stdout()
    if redirected is None:
        return main()
    capture, saved_fd = redirected
    real_stdin = sys.stdin
    sys.stdin = recorder = _RecordingStdin(real_stdin)
    result = None
    failure = None
    try:
        result = main()
    except BaseException as exc:
        failure = exc
    finally:
        _restore_stdout(saved_fd)
        sys.stdin = real_stdin
    captured = _read_capture(capture)
    try:
        _emit(captured)
    except OSError:
        pass

    try:
        if failure is None:
            status = _exit_status(result)
        elif isinstance(failure, SystemExit):
            status = _exit_status(failure.code)
        else:
            status = 1
        payload = _payload(recorder.text)
        session = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown"
        event = payload.get("hook_event_name") or "unknown"
        record(mechanism, event, decision_class(captured.decode("utf-8", errors="replace"), status), session, layer=layer)
    except Exception:
        pass
    if failure is not None:
        raise failure
    return result
