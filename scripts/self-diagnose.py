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

Three read-only, mechanical scans over the agent's OWN functional elements:

  oversized-index      any MEMORY.md over a line-count threshold (default
                        MEMORY_INDEX_LINE_THRESHOLD, matching the harness
                        auto-load truncation memory-global/MEMORY.md already
                        documents: "anything past the first 200 lines is
                        truncated").
  dangling-pointer      a markdown link in a MEMORY.md whose local .md target
                        does not exist on disk.
  ceiling-proximity      a governed instructions file (CLAUDE.md, README,
                        cursor mirror, any SKILL.md/policy.md) at or above
                        lint-prose-length.py's own WARN threshold — reused via
                        import, never re-derived here.

Never edits anything. `main()` prints the worklist and exits 1 if non-empty
(0 if clean) — for use as a scriptable check; the SessionStart hook that
drives this at session start is always fail-open regardless of this exit code.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# Mirrors memory-global/MEMORY.md's own "keep this index under ~200 lines" note.
MEMORY_INDEX_LINE_THRESHOLD = 200

_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:")


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
        for m in _LINK_RE.finditer(text):
            target = m.group(1).strip()
            if not target or _is_external(target):
                continue
            if not target.endswith(".md") and "#" not in target:
                continue  # not a file-shaped local pointer (anchors-only, etc.)
            if _pointer_target_missing(md, target):
                out.append(Difficulty("dangling-pointer", str(md.relative_to(memory_root)), target))
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

    byte_limit_raw = constants.get("claude-md-max-bytes")
    claude_md = repo_root / "CLAUDE.md"
    if byte_limit_raw is not None and claude_md.is_file():
        try:
            byte_limit = int(byte_limit_raw)
            nbytes = len(claude_md.read_text(encoding="utf-8").encode("utf-8"))
        except (ValueError, OSError):
            byte_limit = nbytes = None
        if byte_limit is not None:
            level = mod.check_level(nbytes, byte_limit)
            if level in ("warn", "fail"):
                out.append(
                    Difficulty("ceiling-proximity", "CLAUDE.md", f"{nbytes}B {level} of {byte_limit}B (claude-md-max-bytes)")
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
) -> "list[Difficulty]":
    out: "list[Difficulty]" = []
    for root in memory_roots:
        for d in scan_oversized_indexes(root, threshold):
            out.append(Difficulty(d.kind, f"{root}/{d.path}", d.detail))
        for d in scan_dangling_pointers(root):
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
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    memory_roots = [Path(p) for p in args.memory_root] if args.memory_root else default_memory_roots()
    repo_root = None if args.no_repo else Path(args.repo_root) if args.repo_root else REPO_ROOT

    findings = scan(memory_roots, repo_root, threshold=args.threshold)

    if args.json:
        print(json.dumps([asdict(d) for d in findings], indent=2))
    else:
        for d in findings:
            print(d.as_line())

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
