"""The forms of a memory leaf, read from what the leaf says it is.

A leaf's area (regularity, principle, ontology) is read from its frontmatter `schema:` wherever the
file lies, so a leaf converted in place keeps resolving; a leaf without such a schema keeps the
directory rule. A regularity is either induced from experience or given -- taken as known from code,
a document or a measurement -- and the two forms carry different sections. An `ontology/v1` leaf
records a distinction and the difficulty it removes.

Callers reach every seam as `leaf_forms.<name>` so a control can replace one at its module attribute.
"""
from __future__ import annotations

import re
from pathlib import Path

SCHEMA_AREAS = {"regularity/v1": "regularity", "principle/v1": "principle", "ontology/v1": "ontology"}
TIER_AREAS = {"regularities": "regularity", "principles": "principle", "ontology": "ontology"}
CASES_DIR = Path("principles/cases")
INDEX = "MEMORY.md"

INDUCED_SECTIONS = ("## Regularity", "## Generality", "## Refutation", "## Induced from")
GIVEN_SECTIONS = ("## Regularity", "## Given", "## Refutation")
ONTOLOGY_SCHEMA = "ontology/v1"
ONTOLOGY_SECTIONS = ("## Distinction", "## Difficulty", "## Adequacy")
GIVEN_KINDS = ("code", "doc", "measurement")

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def frontmatter_of(text: str) -> str:
    found = _FRONTMATTER.match(text)
    return found.group(1) if found else ""


def field(frontmatter: str, key: str) -> str | None:
    found = re.search(rf"^{re.escape(key)}[ \t]*:(.*)$", frontmatter, re.MULTILINE)
    return found.group(1).strip().strip("\"'") if found else None


def schema_of(text: str) -> str:
    return field(frontmatter_of(text), "schema") or ""


def schema_area(frontmatter: str) -> str | None:
    return SCHEMA_AREAS.get(field(frontmatter, "schema") or "")


def directory_area(rel: Path, tier: str) -> str:
    parts = rel.parts
    if len(parts) == 2 and parts[0] == "experience":
        return "experience"
    if tier == "global" and len(parts) == 3 and parts[:2] == CASES_DIR.parts:
        return "case"
    if len(parts) == 2 and parts[0] == "regularities":
        return "regularity"
    if len(parts) == 2 and parts[0] == "principles":
        return "principle"
    return "other"


def area_of(rel: Path, tier: str, text: str = "") -> str:
    return schema_area(frontmatter_of(text)) or directory_area(rel, tier)


def tier_leaves(root, tier: str) -> list[Path]:
    """The leaf files under `root` that belong to search tier `tier`, wherever they lie."""
    root = Path(root)
    area = TIER_AREAS[tier]
    found = []
    for path in sorted(root.rglob("*.md")):
        if path.name == INDEX:
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        if area_of(path.relative_to(root), "global", text) == area:
            found.append(path)
    return found


def given_provenance(text: str) -> str | None:
    """The kind a regularity is given from, or None for an induced or order-derived one."""
    front = frontmatter_of(text)
    if field(front, "schema") != "regularity/v1":
        return None
    kind = field(front, "given")
    if kind not in GIVEN_KINDS:
        return None
    if field(front, "induced_from") is not None or field(front, "order_derived") is not None:
        return None
    return kind


def regularity_sections(text: str) -> tuple[str, ...]:
    return GIVEN_SECTIONS if field(frontmatter_of(text), "given") is not None else INDUCED_SECTIONS


def _section_text(body: str, heading: str) -> str | None:
    found = re.search(rf"^##[ \t]+{re.escape(heading.removeprefix('## '))}[ \t]*$", body, re.MULTILINE)
    if found is None:
        return None
    rest = body[found.end():]
    following = re.search(r"^##[ \t]", rest, re.MULTILINE)
    return rest[:following.start()] if following else rest


def _missing(text: str, headings) -> list[str]:
    body = text[_FRONTMATTER.match(text).end():] if _FRONTMATTER.match(text) else text
    return [h for h in headings if not (_section_text(body, h) or "").strip()]


def regularity_section_problems(text: str) -> list[str]:
    return _missing(text, regularity_sections(text))


def ontology_section_problems(text: str) -> list[str]:
    if schema_of(text) != ONTOLOGY_SCHEMA:
        return []
    return _missing(text, ONTOLOGY_SECTIONS)


def given_source_problems(text: str, base) -> list[str]:
    """What is wrong with the `given_source` a given regularity names: it must be a relative path
    that, under `base`, names a file."""
    front = frontmatter_of(text)
    kind = field(front, "given")
    if kind is None:
        return []
    if kind not in GIVEN_KINDS:
        return [f"given: {kind!r} is not one of {', '.join(GIVEN_KINDS)}"]
    if field(front, "induced_from") is not None or field(front, "order_derived") is not None:
        return ["given and induced_from are exclusive: a regularity is given or induced, not both"]
    source = field(front, "given_source")
    if not source:
        return ["given_source: a given regularity names the file it is given from"]
    path = Path(source)
    if path.is_absolute() or ".." in path.parts:
        return [f"given_source: {source!r} is not a path inside the repository"]
    if base is None:
        return [f"given_source: {source!r} names no file"]
    root = Path(base).resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        return [f"given_source: {source!r} is not a path inside the repository"]
    if not resolved.is_file():
        return [f"given_source: {source!r} names no file"]
    return []
