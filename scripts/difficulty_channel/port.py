"""The DifficultyChannel port and its channel-agnostic record schema.

The record schema is the single join contract across every channel: a difficulty submitted
through any adapter carries the same fields, and ``functional_ground`` is the cross-channel
cluster key the digest groups on. The port itself has zero adapter-specific knowledge, so a
new audience is added as an adapter under ``adapters/`` without ever touching this file.
"""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Callable


class Severity(enum.Enum):
    """Difficulty severity. ``mass`` lets the digest weight a cluster by Σseverity."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def mass(self) -> int:
        return {"low": 1, "medium": 2, "high": 4, "critical": 8}[self.value]

    @classmethod
    def parse(cls, value: "Severity | str") -> "Severity":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            allowed = ", ".join(s.value for s in cls)
            raise ValueError(f"invalid severity {value!r}; expected one of: {allowed}") from exc


@dataclass(frozen=True)
class DifficultyRecord:
    """One reported difficulty, channel-agnostic.

    The exact ADR field set — adapters map this onto a tracker's native fields and the digest
    clusters on ``functional_ground`` (the desired-vs-actual divergence the difficulty names).
    """

    ts: str            # ISO-8601 timestamp of when the difficulty was observed
    layer: str         # which precedence layer the difficulty is against (core|team|personal)
    target: str        # the file/rule/path the difficulty is about
    functional_ground: str  # the desired-vs-actual divergence — the cross-channel cluster key
    severity: Severity
    reporter: str      # who/what submitted it
    evidence: str = ""  # free-text: quote, log line, link

    def __post_init__(self) -> None:
        # Validate/normalise the severity enum even if a raw string slipped in.
        object.__setattr__(self, "severity", Severity.parse(self.severity))
        if not self.functional_ground.strip():
            raise ValueError("functional_ground is the cluster key and must be non-empty")


class DifficultyChannel(abc.ABC):
    """Transport-agnostic submit/pull port. Adapters subclass this; the port knows nothing
    about any concrete tracker."""

    @abc.abstractmethod
    def submit(self, record: DifficultyRecord) -> str:
        """Submit a difficulty; return a channel-native id/handle for it."""

    @abc.abstractmethod
    def pull(self, since: str | None = None) -> list[DifficultyRecord]:
        """Return records submitted at/after ``since`` (ISO-8601); all records if None."""


class NullChannel(DifficultyChannel):
    """In-memory test double / no-op sink. Round-trips records without external I/O."""

    def __init__(self) -> None:
        self._store: list[DifficultyRecord] = []

    def submit(self, record: DifficultyRecord) -> str:
        self._store.append(record)
        return f"mem-{len(self._store) - 1}"

    def pull(self, since: str | None = None) -> list[DifficultyRecord]:
        if since is None:
            return list(self._store)
        return [r for r in self._store if r.ts >= since]


# Config-routed registry: channel name -> factory. Adapters register themselves (or are
# registered by the config loader) so the port never imports an adapter.
_REGISTRY: dict[str, Callable[[], DifficultyChannel]] = {}


def register_channel(name: str, factory: Callable[[], DifficultyChannel]) -> None:
    _REGISTRY[name] = factory


def get_channel(name: str) -> DifficultyChannel:
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise KeyError(f"unknown difficulty channel {name!r}; registered: {known}")
    return _REGISTRY[name]()


# The in-memory double is always available as a channel for tests and dry-runs.
register_channel("null", NullChannel)
