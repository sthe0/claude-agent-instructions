"""Tech-writer rules as the published-text gate sees them.

The wording of every rule lives only in ``skills/specializations/tech-writer/SKILL.md``.
``publish-rules.toml`` beside it classifies each numbered rule as judge-checkable
(with candidate literals/patterns) or perception-only (with a reason). This module
parses SKILL.md at runtime, so an edit to a rule reaches the judge prompt without a
second copy; ``scripts/tests/test_writer_rules.py`` fails on any drift between the
two files.

``find_candidates`` is candidate generation only: it picks which bodies are worth a
model judge's time and never decides that a rule was violated.
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
        hits = [
            ln.strip()[:_MAX_HIT_CHARS]
            for ln in lines
            if any(rx.search(ln) for rx in regexes)
        ]
        if hits:
            found.append((rule_id, hits[:_MAX_HIT_LINES]))
    return found
