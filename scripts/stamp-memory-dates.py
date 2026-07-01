#!/usr/bin/env python3
"""Backfill `created` / `last_verified` into existing memory leaves.

One-shot migration for the temporal-frontmatter contract
(memory-global/leaves/memory-temporal-frontmatter.md). For each leaf it derives:

  created       git add-date (`git log --diff-filter=A --follow ... | first`),
                else a `YYYY-MM-DD` filename prefix, else the file mtime;
  last_verified git last-commit date (`git log -1 ...`), else the file mtime,
                clamped so last_verified >= created.

The dates are inserted into the existing frontmatter without reordering or
rewriting the body. A leaf with NO YAML frontmatter (common in project /
personal scope) gets a minimal block synthesized and prepended (name=slug,
description=first heading/sentence, type=reference, created, last_verified);
its body is left untouched. (`last_accessed` is a retired field — it is never
written anywhere; the former PostToolUse(Read) stamp hook was removed.)

Idempotent: a leaf that already carries both dates is left unchanged, so a second
run is a no-op. Dry-run by default; pass --apply to write.

Scopes (all SUPPORTED; the operator picks which to EXECUTE):
  global    memory-global/leaves/**            (this repo)
  project   <project-dir>/.claude/agent-memory/**   (requires --project-dir)
  personal  ~/.claude/projects/*/memory/**
  all       the union of the above
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config_root import agent_home

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
FILENAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
HEADING_RE = re.compile(r"^#+\s+(.*\S)\s*$", re.MULTILINE)


# --------------------------------------------------------------------------
# date derivation
# --------------------------------------------------------------------------
def _git_first_line(args: list[str], cwd: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            timeout=10, check=False,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if line.strip():
            return line.strip()
    return None


def _mtime_date(path: Path) -> str:
    return _dt.date.fromtimestamp(path.stat().st_mtime).isoformat()


def derive_created(path: Path) -> str:
    git_add = _git_first_line(
        ["log", "--diff-filter=A", "--follow", "--format=%ad", "--date=short",
         "--", path.name],
        cwd=path.parent,
    )
    if git_add:
        return git_add
    m = FILENAME_DATE_RE.match(path.stem)
    if m:
        return m.group(1)
    return _mtime_date(path)


def derive_last_verified(path: Path, created: str) -> str:
    git_last = _git_first_line(
        ["log", "-1", "--format=%ad", "--date=short", "--", path.name],
        cwd=path.parent,
    )
    verified = git_last or _mtime_date(path)
    # Clamp: last_verified must never precede created (verify-memory-index rule).
    return max(verified, created)


# --------------------------------------------------------------------------
# frontmatter mutation
# --------------------------------------------------------------------------
def _derive_name_desc(body: str, path: Path) -> tuple[str, str]:
    name = path.stem
    hm = HEADING_RE.search(body)
    if hm:
        desc = hm.group(1).strip()
    else:
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), name)
        desc = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0]
    return name, desc[:200]


def stamp_text(text: str, path: Path, created: str, last_verified: str) -> tuple[str, bool]:
    """Return (new_text, changed). Inserts the two dates into existing
    frontmatter, or synthesizes a minimal block when none exists."""
    m = FRONTMATTER_RE.match(text)
    if m:
        fm_body = m.group(1)
        additions = []
        if not re.search(r"^created\s*:", fm_body, re.MULTILINE):
            additions.append(f"created: {created}")
        if not re.search(r"^last_verified\s*:", fm_body, re.MULTILINE):
            additions.append(f"last_verified: {last_verified}")
        if not additions:
            return text, False
        new_fm = fm_body.rstrip("\n") + "\n" + "\n".join(additions)
        return text[: m.start(1)] + new_fm + text[m.end(1):], True
    # No frontmatter — synthesize a minimal block, leave the body untouched.
    name, desc = _derive_name_desc(text, path)
    block = (
        "---\n"
        f"name: {name}\n"
        f"description: {desc}\n"
        "type: reference\n"
        f"created: {created}\n"
        f"last_verified: {last_verified}\n"
        "---\n\n"
    )
    return block + text, True


# --------------------------------------------------------------------------
# scope resolution
# --------------------------------------------------------------------------
def iter_leaves(scope: str, project_dir: str | None):
    roots: list[Path] = []
    if scope in ("global", "all"):
        roots.append(REPO_ROOT / "memory-global" / "leaves")
    if scope in ("project", "all"):
        if not project_dir and scope == "project":
            sys.exit("--scope project requires --project-dir")
        if project_dir:
            roots.append(Path(project_dir) / ".claude" / "agent-memory")
    if scope in ("personal", "all"):
        projects = agent_home() / "projects"  # system root (isolated or legacy)
        if projects.is_dir():
            roots += sorted(projects.glob("*/memory"))
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.md")):
            if p.name == "MEMORY.md":
                continue
            yield p


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scope", choices=["all", "global", "project", "personal"],
                        default="global")
    parser.add_argument("--project-dir")
    parser.add_argument("--apply", action="store_true",
                        help="write changes (default: dry-run)")
    args = parser.parse_args(argv)

    leaves = list(iter_leaves(args.scope, args.project_dir))
    changed: list[str] = []
    for path in leaves:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        created = derive_created(path)
        last_verified = derive_last_verified(path, created)
        new_text, did_change = stamp_text(text, path, created, last_verified)
        if not did_change:
            continue
        changed.append(f"{path}  (created={created}, last_verified={last_verified})")
        if args.apply:
            path.write_text(new_text, encoding="utf-8")

    verb = "stamped" if args.apply else "would stamp"
    print(f"stamp-memory-dates [{args.scope}]: {len(leaves)} leaf/leaves scanned, "
          f"{verb} {len(changed)}")
    for line in changed[:20]:
        print(f"  {line}")
    if len(changed) > 20:
        print(f"  ... and {len(changed) - 20} more")
    if not args.apply and changed:
        print("(dry-run — re-run with --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
