#!/usr/bin/env python3
"""Derive a filesystem/branch-safe slug from an arbitrary task title.

Difficulty removed: a title written in a non-Latin script or made of pure
symbols collapses, under a naive ``[a-z0-9]`` strip, to an EMPTY slug — which
then assembles a trailing-dash task name (``KEY-``) or makes a ``--name`` path
abort with "could not derive a task name". This module transliterates common
non-Latin input (Cyrillic) and folds accented Latin via Unicode normalization
so real titles survive, while still returning a clean empty string when nothing
survives (the caller decides the empty-slug fallback). Org-neutral: generic
Unicode handling, no project- or tracker-specific knowledge.

CLI: ``slugify.py <title>`` or piped on stdin. Prints the slug (possibly empty)
and exits 0.
"""
import sys
import unicodedata

# Explicit Cyrillic (ru) -> Latin map, applied per-character before
# ascii-encode-ignore so Cyrillic letters transliterate (Привет -> privet)
# instead of being dropped. Keys are single Cyrillic chars; a value may be
# multi-char (ж -> zh) or empty (ь -> "").
_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _transliterate_cyrillic(s: str) -> str:
    return "".join(_CYRILLIC.get(ch, ch) for ch in s)


def slugify(s: str) -> str:
    # Lowercase up front so the Cyrillic map (lowercase keys) covers both cases.
    s = _transliterate_cyrillic(s.lower())
    # NFKD folds accented Latin (é -> e + combining mark); ascii-ignore then
    # drops the marks and any remaining non-ascii.
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    out = []
    prev_dash = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")[:40].rstrip("-")
    return slug


def main() -> int:
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    print(slugify(raw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
