"""GitHub Issues adapter for the difficulty channel.

Maps a DifficultyRecord onto a GitHub issue in sthe0/claude-agent-instructions — the surface
an external contributor already has write access to (never the protected Core). The mapping is
a pure function (``record_to_fields``) so it is tested without network; the actual POST goes
through an injectable HTTP client so tests substitute a fake. Auth uses GITHUB_TOKEN env var,
~/.github-token, or the `gh auth token` CLI output.

No live POST is performed during construction or in tests — creating issues is an outward
effect deferred to an explicit, separately-authorized run.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable

from ..port import DifficultyChannel, DifficultyRecord, Severity, register_channel

REPO = "sthe0/claude-agent-instructions"
API_BASE = "https://api.github.com"
TOKEN_PATH = Path.home() / ".github-token"

# Always-present label so the digest can filter difficulty records in one query.
DIFFICULTY_LABEL = "difficulty"
BACKLOG_LABEL = "backlog"


def record_to_fields(record: DifficultyRecord, stream: str = "report") -> dict:
    """Pure record -> GitHub issue fields mapping. No I/O. The single tested contract."""
    body = (
        f"**Target:** `{record.target}`\n"
        f"**Layer:** {record.layer}\n"
        f"**Functional ground:** {record.functional_ground}\n"
        f"**Severity:** {record.severity.value}\n"
        f"**Reporter:** {record.reporter}\n"
        f"**Observed:** {record.ts}\n\n"
        f"**Evidence:**\n{record.evidence}"
    )
    stream_label = BACKLOG_LABEL if stream == "backlog" else DIFFICULTY_LABEL
    return {
        "title": f"[{record.layer}] {record.functional_ground}"[:256],
        "body": body,
        "labels": [
            f"severity:{record.severity.value}",
            f"layer:{record.layer}",
            stream_label,
        ],
    }


def _default_http(method: str, url: str, headers: dict, body: bytes | None) -> object:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:  # pragma: no cover - real network
        return json.loads(resp.read().decode("utf-8"))


def _read_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok.strip()
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    raise RuntimeError(
        f"no GitHub write token (set GITHUB_TOKEN, create {TOKEN_PATH}, or run `gh auth login`)"
    )


# Fully-qualified issue refs only: owner/repo#N, owner/repo/issues/N, or a full URL.
# A bare N/#N carries no repo and must never be defaulted to REPO — reject it instead.
_ISSUE_REF_RE = re.compile(
    r"^(?:https?://github\.com/)?(?P<repo>[\w.-]+/[\w.-]+)(?:#|/issues/)(?P<number>\d+)/?$"
)


def _parse_issue_ref(issue_ref: str) -> tuple[str, int]:
    """Parse a fully-qualified GitHub issue ref into (repo, number). Raises ValueError on a
    bare number/`#N` (no repo) rather than silently defaulting to REPO."""
    match = _ISSUE_REF_RE.match(issue_ref.strip())
    if not match:
        raise ValueError(
            f"issue ref must be a fully-qualified 'owner/repo#N', 'owner/repo/issues/N', "
            f"or a github.com URL — got {issue_ref!r}"
        )
    return match.group("repo"), int(match.group("number"))


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def add_label(
    issue_ref: str,
    label: str,
    *,
    repo: str | None = None,
    http: Callable[[str, str, dict, bytes | None], object] | None = None,
    token: str | None = None,
) -> None:
    """Add ``label`` to an existing issue (additive, idempotent — does not create the label;
    a missing label 422s, which callers treat as a fail-open skip)."""
    parsed_repo, number = _parse_issue_ref(issue_ref)
    target_repo = repo or parsed_repo
    tok = token or _read_token()
    body = json.dumps([label]).encode("utf-8")
    url = f"{API_BASE}/repos/{target_repo}/issues/{number}/labels"
    (http or _default_http)("POST", url, _gh_headers(tok), body)


def add_comment(
    issue_ref: str,
    body: str,
    *,
    repo: str | None = None,
    http: Callable[[str, str, dict, bytes | None], object] | None = None,
    token: str | None = None,
) -> None:
    """Post a comment to an existing issue (the usage-telemetry sink append path). Same
    fully-qualified-ref rule as add_label — a bare number/`#N` raises ValueError."""
    parsed_repo, number = _parse_issue_ref(issue_ref)
    target_repo = repo or parsed_repo
    tok = token or _read_token()
    payload = json.dumps({"body": body}).encode("utf-8")
    url = f"{API_BASE}/repos/{target_repo}/issues/{number}/comments"
    (http or _default_http)("POST", url, _gh_headers(tok), payload)


def list_comments(
    issue_ref: str,
    *,
    repo: str | None = None,
    http: Callable[[str, str, dict, bytes | None], object] | None = None,
    token: str | None = None,
) -> list[dict]:
    """Fetch every comment on an existing issue (paginated, read-only). Same
    fully-qualified-ref rule as add_label."""
    parsed_repo, number = _parse_issue_ref(issue_ref)
    target_repo = repo or parsed_repo
    tok = token or _read_token()
    client = http or _default_http
    headers = _gh_headers(tok)
    out: list[dict] = []
    page = 1
    while True:
        url = (
            f"{API_BASE}/repos/{target_repo}/issues/{number}/comments"
            f"?per_page=100&page={page}"
        )
        batch = client("GET", url, headers, None)
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


class GitHubChannel(DifficultyChannel):
    """Submits difficulties as GitHub issues. HTTP client and stream are injectable for tests."""

    def __init__(
        self,
        http: Callable[[str, str, dict, bytes | None], object] | None = None,
        token: str | None = None,
        stream: str = "report",
    ) -> None:
        self._http = http or _default_http
        self._token = token  # lazily read on first real call if None
        self._stream = stream

    def _headers(self) -> dict:
        token = self._token or _read_token()
        return {
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def submit(self, record: DifficultyRecord) -> str:
        fields = record_to_fields(record, stream=self._stream)
        body = json.dumps(fields).encode("utf-8")
        url = f"{API_BASE}/repos/{REPO}/issues"
        resp = self._http("POST", url, self._headers(), body)
        return resp.get("html_url", "")

    def pull(self, since: str | None = None) -> list[DifficultyRecord]:
        url = (
            f"{API_BASE}/repos/{REPO}/issues"
            f"?labels={DIFFICULTY_LABEL}&state=open&per_page=100"
        )
        if since:
            url += f"&since={since}"
        issues = self._http("GET", url, self._headers(), None)
        if not isinstance(issues, list):
            return []
        records = [_issue_to_record(i) for i in issues]
        # GitHub `since` filters by updated_at; apply client-side guard on observation ts.
        if since:
            records = [r for r in records if r.ts >= since]
        return records


def _parse_body_field(body: str, field: str) -> str:
    """Extract the value of a '**Field:** value' line from a structured issue body."""
    prefix = f"**{field}:**"
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip().strip("`")
    return ""


def _issue_to_record(issue: dict) -> DifficultyRecord:
    body = issue.get("body") or ""
    labels = [lbl["name"] for lbl in (issue.get("labels") or [])]

    severity = Severity.MEDIUM
    layer = "core"
    for lbl in labels:
        if lbl.startswith("severity:"):
            try:
                severity = Severity.parse(lbl[len("severity:"):])
            except ValueError:
                pass
        elif lbl.startswith("layer:"):
            layer = lbl[len("layer:"):]

    functional_ground = _parse_body_field(body, "Functional ground")
    if not functional_ground:
        title = issue.get("title", "")
        functional_ground = title.split("] ", 1)[-1] if "] " in title else title or "unknown"

    target = _parse_body_field(body, "Target") or issue.get("title", "unknown")
    reporter = (
        _parse_body_field(body, "Reporter")
        or (issue.get("user") or {}).get("login", "unknown")
    )
    ts = _parse_body_field(body, "Observed") or issue.get("created_at", "")

    evidence = ""
    body_lines = body.splitlines()
    for i, line in enumerate(body_lines):
        if line.strip() == "**Evidence:**":
            evidence = "\n".join(body_lines[i + 1:]).strip()
            break

    return DifficultyRecord(
        ts=ts,
        layer=layer,
        target=target,
        functional_ground=functional_ground,
        severity=severity,
        reporter=reporter,
        evidence=evidence,
    )


register_channel("github", GitHubChannel)
