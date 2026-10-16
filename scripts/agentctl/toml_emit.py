"""A deterministic TOML writer for the plan converter (ADR-0011).

The stdlib reads TOML but cannot write it. `dumps` writes the dicts `tomllib` returns: plain
keys first, then sub-tables (`[a.b]`) and arrays of tables (`[[a]]`), keys in dict order,
no randomness, so one dict always gives one text. A hash-pinned reference (a table of
exactly `ref` and `sha256`) is written inline, where the plan's own sealer expects it.

The writer does not vouch for itself: the caller loads the text back and compares.
"""
from __future__ import annotations

import datetime
import math
import re

from .plan import PlanError

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ARRAY_WIDTH = 100
_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _is_ref(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {"ref", "sha256"}


def _key(name: str) -> str:
    return name if _BARE_KEY.fullmatch(name) else _string(name)


def _escape(text: str, *, multiline: bool = False) -> str:
    out = []
    for ch in text:
        if multiline and ch in "\n\"":
            out.append(ch)
        elif ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def _string(text: str) -> str:
    return f'"{_escape(text)}"'


def _multiline(text: str) -> str:
    stripped = text.rstrip('"')
    trailing = '\\"' * (len(text) - len(stripped))
    body = _escape(stripped, multiline=True)
    body = re.sub(r'"{3,}', lambda m: "".join(
        '\\"' if i % 3 == 2 else '"' for i in range(len(m.group()))), body)
    return f'"""\n{body}{trailing}"""'


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    raise PlanError(f"cannot write a {type(value).__name__} value as TOML")


def _inline(value: object) -> str:
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{_key(k)} = {_inline(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_inline(v) for v in value) + "]"
    return _scalar(value)


def _value(value: object) -> str:
    if isinstance(value, str) and "\n" in value:
        return _multiline(value)
    if isinstance(value, list) and value:
        flat = _inline(value)
        if len(flat) > _ARRAY_WIDTH:
            return "[\n" + "".join(f"  {_inline(v)},\n" for v in value) + "]"
        return flat
    return _inline(value)


def assignment(key: str, value: object) -> str:
    """`key = value` as `dumps` writes a plain key, for a caller that rewrites one key of an
    existing file in place."""
    return f"{_key(key)} = {_value(value)}"


def _is_table(value: object) -> bool:
    return isinstance(value, dict) and not _is_ref(value)


def _is_table_array(value: object) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(_is_table(v) for v in value))


def _emit(table: dict, path: list[str], out: list[str]) -> None:
    for key, value in table.items():
        if not _is_table(value) and not _is_table_array(value):
            out.append(assignment(key, value))
    for key, value in table.items():
        here = [*path, key]
        header = ".".join(_key(part) for part in here)
        if _is_table(value):
            out.extend(["", f"[{header}]"])
            _emit(value, here, out)
        elif _is_table_array(value):
            for item in value:
                out.extend(["", f"[[{header}]]"])
                _emit(item, here, out)


def dumps(data: dict) -> str:
    """The TOML text of `data`, a dict as `tomllib` returns it."""
    out: list[str] = []
    _emit(data, [], out)
    text = "\n".join(out).lstrip("\n")
    return text + "\n" if text else ""
