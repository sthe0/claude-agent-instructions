"""Tech-writer rules as the published-text gate sees them.

The wording of every rule lives only in ``skills/specializations/tech-writer/SKILL.md``.
``publish-rules.toml`` beside it classifies each numbered rule as judge-checkable
(with candidate literals/patterns) or perception-only (with a reason). This module
parses SKILL.md at runtime, so an edit to a rule reaches the judge prompt without a
second copy; ``scripts/tests/test_writer_rules.py`` fails on any drift between the
two files.

``find_candidates`` is candidate generation only: it picks which bodies are worth a
model judge's time and never decides that a rule was violated.

``find_form_candidates`` does the same for rules whose defect is a property of the
shape of a long body rather than of its words (status ``judge-form``). The code
decides only countable facts -- body length, the share of table-row and list-item
lines in the tail, whether a numeric table column has a header that names a unit,
how many unit-bearing numbers a body holds and whether the lead holds a table row.
Whether the text is in fact telegraphic, unit-less or a buried estimate is the
judge's verdict, never the picker's.
"""
from __future__ import annotations

import functools
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WRITER_DIR = _REPO_ROOT / "skills" / "specializations" / "tech-writer"
SKILL_PATH = _WRITER_DIR / "SKILL.md"
REGISTRY_PATH = _WRITER_DIR / "publish-rules.toml"

_SECTIONS = {"## What to say": "say", "## How you write": "how"}
_CALQUE_HEADING = "## Calque and jargon table"
_ITEM_RE = re.compile(r"^(\d+)\. \*\*(.+?)\*\*")
_MAX_HIT_LINES = 5
_MAX_HIT_CHARS = 200

STATUSES = ("judge", "perception", "judge-form")

# Read at call time: the mutation catalogue and the control script rebind them.
FORM_MIN_BODY_CHARS = 6000
FORM_TAIL_START = 0.25
FORM_TAIL_SHARE = 0.5
ESTIMATE_MIN_UNIT_NUMBERS = 10

# General Russian and English count, money, time and size words. They only nominate:
# a missing word costs at most one spurious judge call.
UNIT_WORDS = (
    "шт", "штук", "руб", "рублей", "₽", "$", "€",
    "ч", "час", "часов", "дн", "дней", "день", "мс", "сек", "секунд", "мин", "минут",
    "байт", "КБ", "МБ", "ГБ", "ТБ", "ядер", "ядра", "строк",
    "ms", "s", "sec", "min", "h", "hours", "days",
    "KB", "MB", "GB", "TB", "cores", "rows", "items", "requests",
)

_NUMBER = r"[<>≤≥~≈+\-−]?\d+(?:[ .,]\d+)*"
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_NUMERIC_CELL_RE = re.compile(r"\s*" + _NUMBER + r"\s*%?\s*")


@dataclass(frozen=True)
class SkillRule:
    id: str
    section: str
    number: int
    fingerprint: str
    text: str


@dataclass(frozen=True)
class Rule:
    id: str
    section: str
    number: int
    fingerprint: str
    status: str
    literals: tuple[str, ...] = ()
    patterns: tuple[tuple[str, str], ...] = ()
    reason: str = ""


def parse_skill(skill_path: Path | None = None) -> list[SkillRule]:
    lines = Path(skill_path or SKILL_PATH).read_text(encoding="utf-8").splitlines()
    rules: list[SkillRule] = []
    section: str | None = None
    current: tuple[str, int, str, list[str]] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        sec, num, lead, body = current
        text = "\n".join(body).rstrip()
        rules.append(SkillRule(f"{sec}-{num}", sec, num, lead.rstrip("."), text))
        current = None

    for line in lines:
        if line.startswith("## "):
            flush()
            section = next((s for p, s in _SECTIONS.items() if line.startswith(p)), None)
            continue
        if section is None:
            continue
        m = _ITEM_RE.match(line)
        if m:
            flush()
            current = (section, int(m.group(1)), m.group(2), [line])
        elif current is not None:
            current[3].append(line)
    flush()
    return rules


def calque_section(skill_path: Path | None = None) -> str:
    lines = Path(skill_path or SKILL_PATH).read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line.startswith(_CALQUE_HEADING)
        if inside:
            out.append(line)
    return "\n".join(out).rstrip()


