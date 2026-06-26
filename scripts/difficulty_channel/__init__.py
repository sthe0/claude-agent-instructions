"""Difficulty-accumulation channel (ADR-0001 § Difficulty-accumulation mechanism).

A pluggable port that decouples *submitting* a difficulty from *pushing* a change to the
protected Core. Contributors who cannot edit Core (the common case — `is_author = false`)
submit a channel-agnostic ``DifficultyRecord`` through a ``DifficultyChannel`` adapter onto a
surface they already have write access to (a tracker queue, an external issue tracker). The
core-side digest later clusters the accumulated records by their ``functional_ground``.

This package is the transport-agnostic port + registry; concrete adapters live under
``difficulty_channel.adapters``.
"""

from .port import (
    DifficultyChannel,
    DifficultyRecord,
    NullChannel,
    Severity,
    get_channel,
    register_channel,
)

__all__ = [
    "DifficultyChannel",
    "DifficultyRecord",
    "NullChannel",
    "Severity",
    "get_channel",
    "register_channel",
]
