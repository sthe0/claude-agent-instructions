"""The effort of understanding an order, kept on a memory leaf that records its type.

A leaf records the estimate the order's type gets for understanding and the actuals of the orders of that
type that were met. An estimate or an actual holds minutes, dollars or both (one scale is enough); an
estimate names its author and, once someone agreed to it, its confirmer — without a confirmer it stands
as an estimate nobody has confirmed. The block is a fenced TOML table under `## Understanding effort`; a
leaf without it is a leaf that records no such effort and stays valid.

Callers reach every seam as `understanding_effort.<name>` so a control can replace one at its module attribute.
"""
from __future__ import annotations

import json
import math
import re
import tomllib

from . import markdown_sections

SECTION = "## Understanding effort"
_FENCE = re.compile(r"```toml\n(.*?)\n```", re.DOTALL)
_NUMBERS = ("minutes", "usd")
_ESTIMATE_KEYS = (*_NUMBERS, "author", "confirmer")
_ACTUAL_KEYS = (*_NUMBERS, "session", "date")
_ESTIMATE_REQUIRED = ("author",)
_ACTUAL_REQUIRED = ("session", "date")


def section_span(text: str) -> tuple[int, int] | None:
    return markdown_sections.section_span(text, SECTION.removeprefix("## "))


def parse(text: str) -> dict | None:
    span = section_span(text)
    if span is None:
        return None
    fence = _FENCE.search(text[span[0]:span[1]])
    if fence is None:
        raise ValueError(f"{SECTION} holds no fenced toml block")
    try:
        return tomllib.loads(fence.group(1))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{SECTION} block is not valid toml: {exc}") from exc


def _row_problems(row, keys: tuple[str, ...], required: tuple[str, ...], where: str) -> list[str]:
    if not isinstance(row, dict):
        return [f"{where} is not a table"]
    out = [f"{where} lacks {k}" for k in required if k not in row]
    if not any(k in row for k in _NUMBERS):
        out.append(f"{where} holds neither minutes nor usd")
    for key in _NUMBERS:
        if key not in row:
            continue
        value = row[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            out.append(f"{where} {key} is not a finite non-negative number")
    for key in keys:
        if key not in _NUMBERS and key in row and not (isinstance(row[key], str) and row[key]):
            out.append(f"{where} {key} is not a non-empty string")
    return out


def problems(text: str) -> list[str]:
    try:
        block = parse(text)
    except ValueError as exc:
        return [str(exc)]
    if block is None:
        return []
    out = []
    if "estimate" in block:
        out += _row_problems(block["estimate"], _ESTIMATE_KEYS, _ESTIMATE_REQUIRED, "estimate")
    actuals = block.get("actuals", [])
    if not isinstance(actuals, list):
        return [*out, "actuals is not an array of tables"]
    for i, row in enumerate(actuals):
        out += _row_problems(row, _ACTUAL_KEYS, _ACTUAL_REQUIRED, f"actuals[{i}]")
    if not block:
        out.append(f"{SECTION} block holds neither an estimate nor an actual")
    return out


def _scalar(value) -> str:
    return json.dumps(value) if isinstance(value, str) else repr(float(value))


def _lines(row: dict, keys: tuple[str, ...]) -> list[str]:
    return [f"{k} = {_scalar(row[k])}" for k in keys if k in row]


def render(block: dict) -> str:
    lines = [SECTION, "", "```toml"]
    if "estimate" in block:
        lines.append("[estimate]")
        lines += _lines(block["estimate"], _ESTIMATE_KEYS)
    for row in block.get("actuals", []):
        if lines[-1] != "```toml":
            lines.append("")
        lines.append("[[actuals]]")
        lines += _lines(row, _ACTUAL_KEYS)
    lines += ["```", ""]
    return "\n".join(lines)


def _replace(text: str, block: dict) -> str:
    span = section_span(text)
    rendered = render(block)
    if span is None:
        return text.rstrip("\n") + "\n\n" + rendered
    return text[:span[0]] + rendered + ("\n" + text[span[1]:] if text[span[1]:] else "")


def _row(**fields) -> dict:
    return {k: v for k, v in fields.items() if v is not None}


def set_estimate(text: str, *, author: str, minutes: float | None = None, usd: float | None = None,
                 confirmer: str | None = None) -> str:
    block = parse(text) or {}
    block["estimate"] = _row(minutes=minutes, usd=usd, author=author, confirmer=confirmer)
    return _replace(text, block)


def add_actual(text: str, *, session: str, date: str, minutes: float | None = None,
               usd: float | None = None) -> str:
    block = parse(text) or {}
    block.setdefault("actuals", []).append(_row(minutes=minutes, usd=usd, session=session, date=date))
    return _replace(text, block)


def estimate_of(text: str) -> dict | None:
    block = parse(text)
    return dict(block["estimate"]) if block and "estimate" in block else None
