"""The hook's decision log, as the hook writes it and the calibration reads it.

Difficulty removed: scripts/hook-resource-grant-check.py (writer) and agentctl/resource_estimation.py
(reader) both need the log's directory, its per-(parent session, stage) file name and the test for
"this resource is a change", and a copy in each drifts (the calibration once counted a read-class
service call as a change). They import this module instead.
"""
from __future__ import annotations

from pathlib import Path

from . import resources
from .store import safe_session_id

USES_DIRNAME = "resource-uses"


def uses_dir(state_root: "Path | str") -> Path:
    return Path(state_root) / USES_DIRNAME


def uses_log_path(state_root: "Path | str", parent_session: str, stage_index: int) -> Path:
    return uses_dir(state_root) / f"{safe_session_id(parent_session)}-{stage_index}.jsonl"


def is_change(resource) -> bool:
    """A resource a call changes: a write-mode file, a non-read service op, any ref or other kind."""
    if isinstance(resource, resources.FileResource):
        return resource.mode == "write"
    if isinstance(resource, resources.ServiceResource):
        return resource.op_class != "read"
    return True
