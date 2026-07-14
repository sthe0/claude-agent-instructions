"""Startrek (Yandex Tracker) adapter for the difficulty channel.

Maps a DifficultyRecord onto an issue in queue OOSEVENREPORT — the surface an internal
contributor already has write access to (never the protected Core). The mapping is a pure
function (``record_to_fields``) so it is tested without network; the actual POST goes through
an injectable HTTP client so the test substitutes a fake. Auth uses ~/.tracker-token (the
documented Startrek *write* credential; $OAUTH_TOKEN is read-only).

No live POST is performed during construction or in tests — creating OOSEVENREPORT issues is an
outward effect deferred to an explicit, separately-authorized run.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Callable

from ..port import DifficultyChannel, DifficultyRecord, Severity, register_channel

QUEUE = "OOSEVENREPORT"
BACKLOG_QUEUE = "OOSEVEN"
API_BASE = "https://st-api.yandex-team.ru/v2"
TOKEN_PATH = Path.home() / ".tracker-token"

# severity -> Startrek priority key (ADR-0001 adapter table: severity->priority).
# Keys must be accepted by the TARGET QUEUE, not just present in the instance enum:
# OOSEVEN (quickStartV2 preset workflow) takes only the standard five
# (trivial, minor, normal, critical, blocker) — it 422'd both Jira's major and
# the instance-level extra key important (verified live 2026-07-03).
_PRIORITY = {
    Severity.LOW: "minor",
    Severity.MEDIUM: "normal",
    Severity.HIGH: "critical",
    Severity.CRITICAL: "blocker",
}


# Tracker rejects tags containing a comma, tab, or newline, or longer than 480 UTF-8 bytes
# (HTTP 422). Functional grounds routinely contain commas, so the tag is a sanitized
# projection of the ground; the unconstrained full text still lives in summary/description.
_TAG_MAX_BYTES = 480
_TAG_FORBIDDEN = str.maketrans({",": " ", "\t": " ", "\n": " ", "\r": " "})


def _sanitize_tag(ground: str) -> str:
    """Project a functional ground onto a Tracker-legal tag: drop forbidden chars, cap bytes."""
    collapsed = " ".join(ground.translate(_TAG_FORBIDDEN).split())
    return collapsed.encode("utf-8")[:_TAG_MAX_BYTES].decode("utf-8", "ignore")


def record_to_fields(record: DifficultyRecord, queue: str = QUEUE) -> dict:
    """Pure record -> Startrek issue-fields mapping. No I/O. The single tested contract."""
    return {
        "queue": queue,
        "type": {"key": "task"},
        "summary": f"[{record.layer}] {record.functional_ground}"[:254],
        # functional_ground also becomes a tag so the digest's cluster key survives in Tracker.
        "tags": [_sanitize_tag(record.functional_ground)],
        "priority": {"key": _PRIORITY[record.severity]},
        "description": (
            f"Difficulty against `{record.target}` (layer: {record.layer}).\n\n"
            f"Functional ground: {record.functional_ground}\n"
            f"Severity: {record.severity.value}\n"
            f"Reporter: {record.reporter}\n"
            f"Observed: {record.ts}\n\n"
            f"Evidence:\n{record.evidence}"
        ),
    }


def _default_http(method: str, url: str, headers: dict, body: bytes | None) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:  # pragma: no cover - real network
        return json.loads(resp.read().decode("utf-8"))


def _read_token() -> str:
    tok = os.environ.get("TRACKER_TOKEN")
    if tok:
        return tok.strip()
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    raise RuntimeError(f"no tracker write token (set TRACKER_TOKEN or create {TOKEN_PATH})")


class StartrekChannel(DifficultyChannel):
    """Submits difficulties as Startrek issues. Queue and HTTP client are injectable for tests."""

    def __init__(
        self,
        http: Callable[[str, str, dict, bytes | None], dict] | None = None,
        token: str | None = None,
        queue: str = QUEUE,
    ) -> None:
        self._http = http or _default_http
        self._token = token  # lazily read on first real call if None
        self._queue = queue

    def _headers(self) -> dict:
        token = self._token or _read_token()
        return {
            "Authorization": f"OAuth {token}",
            "Content-Type": "application/json",
        }

    def submit(self, record: DifficultyRecord) -> str:
        fields = record_to_fields(record, queue=self._queue)
        body = json.dumps(fields).encode("utf-8")
        resp = self._http("POST", f"{API_BASE}/issues", self._headers(), body)
        return resp.get("key", "")

    def pull(self, since: str | None = None) -> list[DifficultyRecord]:
        query = f"Queue: {self._queue}"
        if since:
            query += f' AND Created: >= "{since}"'
        url = f"{API_BASE}/issues/_search"
        body = json.dumps({"query": query}).encode("utf-8")
        issues = self._http("POST", url, self._headers(), body)
        if not isinstance(issues, list):  # an error body is a dict — don't iterate its keys
            return []
        return [_issue_to_record(i) for i in issues]


def _st_headers(token: str) -> dict:
    return {
        "Authorization": f"OAuth {token}",
        "Content-Type": "application/json",
    }


def add_tag(
    issue_key: str,
    tag: str,
    *,
    http: Callable[[str, str, dict, bytes | None], dict] | None = None,
    token: str | None = None,
) -> None:
    """Add ``tag`` to an existing issue via the add operator (never a full-array replace),
    so tags already on the issue survive."""
    sanitized = _sanitize_tag(tag)
    tok = token or _read_token()
    body = json.dumps({"tags": {"add": [sanitized]}}).encode("utf-8")
    (http or _default_http)("PATCH", f"{API_BASE}/issues/{issue_key}", _st_headers(tok), body)


def add_comment(
    issue_key: str,
    body: str,
    *,
    http: Callable[[str, str, dict, bytes | None], dict] | None = None,
    token: str | None = None,
) -> None:
    """Post a comment to an existing issue (the usage-telemetry sink append path)."""
    tok = token or _read_token()
    payload = json.dumps({"text": body}).encode("utf-8")
    (http or _default_http)("POST", f"{API_BASE}/issues/{issue_key}/comments", _st_headers(tok), payload)


def list_comments(
    issue_key: str,
    *,
    http: Callable[[str, str, dict, bytes | None], dict] | None = None,
    token: str | None = None,
) -> list[dict]:
    """Fetch every comment on an existing issue (read-only)."""
    tok = token or _read_token()
    result = (http or _default_http)("GET", f"{API_BASE}/issues/{issue_key}/comments", _st_headers(tok), None)
    return result if isinstance(result, list) else []


def _issue_to_record(issue: dict) -> DifficultyRecord:
    tags = issue.get("tags") or [""]
    return DifficultyRecord(
        ts=issue.get("createdAt", ""),
        layer="core",
        target=issue.get("summary", ""),
        functional_ground=tags[0] or issue.get("summary", "unknown"),
        severity=_priority_to_severity(issue.get("priority", {}).get("key", "normal")),
        reporter=(issue.get("createdBy") or {}).get("id", "unknown"),
        evidence=issue.get("description", ""),
    )


_PRIORITY_INV = {v: k for k, v in _PRIORITY.items()}


def _priority_to_severity(key: str) -> Severity:
    return _PRIORITY_INV.get(key, Severity.MEDIUM)


register_channel("startrek", StartrekChannel)
