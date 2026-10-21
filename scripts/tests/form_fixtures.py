"""Bodies and blinds for the published-text form pickers (writer_rules.find_form_candidates).

Shared by test_writer_rules.py and form_picker_control.py, so the fixture the unit tests pin
is the fixture the negative control blinds. No test functions live here.

Every body is lexically silent: ``find_candidates`` returns [] on it, so a judge call or a
nomination attributable to these bodies is attributable to a form picker alone.
"""
from __future__ import annotations

from lib import writer_rules as wr

FORM_MIN = wr.FORM_MIN_BODY_CHARS + 500
ESTIMATE_ANSWER_HEADING = "## 6. Capacity estimate"
ESTIMATE_ANSWER_MIN_POSITION = 0.6

_SUBJECTS = (
    "The migration team", "The review group", "The release owner", "The storage service",
    "The scheduler", "The ingest pipeline", "The maintenance window", "The rollout plan",
    "The monitoring stack", "The platform group", "The audit trail", "The capacity model",
)
_PREDICATES = (
    "keeps its configuration next to the code so that a change is reviewed with the code it affects",
    "reports its progress in plain sentences, and each report names what changed since the last one",
    "was moved to the new layout last quarter, which removed a long chain of manual hand-offs",
    "reads its inputs from one place and writes its outputs to another, so a failed run is easy to repeat",
    "relies on a small set of checks that run before every change reaches the shared environment",
    "was described in the design note, and the note explains why the simpler alternative was set aside",
    "records every decision with a reason, which lets a new member of the group follow the history",
    "still depends on a manual step, and the group agreed to replace that step before the next release",
)


def _prose_line(i: int) -> str:
    return f"{_SUBJECTS[i % len(_SUBJECTS)]} {_PREDICATES[(i * 5 + i // len(_SUBJECTS)) % len(_PREDICATES)]}."


def prose_lines(start: int, count: int) -> list[str]:
    return [_prose_line(start + k) for k in range(count)]


def _paragraph(start: int, count: int) -> str:
    return " ".join(prose_lines(start, count))


def _join_long(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def long_prose_line(i: int) -> str:
    return _paragraph(i * 12, 12)


def assemble(tail: list[str]) -> str:
    """A body whose lead is the shortest run of prose lines that reaches the tail offset.

    The offset is FORM_TAIL_START * (lead + tail), so the lead has to hold
    FORM_TAIL_START / (1 - FORM_TAIL_START) of the tail; stopping at the first lead that does
    leaves every lead line starting before the offset and every tail line starting at or after
    it. The result must clear FORM_MIN on its own: padding the lead further would pull tail
    lines into it, so the caller makes the tail long enough instead.
    """
    tail_chars = len(_join_long(tail))
    lead: list[str] = []
    k = 0
    while len(_join_long(lead)) * (1 - wr.FORM_TAIL_START) < tail_chars * wr.FORM_TAIL_START:
        lead.append(_paragraph(k * 3, 3))
        k += 1
    body = _join_long(lead + tail)
    assert len(body) >= FORM_MIN, len(body)
    return body


def _telegraphic_tail() -> list[str]:
    rows = [f"| stage {n} | {10 + 7 * n} | {_PREDICATES[n % len(_PREDICATES)][:60]} |" for n in range(20)]
    table = ["| Stage | Value | Note |", "|---|---|---|"] + rows
    items = [f"- {_SUBJECTS[n % len(_SUBJECTS)]} {_PREDICATES[(n + 3) % len(_PREDICATES)][:70]}" for n in range(20)]
    prose = [_paragraph(100 + 2 * n, 2) for n in range(20)]
    # each kind alone stays at or below FORM_TAIL_SHARE; together they exceed it
    return items[:10] + prose[:10] + table + items[10:] + prose[10:]


def _build_telegraphic() -> str:
    return assemble(_telegraphic_tail())


def _build_estimate() -> str:
    # eight sections; the asked-for figures sit in section 6, past 60 percent of the characters
    lead = [
        "# Capacity report",
        "",
        "## 1. Where the migration stands",
        "",
        _paragraph(0, 3) + " So far **120 GB processed so far** and **3 hours of copying behind us** are the intermediate figures.",
        "",
    ]
    middle: list[str] = []
    for n, size in ((2, 6), (3, 6), (4, 6), (5, 6)):
        middle += [f"## {n}. Background {n}", "", _paragraph(n * 10, size), ""]
    answer = [
        ESTIMATE_ANSWER_HEADING,
        "",
        "The copy needs between 40 GB and 55 GB of free memory on each of 8 cores.",
        "The first pass takes about 6 hours, and the second pass takes between 2 hours and 3 hours.",
        "Storage grows by 12% per day, which is 30 GB per day at the current rate of 250 rows per second.",
        "A run of 14 days costs between 900 $ and 1200 $ at a price of 3 $ per hour of compute.",
        "The final table holds about 4 TB, or 90 000 000 rows, split across 16 shards.",
        "",
    ]
    tail = [
        "## 7. Risks",
        "",
        _paragraph(70, 3),
        "",
        "## 8. Next steps",
        "",
        _paragraph(80, 3),
        "",
    ]
    head = "\n".join(lead + middle) + "\n"
    # grow the middle sections until the answer heading starts past 60 percent of the characters
    pad = 0
    while True:
        text = head + "\n".join(answer + tail) + "\n"
        if text.index(ESTIMATE_ANSWER_HEADING) / len(text) > ESTIMATE_ANSWER_MIN_POSITION and len(text) >= FORM_MIN:
            return text
        pad += 1
        head += _paragraph(200 + pad * 3, 3) + "\n\n"


def _build_prose() -> str:
    lines: list[str] = []
    k = 0
    while len("\n".join(lines)) < FORM_MIN:
        lines.append(_paragraph(k * 3, 3))
        lines.append("")
        k += 1
    return "\n".join(lines) + "\n"


TELEGRAPHIC_BODY = _build_telegraphic()
ESTIMATE_BODY = _build_estimate()
PROSE_BODY = _build_prose()


def _always_true(*_args, **_kwargs) -> bool:
    return True


def _never(*_args, **_kwargs) -> bool:
    return False


# blind name -> (writer_rules attribute, replacement, target pickers). Each target is the
# picker that must go silent on its fixture once the attribute is rebound.
PICKER_FIXTURE = {
    "_tail_candidates": "TELEGRAPHIC_BODY",
    "_header_candidates": "TELEGRAPHIC_BODY",
    "_estimate_candidates": "ESTIMATE_BODY",
}
FORM_BLINDS: dict[str, tuple[str, object, tuple[str, ...]]] = {
    "length": ("FORM_MIN_BODY_CHARS", 10**9, ("_tail_candidates", "_header_candidates", "_estimate_candidates")),
    "tail-start": ("FORM_TAIL_START", 1.01, ("_tail_candidates",)),
    "estimate-tail-start": ("FORM_TAIL_START", 1.01, ("_estimate_candidates",)),
    "share": ("FORM_TAIL_SHARE", 1.01, ("_tail_candidates",)),
    "table-row": ("_is_table_row", _never, ("_tail_candidates", "_header_candidates")),
    "list-item": ("_is_list_item", _never, ("_tail_candidates",)),
    "header-marker": ("_header_has_marker", _always_true, ("_header_candidates",)),
    "numeric-cell": ("_is_numeric_cell", _never, ("_header_candidates",)),
    "estimate-count": ("ESTIMATE_MIN_UNIT_NUMBERS", 10**9, ("_estimate_candidates",)),
    "estimate-lead": ("_lead_has_table_row", _always_true, ("_estimate_candidates",)),
}