def rule_text(rule_id: str, skill_path: Path | None = None) -> str:
    """Verbatim SKILL.md text of one numbered rule.

    A rule's text runs to the next numbered item or heading, so say-13 owns its
    mechanical self-check paragraph; how-7 also carries the calque table it refers to.
    """
    for rule in parse_skill(skill_path):
        if rule.id == rule_id:
            if rule_id == "how-7":
                return rule.text + "\n\n" + calque_section(skill_path)
            return rule.text
    raise KeyError(rule_id)


def load_registry(path: Path | None = None) -> list[Rule]:
    data = tomllib.loads(Path(path or REGISTRY_PATH).read_text(encoding="utf-8"))
    return [
        Rule(
            id=e["id"],
            section=e["section"],
            number=e["number"],
            fingerprint=e["fingerprint"],
            status=e["status"],
            literals=tuple(e.get("literals", ())),
            patterns=tuple((p["source"], p["justification"]) for p in e.get("patterns", ())),
            reason=e.get("reason", ""),
        )
        for e in data["rule"]
    ]


def sync_problems(
    skill_path: Path | None = None, registry_path: Path | None = None
) -> list[str]:
    """Drift between SKILL.md and the registry; empty when they agree."""
    skill = {r.id: r for r in parse_skill(skill_path)}
    problems: list[str] = []
    seen: set[str] = set()
    for rule in load_registry(registry_path):
        if rule.id in seen:
            problems.append(f"{rule.id}: classified more than once")
        seen.add(rule.id)
        if rule.status not in STATUSES:
            problems.append(f"{rule.id}: unknown status {rule.status!r}")
        if rule.status == "judge-form" and (rule.literals or rule.patterns):
            problems.append(f"{rule.id}: judge-form entry carries literals or patterns")
        found = skill.get(rule.id)
        if found is None:
            problems.append(f"{rule.id}: no such numbered rule in SKILL.md")
            continue
        if found.fingerprint != rule.fingerprint:
            problems.append(f"{rule.id}: fingerprint {rule.fingerprint!r} != SKILL.md {found.fingerprint!r}")
        text = rule_text(rule.id, skill_path)
        for literal in rule.literals:
            if literal not in text:
                problems.append(f"{rule.id}: literal {literal!r} not in the rule text")
    problems += [f"{rid}: numbered rule in SKILL.md is not classified" for rid in skill if rid not in seen]
    return problems


def _literal_regex(literal: str) -> str:
    head = r"(?<!\w)" if re.match(r"\w", literal) else ""
    tail = r"(?!\w)" if re.search(r"\w$", literal) else ""
    return head + re.escape(literal) + tail


@functools.lru_cache(maxsize=1)
def _compiled() -> tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]:
    out = []
    for rule in load_registry():
        if rule.status != "judge":
            continue
        regexes = [_literal_regex(lit) for lit in rule.literals]
        regexes += [source for source, _ in rule.patterns]
        out.append((rule.id, tuple(re.compile(r, re.IGNORECASE) for r in regexes)))
    return tuple(out)


def find_candidates(body: str) -> list[tuple[str, list[str]]]:
    """Judge-checkable rules whose lexical handle occurs in ``body``.

    Candidate generation only: a hit says a rule is worth asking a judge about,
    never that the rule was violated.
    """
    lines = body.splitlines()
    found: list[tuple[str, list[str]]] = []
    for rule_id, regexes in _compiled():
        hits = [_hit(ln) for ln in lines if any(rx.search(ln) for rx in regexes)]
        if hits:
            found.append((rule_id, hits[:_MAX_HIT_LINES]))
    return found


def _form_gate(body: str) -> bool:
    return len(body) >= FORM_MIN_BODY_CHARS


def _tail_offset(body: str) -> int:
    return int(len(body) * FORM_TAIL_START)


