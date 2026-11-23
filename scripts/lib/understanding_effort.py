"""The effort of understanding an order, kept on a memory leaf that records its type.

A leaf records the estimate the order's type gets for understanding (minutes and dollars, who
authored it and who confirmed it) and the actuals of the orders of that type that were met. The block
is a fenced TOML table under `## Understanding effort`; a leaf without it is a leaf that records no
such effort and stays valid.

Callers reach every seam as `understanding_effort.<name>` so a control can replace one at its module attribute.
"""
from __future__ import annotations

import json
import re
import tomllib

SECTION = "## Understanding effort"
_FENCE = re.compile(r"```toml\n(.*?)\n```", re.DOTALL)
_ESTIMATE_KEYS = ("minutes", "usd", "author", "confirmer")
_ACTUAL_KEYS = ("minutes", "usd", "session", "date")
_NUMBERS = ("minutes", "usd")


def section_span(text: str) -> tuple[int, int] | None:
    start = re.search(rf"^{re.escape(SECTION)}[ \t]*$", text, re.MULTILINE)
    if not start:
        return None
    nxt = re.search(r"^## ", text[start.end():], re.MULTILINE)
    return start.start(), start.end() + nxt.start() if nxt else len(text)


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


def _row_problems(row, keys: tuple[str, ...], where: str) -> list[str]:
    if not isinstance(row, dict):
        return [f"{where} is not a table"]
    out = [f"{where} lacks {k}" for k in keys if k not in row]
    for key in _NUMBERS:
        value = row.get(key)
        if key in row and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
            out.append(f"{where} {key} is not a non-negative number")
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
        out += _row_problems(block["estimate"], _ESTIMATE_KEYS, "estimate")
    actuals = block.get("actuals", [])
    if not isinstance(actuals, list):
        return [*out, "actuals is not an array of tables"]
    for i, row in enumerate(actuals):
        out += _row_problems(row, _ACTUAL_KEYS, f"actuals[{i}]")
    if not block:
        out.append(f"{SECTION} block holds neither an estimate nor an actual")
    return out


def _scalar(value) -> str:
    return json.dumps(value) if isinstance(value, str) else repr(float(value))


def render(block: dict) -> str:
    lines = [SECTION, "", "```toml"]
    if "estimate" in block:
        lines.append("[estimate]")
        lines += [f"{k} = {_scalar(block['estimate'][k])}" for k in _ESTIMATE_KEYS]
    for row in block.get("actuals", []):
        if lines[-1] != "```toml":
            lines.append("")
        lines.append("[[actuals]]")
        lines += [f"{k} = {_scalar(row[k])}" for k in _ACTUAL_KEYS]
    lines += ["```", ""]
    return "\n".join(lines)


def _replace(text: str, block: dict) -> str:
    span = section_span(text)
    rendered = render(block)
    if span is None:
        return text.rstrip("\n") + "\n\n" + rendered
    return text[:span[0]] + rendered + ("\n" + text[span[1]:] if text[span[1]:] else "")


def set_estimate(text: str, minutes: float, usd: float, author: str, confirmer: str) -> str:
    block = parse(text) or {}
    block["estimate"] = {"minutes": minutes, "usd": usd, "author": author, "confirmer": confirmer}
    return _replace(text, block)


def add_actual(text: str, minutes: float, usd: float, session: str, date: str) -> str:
    block = parse(text) or {}
    block.setdefault("actuals", []).append(
        {"minutes": minutes, "usd": usd, "session": session, "date": date})
    return _replace(text, block)


def estimate_of(text: str) -> dict | None:
    block = parse(text)
    return dict(block["estimate"]) if block and "estimate" in block else None
