"""Locating a `## <heading>` section of a markdown leaf.

One section finder for `record-experience.py` and `understanding_effort`, so the two cannot disagree on where a section ends.
"""
from __future__ import annotations

import re

_H2 = re.compile(r"^##\s", re.MULTILINE)


def section_span(text: str, heading: str) -> tuple[int, int] | None:
    """Return (start, end) of a `## <heading>` section, end being the start of the next `## ` heading
    or the end of the text. None if the heading is absent."""
    found = re.search(rf"^##\s+{re.escape(heading)}\b.*$", text, re.MULTILINE)
    if not found:
        return None
    following = _H2.search(text, found.end())
    return found.start(), (following.start() if following else len(text))
