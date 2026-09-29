"""Order-keyed permission-approvals ledger.

Difficulty this removes: `task_accumulator.py` already solves "cross-session
data keyed by something more durable than a session" for effort counters, but
it is keyed by `task_id` — and a `task_id` is reused across a renegotiated or
re-scoped order (K5: the session hook auto-starts sessions with prompt-derived
task ids, so a bare `task_id` is not a safe key for "what has the CUSTOMER
already approved"). This module is the one place that remembers, per
ORDER (keyed by `plan.order_digest(doc)`, not `task_id`), which resources a
customer has already approved — so a later session, or a later plan for the
SAME order, can self-grant a covered request without re-asking (REQ4).

Schema (`schema_version=1`), one JSON file per `order_sha256` under
`config_root.agentctl_order_approvals_dir() / "<order_sha256>.json"` —
honors an `$AGENTCTL_ORDER_APPROVALS_DIR` override (mirroring
`task_accumulator.py`'s own override), which is what lets the test suite
redirect every call site to a per-test tmp dir instead of the real
cross-machine ledger directory::

    {
      "schema_version": 1,
      "order_sha256": "<the order digest this file is keyed by>",
      "records": [
        {
          "plan_sha256": "...",
          "resources": [{"kind": "file", "path": "...", "mode": "write"}, ...],
          "unresolved_identities": [[...], ...],
          "stage_effects": [{"path": "...", "sha256": "..."}, ...],
          "by": "alice",
          "at": "2026-09-29T12:00:00Z"
        }
      ]
    }

Two writers, both customer-authored (never `AGENT_ACTOR`):
  * `cmd_approve` calls `record_approval()` once per successful approve for
    the customer (casefolded match against `[meta.order].customer_id`) —
    never for a non-customer `--by`, never for `--by agent` (refused earlier,
    at submission and at `cmd_approve` itself).
  * a customer-authored `resolve-permission --scope stage` call appends a
    single granted resource via `record_customer_grant()` — a `--scope once`
    grant is deliberately NOT recorded (A2: only a stage-scoped grant counts
    as user-approved for cross-session purposes).

`reset()`/renegotiation and `task-reset` never touch this ledger (it is
untouched by construction — no caller here is wired to either); this module
exposes no delete/clear entry point at all, matching that invariant.

Concurrency: mirrors `task_accumulator.py`'s `_FileLock` (exclusive `fcntl`
lock on a sibling `.lock` file spanning the read-modify-write) and its atomic
`os.replace`-based write.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from lib import config_root

from . import resources as _resources
from .state import AGENT_ACTOR

SCHEMA_VERSION = 1
READABLE_SCHEMA_VERSIONS = (1,)


def _root(root: Path | None) -> Path:
    """Resolved lazily per call, never cached at import time — the same
    discipline `task_accumulator.py`'s `_root()` documents, so a test setting
    `$AGENTCTL_ORDER_APPROVALS_DIR` after import still takes effect."""
    if root is not None:
        return Path(root)
    override = os.environ.get("AGENTCTL_ORDER_APPROVALS_DIR")
    if override:
        return Path(override).expanduser()
    return config_root.agentctl_order_approvals_dir()


def _path(order_sha256: str, root: Path | None = None) -> Path:
    return _root(root) / f"{order_sha256}.json"


def _empty(order_sha256: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "order_sha256": order_sha256, "records": []}


def resource_to_dict(resource: _resources.Resource) -> dict:
    """A JSON-safe dict for any `resources.Resource` subclass — every kind is
    a frozen dataclass whose fields (besides `kind`, already present) are all
    JSON-native strings, so a generic field walk is sufficient; no per-kind
    special-casing is needed and none is added, so a new kind added to
    resources.py serializes here without an order_approvals.py change."""
    import dataclasses

    return dataclasses.asdict(resource)


def resource_from_dict(raw: dict) -> _resources.Resource | None:
    """The inverse of `resource_to_dict`, or `None` for an unrecognized/
    malformed entry — tolerant, matching `task_accumulator._coerce`'s bias:
    a ledger file from a future kind this code does not know about degrades
    to "that one entry is unreadable", not to the whole file being foreign."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    try:
        if kind == "file":
            return _resources.FileResource(path=raw["path"], mode=raw["mode"])
        if kind == "vcs_ref":
            return _resources.VcsRefResource(remote=raw["remote"], ref=raw["ref"], op=raw["op"])
        if kind == "specialist":
            return _resources.SpecialistResource(role=raw["role"])
        if kind == "service":
            return _resources.ServiceResource(name=raw["name"])
        if kind == "dataset":
            return _resources.DatasetResource(name=raw["name"])
    except (KeyError, ValueError, TypeError):
        return None
    return None


