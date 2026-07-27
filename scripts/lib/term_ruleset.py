"""Layered term-ruleset schema, discovery, and matcher (C1 org-neutrality mechanism).

Difficulty removed: Core must carry zero org-internal identifiers, yet a real
denylist of those identifiers is itself org-specific data — hardcoding it in
Core (as ``check-org-neutral.py`` used to) means Core keeps leaking new org
data every time the list grows. This module is the generic, org-agnostic
mechanism: it knows the *shape* of a term ruleset and how to find/compose one,
never a single real term.

Ruleset file shape (TOML, see ``term-ruleset.example.toml``)::

    [[deny]]
    pattern = "<regex>"
    label = "<short class>"      # optional
    note = "<why>"               # optional

    [[exempt]]
    pattern = "<regex>"
    path = "<glob>"               # optional; unscoped if omitted
    reason = "<why this is safe to publish>"   # REQUIRED, permanent

    [[grandfather]]
    path = "<glob>"
    reason = "<why not yet fixed, and by whom>"  # REQUIRED, temporary

Compose semantics (pinned per item 1 — the layering doctrine's "structured
constants deep-merge at the key level" applied to a table-of-tables):

* ``deny`` patterns UNION across every discovered ruleset — any file is
  scanned against all of them.
* ``exempt`` and ``grandfather`` entries apply ONLY to hits produced by deny
  patterns from the SAME ruleset. An exemption in ruleset A never blinds a
  hit from ruleset B's deny pattern — otherwise a low-stakes personal
  ruleset could silently defeat a stricter team ruleset.
* An ``exempt`` entry suppresses a deny hit only when the hit's span is
  CONTAINED in the exempt pattern's match (occurrence-level, not blanket
  per-file/per-word).
* A ``grandfather`` entry suppresses ALL deny hits (content and path) from
  its own ruleset for any file whose repo-relative path matches its glob —
  a temporary, path-scoped blanket exemption, meant to shrink to zero.

Discovery order (mirrors ``scripts/project_entry/registry.sh``'s resolution):

1. ``$CLAUDE_TERM_RULESET_DIR`` — if set, REPLACES the discovery set (does
   not union with the tiers below); a nonexistent or empty directory yields
   zero rulesets, which is not an error.
2. Personal dir: ``<agent-home>/term-rulesets/``.
3. Team/project dir: ``<project>/.claude/term-rulesets/``.

Both tiers 2 and 3 are unioned when no override is set.

Self-publication refusal: a ruleset file tracked inside the very repo it is
scanning must never load — that repo's users could read its contents and
learn every "hidden" term. Symlinks are resolved (``Path.resolve()``) before
checking ``git ls-files``, because an agent home commonly holds config files
as symlinks *into* the guarded repo.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TERM_RULESET_DIR_ENV = "CLAUDE_TERM_RULESET_DIR"
PERSONAL_DIRNAME = "term-rulesets"
TEAM_DIRNAME = ".claude/term-rulesets"


class RulesetError(ValueError):
    """A ruleset file is malformed, or refused to load (e.g. self-publication)."""


@dataclass(frozen=True)
class DenyRule:
    pattern: str
    label: str
    note: str
    regex: re.Pattern


@dataclass(frozen=True)
class ExemptRule:
    pattern: str
    path_glob: str | None
    reason: str
    regex: re.Pattern


@dataclass(frozen=True)
class GrandfatherRule:
    path_glob: str
    reason: str


@dataclass(frozen=True)
class Ruleset:
    name: str
    source_path: Path
    deny: tuple[DenyRule, ...]
    exempt: tuple[ExemptRule, ...]
    grandfather: tuple[GrandfatherRule, ...]


@dataclass(frozen=True)
class Hit:
    ruleset: str
    pattern: str
    label: str
    note: str
    source: str  # "content" | "path"
    path: str | None
    line_no: int | None
    snippet: str

    def format(self) -> str:
        loc = f"{self.path}:{self.line_no}" if self.line_no else f"{self.path} (path)"
        label = f" [{self.label}]" if self.label else ""
        return f"{loc}{label}: /{self.pattern}/ ruleset={self.ruleset} ...{self.snippet!r}..."


def _compile(pattern: str, where: str) -> re.Pattern:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise RulesetError(f"{where}: invalid regex {pattern!r}: {exc}") from exc


def load_ruleset(path: Path) -> Ruleset:
    """Parse and validate one ruleset TOML file. Raises RulesetError on any defect."""
    import tomllib

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RulesetError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise RulesetError(f"{path}: cannot read: {exc}") from exc

    deny: list[DenyRule] = []
    for i, entry in enumerate(raw.get("deny", [])):
        pattern = entry.get("pattern")
        if not pattern:
            raise RulesetError(f"{path}: deny[{i}] missing required 'pattern'")
        where = f"{path}: deny[{i}] ({pattern!r})"
        deny.append(DenyRule(
            pattern=pattern, label=entry.get("label", ""), note=entry.get("note", ""),
            regex=_compile(pattern, where),
        ))

    exempt: list[ExemptRule] = []
    for i, entry in enumerate(raw.get("exempt", [])):
        pattern = entry.get("pattern")
        reason = entry.get("reason")
        if not pattern:
            raise RulesetError(f"{path}: exempt[{i}] missing required 'pattern'")
        if not reason:
            raise RulesetError(f"{path}: exempt[{i}] ({pattern!r}) missing required 'reason'")
        where = f"{path}: exempt[{i}] ({pattern!r})"
        exempt.append(ExemptRule(
            pattern=pattern, path_glob=entry.get("path"), reason=reason,
            regex=_compile(pattern, where),
        ))

    grandfather: list[GrandfatherRule] = []
    for i, entry in enumerate(raw.get("grandfather", [])):
        path_glob = entry.get("path")
        reason = entry.get("reason")
        if not path_glob:
            raise RulesetError(f"{path}: grandfather[{i}] missing required 'path'")
        if not reason:
            raise RulesetError(f"{path}: grandfather[{i}] ({path_glob!r}) missing required 'reason'")
        grandfather.append(GrandfatherRule(path_glob=path_glob, reason=reason))

    return Ruleset(
        name=path.stem, source_path=path,
        deny=tuple(deny), exempt=tuple(exempt), grandfather=tuple(grandfather),
    )


def _refuse_if_self_published(ruleset_path: Path, guarded_repo_root: Path | None) -> None:
    if guarded_repo_root is None:
        return
    real = ruleset_path.resolve()
    real_root = guarded_repo_root.resolve()
    try:
        rel = real.relative_to(real_root)
    except ValueError:
        return  # not inside the guarded tree at all (after resolving symlinks)
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(rel)],
        cwd=real_root, capture_output=True, text=True, check=False,
    )
    if tracked.returncode == 0:
        raise RulesetError(
            f"{ruleset_path}: self-publication refused — resolves to {real}, which is "
            f"tracked inside the guarded repo ({real_root}) at {rel}. A term ruleset must "
            "not be loadable from inside the tree it guards (loading it would publish the "
            "very terms it hides). Move it to a machine-local directory outside the tree."
        )


def discover_rulesets(
    *,
    agent_home: Path,
    project_dir: Path | None = None,
    guarded_repo_root: Path | None = None,
    env: dict | None = None,
) -> list[Ruleset]:
    """Resolve and load every ruleset per the discovery order (see module docstring).

    ``env`` defaults to ``os.environ`` — overridable for hermetic tests that
    must not touch the real process environment.
    """
    import os

    env = os.environ if env is None else env
    override = env.get(TERM_RULESET_DIR_ENV)
    if override:
        dirs = [Path(override).expanduser()]
    else:
        dirs = [Path(agent_home) / PERSONAL_DIRNAME]
        if project_dir is not None:
            dirs.append(Path(project_dir) / TEAM_DIRNAME)

    rulesets: list[Ruleset] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for toml_path in sorted(d.glob("*.toml")):
            _refuse_if_self_published(toml_path, guarded_repo_root)
            rulesets.append(load_ruleset(toml_path))
    return rulesets


def _deny_hits(text: str, deny: tuple[DenyRule, ...]) -> list[tuple[int, int, DenyRule]]:
    hits = []
    for d in deny:
        for m in d.regex.finditer(text):
            hits.append((m.start(), m.end(), d))
    return hits


def _exempt_spans(text: str, exempt: tuple[ExemptRule, ...], path: str | None) -> list[tuple[int, int]]:
    spans = []
    for e in exempt:
        if e.path_glob and (path is None or not fnmatch.fnmatch(path, e.path_glob)):
            continue
        for m in e.regex.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _contained(span: tuple[int, int], exempt_spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(es <= start and end <= ee for es, ee in exempt_spans)


def _is_grandfathered(path: str, ruleset: Ruleset) -> bool:
    return any(fnmatch.fnmatch(path, g.path_glob) for g in ruleset.grandfather)


def _make_hit(rs: Ruleset, deny: DenyRule, text: str, start: int, end: int,
              source: str, path: str | None, line_no: int | None) -> Hit:
    snippet = text[max(0, start - 30):end + 30]
    return Hit(
        ruleset=rs.name, pattern=deny.pattern, label=deny.label, note=deny.note,
        source=source, path=path, line_no=line_no, snippet=snippet,
    )


def scan(
    content: str,
    rulesets: list[Ruleset],
    *,
    path: str | None = None,
    ignore_grandfather: bool = False,
) -> list[Hit]:
    """Scan ``content`` (and, when ``path`` is given, the path string itself) for deny hits.

    ``path`` is the repo-relative path of the file being scanned — it drives
    path-glob-scoped exemptions, the grandfather check, and the path-name
    scan. Pass ``None`` for non-file text (a commit message, an issue body):
    grandfathering and the path-name scan are then both skipped.
    """
    hits: list[Hit] = []
    for rs in rulesets:
        if path is not None and not ignore_grandfather and _is_grandfathered(path, rs):
            continue

        content_exempt = _exempt_spans(content, rs.exempt, path)
        for start, end, deny in _deny_hits(content, rs.deny):
            if _contained((start, end), content_exempt):
                continue
            line_no = content.count("\n", 0, start) + 1
            hits.append(_make_hit(rs, deny, content, start, end, "content", path, line_no))

        if path is not None:
            path_exempt = _exempt_spans(path, rs.exempt, path)
            for start, end, deny in _deny_hits(path, rs.deny):
                if _contained((start, end), path_exempt):
                    continue
                hits.append(_make_hit(rs, deny, path, start, end, "path", path, None))

    return hits
