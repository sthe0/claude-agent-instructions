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

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from lib import config_root

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


def record(mechanism: str, event: str, decision: str, session: str, *, layer: str = "core") -> None:
    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mechanism": str(mechanism)[:_FIELD_MAX_CHARS],
        "event": str(event)[:_FIELD_MAX_CHARS],
        "decision": decision,
        "session": str(session)[:_FIELD_MAX_CHARS],
        "test": is_test_run(session),
        "layer": layer,
    }
    try:
        path = config_root.decision_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + chr(10))
    except OSError:
        return


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


def run(main, *, mechanism: str, layer: str = "core"):
    """Run ``main`` and return its result; stdout and the exit code reach the caller
    exactly as ``main`` produced them. Use as ``sys.exit(run(main, mechanism=...))``."""
    try:
        capture = tempfile.TemporaryFile()
    except OSError:
        return main()
    real_stdin = sys.stdin
    sys.stdin = recorder = _RecordingStdin(real_stdin)
    sys.stdout.flush()
    saved_fd = os.dup(1)
    os.dup2(capture.fileno(), 1)
    result = None
    failure = None
    try:
        result = main()
    except BaseException as exc:
        failure = exc
    finally:
        sys.stdout.flush()
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
        sys.stdin = real_stdin
    capture.seek(0)
    captured = capture.read()
    capture.close()
    _emit(captured)

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
    if failure is not None:
        raise failure
    return result
