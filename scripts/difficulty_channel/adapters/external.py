"""External issue-tracker adapter stub (GitHub / Linear / Jira shape).

ADR-0001 adapter table lists an External audience whose concrete transport is out of scope for
this slice. The stub advertises the *same* DifficultyRecord contract and documents the intended
field mapping, but submit/pull raise NotImplementedError — a real transport is a later adapter,
added without touching the port.
"""
from __future__ import annotations

from ..port import DifficultyChannel, DifficultyRecord, Severity, register_channel

# Documented mapping a concrete external adapter must implement (record field -> issue field):
RECORD_FIELD_MAPPING = {
    "functional_ground": "title + label",
    "severity": "label (severity:<level>)",
    "target": "body (affected path)",
    "layer": "label (layer:<name>)",
    "evidence": "body",
    "reporter": "reporter / author",
    "ts": "created_at",
}

# severity -> external label, kept here so the future adapter inherits one source of truth.
SEVERITY_LABEL = {
    Severity.LOW: "severity:low",
    Severity.MEDIUM: "severity:medium",
    Severity.HIGH: "severity:high",
    Severity.CRITICAL: "severity:critical",
}


class ExternalChannel(DifficultyChannel):
    """Stub for an external tracker (GitHub Issues / Linear / Jira). Same record contract."""

    record_contract = DifficultyRecord  # advertises the shared contract

    def submit(self, record: DifficultyRecord) -> str:
        raise NotImplementedError(
            "ExternalChannel is a stub; implement submit() against the target tracker's API "
            f"using RECORD_FIELD_MAPPING ({sorted(RECORD_FIELD_MAPPING)})."
        )

    def pull(self, since: str | None = None) -> list[DifficultyRecord]:
        raise NotImplementedError("ExternalChannel.pull() is not implemented in this stub.")


register_channel("external", ExternalChannel)