def _is_table_separator(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and "-" in s and set(s) <= set("|-: ")


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|") and not _is_table_separator(line)


def _is_list_item(line: str) -> bool:
    return _LIST_ITEM_RE.match(line) is not None


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_numeric_cell(cell: str) -> bool:
    return _NUMERIC_CELL_RE.fullmatch(cell) is not None


@functools.lru_cache(maxsize=None)
def _unit_alternation(words: tuple[str, ...]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


@functools.lru_cache(maxsize=None)
def _unit_number_re(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(_NUMBER + r"\s?(?:%|(?:" + _unit_alternation(words) + r")(?!\w))", re.IGNORECASE)


@functools.lru_cache(maxsize=None)
def _unit_word_re(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)(?:" + _unit_alternation(words) + r")(?!\w)", re.IGNORECASE)


def _unit_numbers(text: str) -> int:
    return len(_unit_number_re(UNIT_WORDS).findall(text))


def _header_has_marker(cell: str) -> bool:
    return any(ch in cell for ch in ",()%") or _unit_word_re(UNIT_WORDS).search(cell) is not None


def _lead_has_table_row(lines: list[str]) -> bool:
    return any(_is_table_row(ln) for ln in lines)


def _split_at_offset(body: str) -> tuple[list[str], list[str]]:
    """Lines starting before the shared lead/tail offset, and lines starting at or after it."""
    offset = _tail_offset(body)
    lead: list[str] = []
    tail: list[str] = []
    pos = 0
    for raw in body.splitlines(keepends=True):
        (tail if pos >= offset else lead).append(raw.rstrip("\r\n"))
        pos += len(raw)
    return lead, tail


def _hit(line: str) -> str:
    return line.strip()[:_MAX_HIT_CHARS]


def _spread(items: list[str], cap: int) -> list[str]:
    """Up to ``cap`` items taken evenly from first through last."""
    if len(items) <= cap or cap < 2:
        return items[:cap]
    picked = sorted({round(k * (len(items) - 1) / (cap - 1)) for k in range(cap)})
    return [items[i] for i in picked]


def _tail_candidates(body: str) -> list[tuple[str, list[str]]]:
    if not _form_gate(body):
        return []
    _, tail = _split_at_offset(body)
    filled = [ln for ln in tail if ln.strip()]
    if not filled:
        return []
    structured = [ln for ln in filled if _is_table_row(ln) or _is_list_item(ln)]
    if len(structured) / len(filled) <= FORM_TAIL_SHARE:
        return []
    hits = [_hit(ln) for ln in _spread(structured, _MAX_HIT_LINES)]
    return [("how-3", hits), ("say-4", list(hits))]


def _header_candidates(body: str) -> list[tuple[str, list[str]]]:
    if not _form_gate(body):
        return []
    lines = body.splitlines()
    hits: list[str] = []
    for i in range(len(lines) - 1):
        if not (_is_table_row(lines[i]) and _is_table_separator(lines[i + 1])):
            continue
        rows = []
        for ln in lines[i + 2:]:
            if not _is_table_row(ln):
                break
            rows.append(_table_cells(ln))
        for col, head in enumerate(_table_cells(lines[i])):
            cells = [r[col] for r in rows if col < len(r)]
            numeric = [c for c in cells if _is_numeric_cell(c)]
            if len(cells) >= 2 and len(numeric) * 2 > len(cells) and not _header_has_marker(head):
                hits.append(_hit(lines[i]))
                break
    return [("say-4", hits[:_MAX_HIT_LINES])] if hits else []


def _estimate_candidates(body: str) -> list[tuple[str, list[str]]]:
    if not _form_gate(body):
        return []
    lead, tail = _split_at_offset(body)
    if _unit_numbers(body) < ESTIMATE_MIN_UNIT_NUMBERS or _lead_has_table_row(lead):
        return []
    hits = [_hit(ln) for ln in tail if _unit_numbers(ln)]
    return [("say-15", hits[:_MAX_HIT_LINES])] if hits else []


def find_form_candidates(body: str) -> list[tuple[str, list[str]]]:
    """``judge-form`` rules whose structural handle occurs in a long ``body``.

    Header hits come first, then tail, then estimate. Candidate generation only, like
    ``find_candidates``: every hit is a verbatim line of ``body`` and says a rule is
    worth asking a judge about, never that it was violated.

    Calibration: tail 36%, header 7%, estimate 5% of 137 repo prose bodies (measured at 77612698 plus this change).
    """
    return _header_candidates(body) + _tail_candidates(body) + _estimate_candidates(body)


def find_all_candidates(body: str) -> list[tuple[str, list[str]]]:
    """``find_candidates`` plus ``find_form_candidates``, equal rule ids merged."""
    merged: dict[str, list[str]] = {}
    for rule_id, hits in find_candidates(body) + find_form_candidates(body):
        merged.setdefault(rule_id, [])
        merged[rule_id] += [h for h in hits if h not in merged[rule_id]]
    return [(rule_id, hits[:_MAX_HIT_LINES]) for rule_id, hits in merged.items()]
