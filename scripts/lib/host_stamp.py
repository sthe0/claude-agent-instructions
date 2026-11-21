"""The host stamp every task record carries, so tasks run on different hosts can be compared.

A stamp is written when a row is written and never added to old rows: a row without it
reads as an unknown host.
"""
from __future__ import annotations

import json
import os

from lib import engine_session_binding

HARNESS_VERSION_ENV = "AGENTCTL_RUNTIME_HOST_VERSION"
STAMP_KEYS = ("host", "harness_version", "models", "root_session_id")


def stamp(host, harness_version, models, root_session_id) -> dict:
    return {
        "host": host,
        "harness_version": harness_version,
        "models": list(models),
        "root_session_id": root_session_id,
    }


def task_key(environ=None) -> str | None:
    """The engine session id a record belongs to: the linked child's own session, else the
    engine session the harness conversation last drove, else None."""
    env = os.environ if environ is None else environ
    own = env.get("AGENTCTL_OWN_SESSION")
    if own:
        return own
    harness = engine_session_binding.harness_session_id(env)
    return engine_session_binding.resolve(harness) if harness else None


def _iter_transcript(session_id: str):
    path = engine_session_binding.transcript_for_session(session_id)
    if path is None:
        return
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


def models_from_transcript(session_id: str | None) -> list[str]:
    """Models of the root transcript's assistant turns, in order of first appearance."""
    seen: list[str] = []
    for entry in _iter_transcript(session_id) if session_id else ():
        message = entry.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and not model.startswith("<") and model not in seen:
            seen.append(model)
    return seen


def version_from_transcript(session_id: str | None) -> str | None:
    for entry in _iter_transcript(session_id) if session_id else ():
        version = entry.get("version")
        if isinstance(version, str) and version:
            return version
    return None


def bind_time_version(environ=None) -> str | None:
    env = os.environ if environ is None else environ
    return env.get(HARNESS_VERSION_ENV) or None


__all__ = [
    "STAMP_KEYS", "HARNESS_VERSION_ENV", "stamp", "task_key", "models_from_transcript",
    "version_from_transcript", "bind_time_version",
]
