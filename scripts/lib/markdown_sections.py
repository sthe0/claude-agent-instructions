"""Locating a `## <heading>` section of a markdown leaf.

One section finder for `record-experience.py` and `understanding_effort`, so the two cannot disagree on where a section ends.
"""
from __future__ import annotations

import re

_H2 = re.compile(r"^##\s", re.MULTILINE)


def section_span(text: str, heading: str, *, exact: bool = False) -> tuple[int, int] | None:
    """Return (start, end) of a `## <heading>` section, end being the start of the next `## ` heading
    or the end of the text. None if the heading is absent. A heading matches as a whole word and may
    carry a suffix (`## Cost (draft)`); `exact` takes only the heading alone on its line."""
    tail = r"[ \t]*$" if exact else r"\b.*$"
    found = re.search(rf"^##\s+{re.escape(heading)}{tail}", text, re.MULTILINE)
    if not found:
        return None
    following = _H2.search(text, found.end())
    return found.start(), (following.start() if following else len(text))
