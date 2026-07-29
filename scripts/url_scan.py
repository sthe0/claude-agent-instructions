#!/usr/bin/env python3
"""Shared "find the URLs in a blob of text" scan for the hooks that reason about
links a session saw.

Difficulty removed: two hooks scan free text for platform URLs — the run-URL
surfaced reminder and the review-mergeable guardian — and each grew its own copy
of the same `https?://…` regex. The copies then drifted. The review scan learned
to trim trailing sentence punctuation (tool output routinely reads `review
created: <url>.` or `<url>: green`, and the trailing character defeats the
path/id match that follows); the run-URL scan did not, so a run URL that ended a
sentence was dropped. Both hooks fail SILENTLY on a miss, which is the expensive
direction — nobody notices a nudge that never came. One home for the regex and
the trim means a fix reaches every consumer.

Hook filenames are hyphenated and cannot be imported, which is why this lives in
an importable sibling module, mirroring `transcript_read.py` (the transcript
predicate) and `long_job_detect.py` (the launch predicate).

Total on bad input: a non-string yields nothing rather than raising, because
every consumer is a fail-open advisory that must never crash a turn.
"""
from __future__ import annotations

import re
from typing import Iterator

# Any http(s) URL, minus the markdown / quote delimiters that commonly wrap one.
URL_RE = re.compile(r"https?://[^\s)\]}>\"'`]+", re.IGNORECASE)

# Sentence punctuation the URL regex cannot exclude (a URL may legally carry
# these mid-path), trimmed from the END of a match.
TRAILING_PUNCT = ".,;:!?"


def iter_urls(text) -> Iterator[str]:
    """Yield every http(s) URL in `text`, trailing sentence punctuation trimmed."""
    if not isinstance(text, str) or not text:
        return
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(TRAILING_PUNCT)
        if url:
            yield url
