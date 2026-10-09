#!/usr/bin/env python3
"""Cursor sessionStart hook: inject compact session context via additional_context.

Read-only. Injects MEMORY.md indexes, config.md key/value constants, and a compact
skill catalog (frontmatter only). Never embeds leaf bodies, skill bodies, or config
Meaning essays.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl.config import CONFIG_KEY_RE  # noqa: E402
from lib import config_root  # noqa: E402

MAX_LINES = 200
MAX_BYTES = 25000
# Whole additional_context payload cap (memory indexes + config table + skill catalog).
MAX_AGGREGATE_BYTES = 120000
SKILL_DESCRIPTION_CATALOG_MAX = 300

_READ_GUIDANCE = (
    "Leaf files and skill bodies are not embedded here. Read specific leaves or "
    "SKILL.md files on demand using the paths listed below."
)

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)
DESCRIPTION_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)

SKILL_GLOBS = (
    "*/SKILL.md",
    "specializations/*/SKILL.md",
)


def cwd_hash(absolute_cwd: str) -> str:
    """Match setup-project-memory.sh: non-alphanumeric chars in abs cwd -> dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(absolute_cwd))


def _resolve_index_candidates(workspace_roots: list[str]) -> list[tuple[str, Path]]:
    """Return (label, path) in display order before deduplication."""
    home = config_root.agent_home()
    candidates: list[tuple[str, Path]] = []

    candidates.append(("global", home / "memory-global" / "MEMORY.md"))

    for root in workspace_roots:
        root_path = Path(root).expanduser()
        try:
            root_abs = str(root_path.resolve())
        except OSError:
            root_abs = os.path.abspath(str(root_path))
        candidates.append(
            (f"project ({root_abs})", Path(root_abs) / ".claude" / "agent-memory" / "MEMORY.md")
        )

    if workspace_roots:
        first_root = workspace_roots[0]
        try:
            first_abs = os.path.abspath(str(Path(first_root).expanduser().resolve()))
        except OSError:
            first_abs = os.path.abspath(str(Path(first_root).expanduser()))
        candidates.append(
            (
                "personal",
                home / "projects" / cwd_hash(first_abs) / "memory" / "MEMORY.md",
            )
        )

    return candidates


