#!/usr/bin/env python3
"""Mechanically enumerate observable self-friction difficulties.

Difficulty removed: proactive self-diagnosis (CLAUDE.md § When the work is
stuck) has a DECIDABLE rule part — which memory/instruction signals count as
self-friction, at what threshold — that used to live only as a forgettable
"notice when..." directive. This script determinizes that rule part; the
PERCEPTION part (is a flagged item worth re-norming, and how) stays the
model's, surfaced as a normal difficulty declaration once this scanner's
worklist reaches a session (see reflexive-exit-is-base-activity-figure.md —
material = the norm; this scanner supplies the "actual" half of that
material/result comparison).

Read-only, mechanical scans over the agent's OWN functional elements:

  oversized-index      any MEMORY.md over a line-count threshold (default
                        MEMORY_INDEX_LINE_THRESHOLD, matching the harness
                        auto-load truncation memory-global/MEMORY.md already
                        documents: "anything past the first 200 lines is
                        truncated" — CONFIRMED real, not cosmetic; verified
                        2026-07-23 against the installed client bundle, see
                        memory-global/MEMORY.md line 5).
  dangling-pointer      a markdown link in a MEMORY.md whose local .md target
                        does not exist on disk.
  ceiling-proximity      a governed instructions file (CLAUDE.md, README,
                        cursor mirror, any SKILL.md/policy.md) at or above
                        lint-prose-length.py's own WARN threshold — reused via
                        import, never re-derived here.
  broken-hook-registration
                        a settings.json hook whose command names an absolute
                        script path that does not exist on disk (a runtime
                        "hook not found" waiting to happen).
  near-duplicate        two memory leaves whose frontmatter name+description
                        overlap above a Jaccard threshold — memory-hierarchy.md's
                        "generalize and group" norm, mechanized as a flag (never
                        an auto-merge: the merge decision stays the model's).
  orphan-leaf/-index    a .md under a memory root that no MEMORY.md index links
                        to — the reachability half of the same norm. Reachability
                        follows both markdown [](path) links and [[slug]]
                        wikilinks (resolved via a frontmatter name: index).

The DECIDABLE rule is mechanized here; the PERCEPTION (is a flagged candidate
worth re-norming, and how) stays the model's — this scanner only detects and
lists, it never merges, moves, or edits.

Never edits anything. `main()` prints the worklist and exits 1 if non-empty
(0 if clean) — for use as a scriptable check; the SessionStart hook that
drives this at session start is always fail-open regardless of this exit code.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# Mirrors memory-global/MEMORY.md's own "keep this index under ~200 lines" note
# (CONFIRMED real cap, verified 2026-07-23 — see that file's line 5).
MEMORY_INDEX_LINE_THRESHOLD = 200

# Heuristic default: two leaves whose frontmatter name+description token sets
# overlap at or above this Jaccard similarity are flagged as near-duplicates
# ("generalize and group" candidates). Tunable via --near-dup-threshold.
NEAR_DUPLICATE_JACCARD_THRESHOLD = 0.6

_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:")
_FM_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)
_FM_DESC_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Difficulty:
    kind: str
    path: str
    detail: str

    def as_line(self) -> str:
        return f"{self.kind}: {self.path} — {self.detail}"


def scan_oversized_indexes(
    memory_root: Path, threshold: int = MEMORY_INDEX_LINE_THRESHOLD
) -> "list[Difficulty]":
    out: "list[Difficulty]" = []
    if not memory_root.is_dir():
        return out
    for md in sorted(memory_root.rglob("MEMORY.md")):
        try:
            n = len(md.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
        if n > threshold:
            out.append(
                Difficulty("oversized-index", str(md.relative_to(memory_root)), f"{n} lines > {threshold}")
            )
    return out


def _is_external(target: str) -> bool:
    return target.startswith(_EXTERNAL_PREFIXES)


def _pointer_target_missing(md: Path, target: str) -> bool:
    target_file = target.split("#", 1)[0].strip()
    if not target_file:
        return False
    expanded = Path(target_file).expanduser()
    resolved = expanded if expanded.is_absolute() else (md.parent / expanded)
    return not resolved.exists()


def scan_dangling_pointers(memory_root: Path) -> "list[Difficulty]":
    out: "list[Difficulty]" = []
    if not memory_root.is_dir():
        return out
    for md in sorted(memory_root.rglob("MEMORY.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        # Strip HTML comment blocks first: a link inside <!-- ... --> is
        # inert markup, not a live pointer, so it must not be flagged.
        text = _HTML_COMMENT_RE.sub("", text)
        for m in _LINK_RE.finditer(text):
            target = m.group(1).strip()
            if not target or _is_external(target):
                continue
            if "<" in target or ">" in target:
                continue  # placeholder like leaves/<slug>.md, not a real path
            if not target.endswith(".md") and "#" not in target:
                continue  # not a file-shaped local pointer (anchors-only, etc.)
            if _pointer_target_missing(md, target):
                out.append(Difficulty("dangling-pointer", str(md.relative_to(memory_root)), target))
    return out


def _hook_script_path(command: str) -> "Path | None":
    """The absolute script path a hook command invokes, or None when there is
    nothing on disk to resolve.

    We expand ~ and env vars ($CLAUDE_PROJECT_DIR etc.), take the command's
    leading token, and return it only when it is absolute. Bare commands
    (`jq`, `bash -c ...`) and unresolved vars (a `$CLAUDE_PROJECT_DIR` left
    literal because the var is unset) stay unflagged — fail-safe: we would
    rather miss a broken hook than false-flag a legitimate bare command.
    """
    if not command:
        return None
    expanded = os.path.expanduser(os.path.expandvars(command))
    try:
        tokens = shlex.split(expanded)
    except ValueError:
        return None
    if not tokens:
        return None
    lead = Path(tokens[0])
    return lead if lead.is_absolute() else None


def scan_broken_hooks(settings_paths: "list[Path]") -> "list[Difficulty]":
    """Flag settings.json hook commands whose absolute script does not exist."""
    out: "list[Difficulty]" = []
    for sp in settings_paths:
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, matchers in hooks.items():
            if not isinstance(matchers, list):
                continue
            for matcher in matchers:
                if not isinstance(matcher, dict):
                    continue
                for hk in matcher.get("hooks", []) or []:
                    if not isinstance(hk, dict):
                        continue
                    script = _hook_script_path(hk.get("command", ""))
                    if script is not None and not script.exists():
                        out.append(
                            Difficulty("broken-hook-registration", str(sp), f"{event}: {script} not found")
                        )
    return out


def _frontmatter_tokens(md: Path) -> "set[str] | None":
    """Lowercase alphanumeric token set of a leaf's frontmatter name+description,
    or None when the file has no frontmatter (e.g. a MEMORY.md index)."""
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    fm = text[3:end]
    parts: "list[str]" = []
    name_m = _FM_NAME_RE.search(fm)
    if name_m:
        parts.append(name_m.group(1))
    desc_m = _FM_DESC_RE.search(fm)
    if desc_m:
        parts.append(desc_m.group(1))
    if not parts:
        return None
    tokens = {t for t in re.split(r"[^a-z0-9]+", " ".join(parts).lower()) if t}
    return tokens or None


def scan_near_duplicates(
    memory_root: Path, threshold: float = NEAR_DUPLICATE_JACCARD_THRESHOLD
) -> "list[Difficulty]":
    """Flag leaf pairs whose frontmatter name+description Jaccard-overlap >= threshold.

    Only flags (perception — whether to actually merge/generalize — stays the
    model's); mechanizes memory-hierarchy.md's "generalize and group" norm."""
    out: "list[Difficulty]" = []
    if not memory_root.is_dir():
        return out
    entries: "list[tuple[Path, set[str]]]" = []
    for md in sorted(memory_root.rglob("*.md")):
        toks = _frontmatter_tokens(md)
        if toks:
            entries.append((md, toks))
    for i in range(len(entries)):
        a_path, a = entries[i]
        for j in range(i + 1, len(entries)):
            b_path, b = entries[j]
            union = a | b
            if not union:
                continue
            jaccard = len(a & b) / len(union)
            if jaccard >= threshold:
                out.append(
                    Difficulty(
                        "near-duplicate",
                        str(a_path.relative_to(memory_root)),
                        f"{jaccard:.2f} Jaccard vs {b_path.relative_to(memory_root)}",
                    )
                )
    return out


def _link_target_path(source: Path, target: str) -> "Path | None":
    """Resolve a local .md link target relative to its source file, or None
    when the target is not a file-shaped local .md pointer."""
    target_file = target.split("#", 1)[0].strip()
    if not target_file or not target_file.endswith(".md"):
        return None
    if "<" in target_file or ">" in target_file:
        return None  # placeholder like leaves/<slug>.md
    expanded = Path(target_file).expanduser()
    base = expanded if expanded.is_absolute() else source.parent / expanded
    return base.resolve()


def _build_name_index(memory_root: Path) -> "dict[str, Path]":
    """Map frontmatter name: slug -> resolved .md path, for [[wikilink]]
    resolution. First occurrence of a slug wins; files with no frontmatter
    (MEMORY.md indexes typically have none) are skipped naturally."""
    index: "dict[str, Path]" = {}
    for md in sorted(memory_root.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end == -1:
            continue
        name_m = _FM_NAME_RE.search(text[3:end])
        if not name_m:
            continue
        slug = name_m.group(1).strip()
        if slug:
            index.setdefault(slug, md.resolve())
    return index


def scan_orphans(memory_root: Path) -> "list[Difficulty]":
    """Flag any .md under memory_root that no MEMORY.md index reaches.

    BFS from the root MEMORY.md over local .md links AND [[slug]] wikilinks
    (resolved via a frontmatter name: index), recursing only into linked
    MEMORY.md index files (leaves are terminal). Reachability is the other
    half of the "generalize and group" norm: a leaf nobody indexes is
    invisible to the model at recall time."""
    out: "list[Difficulty]" = []
    if not memory_root.is_dir():
        return out
    root_index = memory_root / "MEMORY.md"
    if not root_index.is_file():
        # Do NOT flag every .md as an orphan from a non-existent BFS root. But a
        # root with no .md at all has nothing to strand — stay silent there.
        if any(memory_root.rglob("*.md")):
            out.append(
                Difficulty("no-root-index", str(memory_root), "no top-level MEMORY.md; skipping orphan sweep")
            )
        return out

    name_index = _build_name_index(memory_root)
    visited: "set[Path]" = set()
    queued: "set[Path]" = {root_index.resolve()}
    stack: "list[Path]" = [root_index.resolve()]
    while stack:
        current = stack.pop()
        visited.add(current)
        if current.name != "MEMORY.md":
            continue  # only index files fan out; leaves are terminal
        try:
            text = _HTML_COMMENT_RE.sub("", current.read_text(encoding="utf-8"))
        except OSError:
            continue
        for m in _LINK_RE.finditer(text):
            target = m.group(1).strip()
            if not target or _is_external(target):
                continue
            resolved = _link_target_path(current, target)
            if resolved is None or not resolved.exists():
                continue  # dangling targets are scan_dangling_pointers' job
            if resolved not in queued:
                queued.add(resolved)
                stack.append(resolved)
        for m in _WIKILINK_RE.finditer(text):
            slug = m.group(1).split("|", 1)[0].split("#", 1)[0].strip()
            resolved = name_index.get(slug)
            if resolved is None or not resolved.exists():
                continue  # unknown slug: a genuine orphan is still caught below
            if resolved not in queued:
                queued.add(resolved)
                stack.append(resolved)

    for md in sorted(memory_root.rglob("*.md")):
        if md.resolve() not in visited:
            kind = "orphan-index" if md.name == "MEMORY.md" else "orphan-leaf"
            out.append(
                Difficulty(kind, str(md.relative_to(memory_root)), "not reachable from root MEMORY.md")
            )
    return out


def default_settings_paths() -> "list[Path]":
    """User + project settings.json / settings.local.json that exist on disk,
    de-duplicated by resolved path (a project symlink to a shared file scanned
    once). $CLAUDE_PROJECT_DIR (else cwd) supplies the project location — both
    are generic Claude Code settings homes, not arc/composed-dir specifics."""
    home = Path.home()
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    proj_dir = Path(proj) if proj else Path.cwd()
    candidates = [
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        proj_dir / ".claude" / "settings.json",
        proj_dir / ".claude" / "settings.local.json",
    ]
    seen: "set[Path]" = set()
    out: "list[Path]" = []
    for c in candidates:
        if not c.is_file():
            continue
        try:
            key = c.resolve()
        except OSError:
            key = c
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _load_lint_prose_length(repo_root: Path):
    lint_path = repo_root / "scripts" / "lint-prose-length.py"
    if not lint_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("self_diagnose_lint_prose_length", lint_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scan_ceiling_proximity(repo_root: Path) -> "list[Difficulty]":
    """WARN/FAIL governed-file proximity, reusing lint-prose-length.py's own
    ceilings and classify() — never re-derives the numeric limits here."""
    mod = _load_lint_prose_length(repo_root)
    if mod is None:
        return []
    out: "list[Difficulty]" = []
    try:
        constants = mod.parse_config_md()
    except OSError:
        return out

    char_limit_raw = constants.get("claude-md-max-chars")
    claude_md = repo_root / "CLAUDE.md"
    if char_limit_raw is not None and claude_md.is_file():
        try:
            char_limit = int(char_limit_raw)
            nchars = len(claude_md.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            char_limit = nchars = None
        if char_limit is not None:
            level = mod.check_level(nchars, char_limit)
            if level in ("warn", "fail"):
                out.append(
                    Difficulty("ceiling-proximity", "CLAUDE.md", f"{nchars} chars {level} of {char_limit} chars (claude-md-max-chars)")
                )

    for glob_pat, key in mod.GOVERNED:
        limit_raw = constants.get(key)
        if limit_raw is None:
            continue
        try:
            limit = int(limit_raw)
        except ValueError:
            continue
        for f in sorted(repo_root.glob(glob_pat)):
            try:
                n = len(f.read_text(encoding="utf-8").splitlines())
            except OSError:
                continue
            level = mod.check_level(n, limit)
            if level in ("warn", "fail"):
                out.append(
                    Difficulty("ceiling-proximity", str(f.relative_to(repo_root)), f"{n} lines {level} of {limit} ({key})")
                )
    return out


def default_memory_roots() -> "list[Path]":
    home = Path.home()
    roots: "list[Path]" = []
    global_mem = home / ".claude-agent" / "memory-global"
    if global_mem.is_dir():
        roots.append(global_mem)
    projects_dir = home / ".claude-agent" / "projects"
    if projects_dir.is_dir():
        for proj in sorted(projects_dir.iterdir()):
            mem = proj / "memory"
            if mem.is_dir():
                roots.append(mem)
    cwd_mem = Path.cwd() / ".claude" / "agent-memory"
    if cwd_mem.is_dir():
        roots.append(cwd_mem)
    return roots


def scan(
    memory_roots: "list[Path]",
    repo_root: "Path | None",
    threshold: int = MEMORY_INDEX_LINE_THRESHOLD,
    settings_paths: "list[Path] | None" = None,
    scan_hooks: bool = True,
    near_dup_threshold: float = NEAR_DUPLICATE_JACCARD_THRESHOLD,
) -> "list[Difficulty]":
    out: "list[Difficulty]" = []
    if scan_hooks:
        paths = settings_paths if settings_paths is not None else default_settings_paths()
        out.extend(scan_broken_hooks(paths))
    for root in memory_roots:
        for d in scan_oversized_indexes(root, threshold):
            out.append(Difficulty(d.kind, f"{root}/{d.path}", d.detail))
        for d in scan_dangling_pointers(root):
            out.append(Difficulty(d.kind, f"{root}/{d.path}", d.detail))
        for d in scan_near_duplicates(root, near_dup_threshold):
            out.append(Difficulty(d.kind, f"{root}/{d.path}", d.detail))
        for d in scan_orphans(root):
            out.append(Difficulty(d.kind, f"{root}/{d.path}", d.detail))
    if repo_root is not None:
        out.extend(scan_ceiling_proximity(repo_root))
    return out


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memory-root", action="append", default=None, help="repeatable; overrides the default memory-root discovery")
    parser.add_argument("--repo-root", default=None, help="instructions repo root for the ceiling-proximity scan (default: this script's own repo)")
    parser.add_argument("--no-repo", action="store_true", help="skip the ceiling-proximity scan")
    parser.add_argument("--threshold", type=int, default=MEMORY_INDEX_LINE_THRESHOLD)
    parser.add_argument("--settings-path", action="append", default=None, help="repeatable; overrides the default settings.json discovery for the broken-hook-registration scan")
    parser.add_argument("--no-hooks", action="store_true", help="skip the broken-hook-registration scan")
    parser.add_argument("--near-dup-threshold", type=float, default=NEAR_DUPLICATE_JACCARD_THRESHOLD, help="Jaccard threshold for the near-duplicate scan")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    memory_roots = [Path(p) for p in args.memory_root] if args.memory_root else default_memory_roots()
    repo_root = None if args.no_repo else Path(args.repo_root) if args.repo_root else REPO_ROOT
    settings_paths = [Path(p) for p in args.settings_path] if args.settings_path else None

    findings = scan(
        memory_roots,
        repo_root,
        threshold=args.threshold,
        settings_paths=settings_paths,
        scan_hooks=not args.no_hooks,
        near_dup_threshold=args.near_dup_threshold,
    )

    if args.json:
        print(json.dumps([asdict(d) for d in findings], indent=2))
    else:
        for d in findings:
            print(d.as_line())

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