def _coerce(raw: str, order_sha256: str) -> dict:
    if not raw.strip():
        return _empty(order_sha256)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _empty(order_sha256)
    if not isinstance(data, dict) or data.get("schema_version") not in READABLE_SCHEMA_VERSIONS:
        return _empty(order_sha256)
    records = data.get("records")
    if not isinstance(records, list):
        records = []
    return {
        "schema_version": SCHEMA_VERSION,
        "order_sha256": data.get("order_sha256", order_sha256),
        "records": records,
    }


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(json.dumps(data, indent=2, sort_keys=True))
            tmp.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class _FileLock:
    """Exclusive lock on `path`'s sibling `.lock` file — identical shape to
    `task_accumulator._FileLock`; duplicated rather than imported since
    `task_accumulator.py` is documented as a pattern to copy, not to extend
    (the stage brief names this explicitly)."""

    def __init__(self, path: Path):
        self._lock_path = path.with_suffix(path.suffix + ".lock")
        self._fh = None

    def __enter__(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._lock_path, "a+")
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        finally:
            self._fh.close()
        return False


def get(order_sha256: str, *, root: Path | None = None) -> dict:
    """This order's ledger, or a zeroed shape if no file exists yet — "no
    file" and "no approval yet" are the same state, so a caller (including a
    reused `task_id` now carrying a different order, K5) never special-cases
    the first read."""
    path = _path(order_sha256, root)
    if not path.exists():
        return _empty(order_sha256)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return _empty(order_sha256)
    return _coerce(raw, order_sha256)


def record_approval(
    order_sha256: str,
    *,
    plan_sha256: str,
    resources: list[_resources.Resource],
    unresolved_identities: list[tuple],
    stage_effects: list[dict],
    by: str,
    at: str,
    root: Path | None = None,
) -> dict:
    """Append one approval record, stamped by `cmd_approve` for the customer
    only. `by` must never be `AGENT_ACTOR` — enforced by the caller (an
    `approve --by agent` is refused before this is ever reached), asserted
    here too as a last-resort guard against a future caller forgetting it."""
    if by.casefold() == AGENT_ACTOR:
        raise ValueError(f"order_approvals.record_approval refuses by={AGENT_ACTOR!r}")
    record = {
        "plan_sha256": plan_sha256,
        "resources": [resource_to_dict(r) for r in resources],
        "unresolved_identities": [list(identity) for identity in unresolved_identities],
        "stage_effects": stage_effects,
        "by": by,
        "at": at,
    }
    path = _path(order_sha256, root)
    with _FileLock(path):
        data = _coerce(path.read_text(encoding="utf-8"), order_sha256) if path.exists() else _empty(order_sha256)
        data["records"].append(record)
        _write_atomic(path, data)
        return data


def record_customer_grant(
    order_sha256: str,
    *,
    resource: _resources.Resource,
    by: str,
    at: str,
    root: Path | None = None,
) -> dict:
    """Append a single customer-authored `resolve-permission --scope stage`
    grant (A2: `--scope once` never calls this). Recorded as a one-resource
    record so `approved_resources()` treats it identically to an approval
    record's own resource list."""
    if by.casefold() == AGENT_ACTOR:
        raise ValueError(f"order_approvals.record_customer_grant refuses by={AGENT_ACTOR!r}")
    record = {
        "plan_sha256": None,
        "resources": [resource_to_dict(resource)],
        "unresolved_identities": [],
        "stage_effects": [],
        "by": by,
        "at": at,
    }
    path = _path(order_sha256, root)
    with _FileLock(path):
        data = _coerce(path.read_text(encoding="utf-8"), order_sha256) if path.exists() else _empty(order_sha256)
        data["records"].append(record)
        _write_atomic(path, data)
        return data


def approved_resources(order_sha256: str, *, root: Path | None = None) -> list[_resources.Resource]:
    """Every resource ever approved for this order, across every record —
    the set `resolve-permission --by agent` and `dispatch`'s self_grant check
    a requested resource's coverage against (REQ5). Unrecognized/malformed
    entries are silently dropped (tolerant read, matching `resource_from_dict`)."""
    data = get(order_sha256, root=root)
    out: list[_resources.Resource] = []
    for record in data["records"]:
        for raw in record.get("resources") or []:
            resource = resource_from_dict(raw)
            if resource is not None:
                out.append(resource)
    return out