def _dedupe_existing(candidates: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    seen: set[str] = set()
    entries: list[tuple[str, Path]] = []
    for label, path in candidates:
        if not path.is_file():
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        entries.append((label, Path(real)))
    return entries


def _read_index(path: Path) -> tuple[str | None, str | None]:
    """Return (body, skip_note). skip_note is set when the index exceeds caps."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    line_count = len(text.splitlines())
    byte_count = len(text.encode("utf-8"))
    if line_count > MAX_LINES or byte_count > MAX_BYTES:
        return None, (
            f"[skipped: index exceeds {MAX_LINES}-line / {MAX_BYTES}-byte cap "
            f"({line_count} lines, {byte_count} bytes) — read on demand]"
        )
    return text, None


def parse_config_constants(config_path: Path) -> tuple[dict[str, str] | None, str | None]:
    """Parse config.md key/value rows; return (constants, skip_note)."""
    if not config_path.is_file():
        return None, None
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, "[skipped: config.md unreadable — read on demand]"
    constants: dict[str, str] = {}
    for line in lines:
        match = CONFIG_KEY_RE.match(line)
        if match:
            constants[match.group(1)] = match.group(2)
    if not constants:
        return None, "[skipped: config.md has no constants table rows]"
    return constants, None


def _format_config_table(constants: dict[str, str], source_path: Path) -> str:
    rows = ["## Coordination constants (key/value only)", "", f"Source: {source_path}", ""]
    rows.append("| Key | Value |")
    rows.append("|---|---|")
    for key in sorted(constants):
        rows.append(f"| `{key}` | `{constants[key]}` |")
    rows.append("")
    rows.append(
        "Meaning essays and non-table prose from config.md are not embedded — "
        "read the source file on demand."
    )
    return "\n".join(rows)


def _parse_skill_frontmatter(path: Path) -> tuple[str | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None, None
    block = match.group(1)
    name_match = NAME_RE.search(block)
    desc_match = DESCRIPTION_RE.search(block)
    name = name_match.group(1).strip() if name_match else path.parent.name
    description = desc_match.group(1).strip() if desc_match else None
    return name, description


def _iter_skill_files(skills_dir: Path) -> list[Path]:
    if not skills_dir.is_dir():
        return []
    paths: list[Path] = []
    for pattern in SKILL_GLOBS:
        paths.extend(sorted(skills_dir.glob(pattern)))
    return [path for path in paths if path.is_file()]


def scan_skill_catalog(skills_dir: Path) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    for path in _iter_skill_files(skills_dir):
        name, description = _parse_skill_frontmatter(path)
        if name is None:
            continue
        catalog.append(
            {
                "name": name,
                "description": description or "",
                "path": str(path.resolve()),
            }
        )
    catalog.sort(key=lambda entry: entry["name"])
    return catalog


def _format_skill_catalog(catalog: list[dict[str, str]]) -> str:
    if not catalog:
        return ""
    rows = [
        "## Skill catalog (frontmatter index only)",
        "",
        _READ_GUIDANCE,
        "",
    ]
    for entry in catalog:
        rows.append(f"- **{entry['name']}** — `{entry['path']}`")
        description = entry["description"]
        if description:
            if len(description) > SKILL_DESCRIPTION_CATALOG_MAX:
                description = description[: SKILL_DESCRIPTION_CATALOG_MAX - 3].rstrip() + "..."
            rows.append(f"  Trigger/description: {description}")
    rows.append("")
    rows.append("Skill bodies are not embedded — invoke by reading the SKILL.md path on demand.")
    return "\n".join(rows)


def _assemble_memory_section(workspace_roots: list[str]) -> str:
    entries = _dedupe_existing(_resolve_index_candidates(workspace_roots))
    if not entries:
        return ""

    sections = ["## Agent memory indexes (sessionStart injection)", "", _READ_GUIDANCE, ""]
    for label, path in entries:
        body, skip_note = _read_index(path)
        sections.append(f"### {label} memory index")
        sections.append(f"Source: {path}")
        if skip_note:
            sections.append(skip_note)
        elif body is not None:
            sections.append("")
            sections.append(body.rstrip())
        sections.append("")
    return "\n".join(sections).rstrip()


def _fit_aggregate_payload(parts: list[str]) -> str:
    """Join sections and trim skill catalog if the aggregate exceeds MAX_AGGREGATE_BYTES."""
    non_empty = [part.strip() for part in parts if part.strip()]
    payload = "\n\n".join(non_empty)
    if len(payload.encode("utf-8")) <= MAX_AGGREGATE_BYTES:
        return payload

    memory_part = non_empty[0] if non_empty else ""
    config_part = non_empty[1] if len(non_empty) > 1 else ""
    skill_part = non_empty[2] if len(non_empty) > 2 else ""

    catalog_match = re.search(
        r"(## Skill catalog \(frontmatter index only\).*?)(?=\nSkill bodies are not embedded|\Z)",
        skill_part,
        flags=re.DOTALL,
    )
    if catalog_match:
        prefix = skill_part[: catalog_match.start()]
        suffix = skill_part[catalog_match.end() :]
        bullet_lines = [
            line
            for line in catalog_match.group(1).splitlines()
            if line.startswith("- **") or line.startswith("  Trigger/description:")
        ]
        trimmed_lines: list[str] = []
        for line in bullet_lines:
            if line.startswith("- **"):
                trimmed_lines.append(line)
            elif trimmed_lines and trimmed_lines[-1].startswith("- **"):
                trimmed_lines.append(line)
        while trimmed_lines:
            candidate_catalog = "\n".join(
                [
                    "## Skill catalog (frontmatter index only)",
                    "",
                    _READ_GUIDANCE,
                    "",
                    *trimmed_lines,
                ]
            )
            candidate_skill = f"{prefix}{candidate_catalog}{suffix}".strip()
            candidate_parts = [part for part in (memory_part, config_part, candidate_skill) if part]
            candidate_payload = "\n\n".join(candidate_parts)
            if len(candidate_payload.encode("utf-8")) <= MAX_AGGREGATE_BYTES:
                note = (
                    f"[truncated: skill catalog trimmed to fit {MAX_AGGREGATE_BYTES}-byte "
                    "sessionStart aggregate cap]"
                )
                return candidate_payload + "\n\n" + note
            if len(trimmed_lines) >= 2:
                trimmed_lines = trimmed_lines[:-2]
            else:
                trimmed_lines = []

    note = (
        f"[truncated: sessionStart payload exceeds {MAX_AGGREGATE_BYTES}-byte aggregate cap — "
        "read config.md and skills on demand]"
    )
    fallback_parts = [part for part in (memory_part, config_part) if part]
    fallback = "\n\n".join(fallback_parts)
    if fallback:
        return fallback + "\n\n" + note
    return note


def assemble_context(workspace_roots: list[str]) -> str:
    home = config_root.agent_home()
    parts: list[str] = []

    memory_section = _assemble_memory_section(workspace_roots)
    if memory_section:
        parts.append(memory_section)

    config_path = home / "config.md"
    constants, config_skip = parse_config_constants(config_path)
    if constants:
        parts.append(_format_config_table(constants, config_path.resolve()))
    elif config_skip:
        parts.append(
            "\n".join(
                [
                    "## Coordination constants (key/value only)",
                    "",
                    f"Source: {config_path.resolve()}",
                    config_skip,
                ]
            )
        )

    catalog = scan_skill_catalog(config_root.skills_dir())
    skill_section = _format_skill_catalog(catalog)
    if skill_section:
        parts.append(skill_section)

    if not parts:
        return ""
    return _fit_aggregate_payload(parts)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        return 0

    roots = payload.get("workspace_roots")
    if not isinstance(roots, list):
        roots = []
    workspace_roots = [str(root) for root in roots if isinstance(root, str) and root.strip()]

    context = assemble_context(workspace_roots)
    if context:
        print(json.dumps({"additional_context": context}, ensure_ascii=False))
    else:
        print(json.dumps({}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
