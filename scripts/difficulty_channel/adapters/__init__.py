"""Concrete DifficultyChannel adapters (ADR-0001 adapter table).

Each adapter maps the one common DifficultyRecord onto a tracker's native fields, so a
non-author submits to a surface they already have write access to — never the protected Core.
Importing this package registers the adapters with the port registry.
"""

from .startrek import StartrekChannel
from .external import ExternalChannel

__all__ = ["StartrekChannel", "ExternalChannel"]
