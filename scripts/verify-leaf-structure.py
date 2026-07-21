#!/usr/bin/env python3
"""Verify non-experience memory leaves have the required structure.

This verifier also guards the `schema: principle/v1` leaf (grandfathered below,
under `principles/`), which is the **generality>=1 profile** of a single
difficulty-record model. Its **generality-0 profile** is the
`schema: difficulty/v1` experience leaf guarded by the sibling
`verify-experience-leaf.py` — the two schemas are two faces of one continuum
keyed by the `generality` field, not unrelated types. The `leaf/v1` shape this
verifier enforces for ordinary reference/feedback leaves is SEPARATE from that
continuum (an unrelated ordinary-leaf structure), not a point on it.

Rules:
  1. Leaves with `schema: leaf/v1` in frontmatter must contain all three
     H2 sections: `## Difficulty`, `## Guidance`, `## See also`.
  2. Leaves WITHOUT `schema: leaf/v1` are grandfathered:
     - Under system-knowledge/: the difficulty-lead baseline applies —
       description names difficulty OR body leads with a blockquote
       `> **Difficulty …` / `> **Затруднение …`.
     - Under other dirs: passes unconditionally.

Scope: any *.md leaf file under a `leaves/` or `agent-memory/` directory,
  EXCLUDING `experience/` subdirectories and MEMORY.md sub-indexes.
  This subsumes verify-difficulty-lead.py (retired).

Invocation modes (mirrors verify-experience-leaf.py):
  (no args)   Scan all tracked leaves in the repo. Used by verify-all.py.
  --staged    Check files staged for commit (pre-commit hook).
  --hook      PreToolUse mode. Reads tool-input JSON on stdin; validates a
              Write targeting a leaf path. Exit 2 on violation.
  <path>      Ad-hoc CLI check for a single file.

Exit codes:
  0   OK / not applicable
  1   violation in (default) / --staged / <path> mode
  2   violation in --hook mode (blocks the Write tool call)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import memory_dates as md

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
SCHEMA_RE = re.compile(r"^schema\s*:\s*(.*?)\s*$", re.MULTILINE)

# leaf/v1 required H2 sections
V1_SECTIONS = [
    ("## Difficulty", re.compile(r"^##\s+Difficulty\b", re.MULTILINE)),
    ("## Guidance", re.compile(r"^##\s+Guidance\b", re.MULTILINE)),
    ("## See also", re.compile(r"^##\s+See\s+also\b", re.MULTILINE | re.IGNORECASE)),
]

# Difficulty-lead baseline for grandfathered system-knowledge/ leaves
SK_PATH_RE = re.compile(r"(?:^|/)system-knowledge/(?!MEMORY\.md$)[^/]+\.md$")
DESC_RE = re.compile(r"^description\s*:\s*(.*?)\s*$", re.MULTILINE)
DESC_MARKER_RE = re.compile(
    r"Difficulty it removes|Difficulty\s*[—:-]|Затруднение|functional ground",
    re.IGNORECASE)
BODY_MARKER_RE = re.compile(r"^>\s*\*\*(Difficulty|Затруднение)\b", re.MULTILINE)
H1_RE = re.compile(r"^#\s", re.MULTILINE)
SECTION_RE = re.compile(r"^##\s", re.MULTILINE)
DEVELOPED_MIN_LINES = 10


def is_leaf(path: str) -> bool:
    """True if path falls in scope for this verifier."""
    if not path.endswith(".md"):
        return False
    p = Path(path)
    if p.name in ("MEMORY.md", ".gitkeep"):
        return False
    parts = p.parts
    if "leaves" not in parts and "agent-memory" not in parts:
        return False
    if "experience" in parts:
        return False
    return True


def is_sk_leaf(path: str) -> bool:
    return bool(SK_PATH_RE.search(path))


def check_content(content: str, path: str = "") -> str | None:
    """Return None if OK (or not applicable), else a human-readable error."""
    fm = FRONTMATTER_RE.match(content)
    fm_body = fm.group(1) if fm else ""
    body = content[fm.end():] if fm else content

    # Mirror-validate temporal frontmatter: reject malformed dates, but never a
    # leaf merely lacking them (require=False) — verify-memory-index is the
    # universal requirer. See memory_dates.py.
    temporal = md.validate_temporal(fm_body, require=False)
    if temporal:
        return "temporal frontmatter — " + "; ".join(temporal)

    schema_m = SCHEMA_RE.search(fm_body)
    schema = schema_m.group(1).strip().strip("\"'") if schema_m else ""

    if "leaf/v1" in schema:
        missing = [name for name, rx in V1_SECTIONS if not rx.search(body)]
        if missing:
            return ("schema:leaf/v1 leaf missing required section(s): "
                    + ", ".join(missing))
        return None

    # Grandfathered — no schema: leaf/v1
    if is_sk_leaf(path):
        # Carry over verify-difficulty-lead baseline
        desc_m = DESC_RE.search(fm_body)
        desc = desc_m.group(1).strip().strip("\"'") if desc_m else ""
        if DESC_MARKER_RE.search(desc):
            return None
        if not H1_RE.search(body):
            return None  # skeleton, no heading yet
        sections = list(SECTION_RE.finditer(body))
        nonempty = [ln for ln in body.splitlines() if ln.strip()]
        if not sections and len(nonempty) < DEVELOPED_MIN_LINES:
            return None  # stub being built incrementally
        lead_end = sections[0].start() if sections else len(body)
        if BODY_MARKER_RE.search(body[:lead_end]):
            return None
        return ("difficulty not named — neither a `description:` naming the "
                "difficulty it removes nor a body-lead blockquote "
                "(`> **Difficulty …`/`> **Затруднение …`) before the first `## ` "
                "section; lead with the divergence the leaf removes, not a bare fact")

    # Ordinary grandfathered leaf (non-SK): pass
    return None


def _list_paths(mode: str) -> list[str]:
    if mode == "staged":
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    else:
        cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line and is_leaf(line)]


def _read_blob(path: str, mode: str) -> tuple[str | None, str | None]:
    if mode == "staged":
        blob = subprocess.run(["git", "show", f":{path}"],
                              capture_output=True, text=True, check=False)
        if blob.returncode != 0:
            return None, "cannot read staged blob"
        return blob.stdout, None
    p = Path(path)
    if not p.exists():
        return None, "file not found on disk"
    return p.read_text(encoding="utf-8"), None


def _scan(mode: str) -> int:
    paths = _list_paths(mode)
    if not paths:
        print(f"verify-leaf-structure: OK — no leaf files in scope ({mode} mode)")
        return 0
    failed: list[str] = []
    for path in paths:
        content, read_err = _read_blob(path, mode)
        if read_err is not None:
            print(f"verify-leaf-structure: FAIL {path}: {read_err}")
            failed.append(path)
            continue
        err = check_content(content or "", path)
        if err:
            print(f"verify-leaf-structure: FAIL {path}: {err}")
            failed.append(path)
        else:
            print(f"verify-leaf-structure: OK {path}")
    if failed:
        print(
            f"\n{len(failed)} leaf/leaves failed the structure check.\n"
            "For schema:leaf/v1 leaves, add missing sections "
            "(## Difficulty, ## Guidance, ## See also).\n"
            "For system-knowledge/ leaves, name the difficulty in `description:` or "
            "add a body blockquote before the first `## ` section:\n"
            "  > **Difficulty (functional ground):** desired … ; actual … ."
        )
        return 1
    return 0


def cmd_hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"verify-leaf-structure: hook input not valid JSON: {e}", file=sys.stderr)
        return 0  # fail open on parse error
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "") or ""
    if tool != "Write" or not is_leaf(file_path):
        return 0
    err = check_content(tool_input.get("content", "") or "", file_path)
    if err is None:
        return 0
    print(
        "verify-leaf-structure: BLOCK\n"
        f"  Write target: {file_path}\n"
        f"  reason: {err}\n"
        "  rule: schema:leaf/v1 leaves must contain ## Difficulty, ## Guidance,\n"
        "        ## See also; system-knowledge/ leaves must name the difficulty.\n"
        "  recovery: add the missing section(s) and retry the Write.\n",
        file=sys.stderr,
    )
    return 2


def cmd_file(path_str: str) -> int:
    p = Path(path_str)
    if not is_leaf(str(p)):
        print(f"verify-leaf-structure: SKIP {p} (not a leaf path in scope)")
        return 0
    if not p.exists():
        print(f"verify-leaf-structure: FAIL {p}: file not found", file=sys.stderr)
        return 1
    err = check_content(p.read_text(encoding="utf-8"), str(p))
    if err:
        print(f"verify-leaf-structure: FAIL {p}: {err}")
        return 1
    print(f"verify-leaf-structure: OK {p}")
    return 0


def cmd_root(root_str: str) -> int:
    """Check every in-scope *.md leaf under DIR.

    Used for the project-memory layout (`.claude/agent-memory/`), which
    `is_leaf` already recognizes (line-79 path check). Reuses the same scope
    and grandfather rules as cmd_file/_scan: non-opted-in files return None
    from check_content and pass.
    """
    root = Path(root_str)
    if not root.is_dir():
        print(f"verify-leaf-structure: FAIL {root}: not a directory", file=sys.stderr)
        return 1
    paths = sorted(str(p) for p in root.rglob("*.md") if is_leaf(str(p)))
    if not paths:
        print(f"verify-leaf-structure: OK — no leaf files in scope under {root}")
        return 0
    failed: list[str] = []
    for path in paths:
        err = check_content(Path(path).read_text(encoding="utf-8"), path)
        if err:
            print(f"verify-leaf-structure: FAIL {path}: {err}")
            failed.append(path)
        else:
            print(f"verify-leaf-structure: OK {path}")
    if failed:
        print(f"\n{len(failed)} leaf/leaves failed the structure check under {root}.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true",
                       help="check files staged for commit")
    group.add_argument("--hook", action="store_true",
                       help="PreToolUse hook mode: JSON tool input on stdin")
    group.add_argument("--root", metavar="DIR",
                       help="check every *.md leaf under DIR "
                            "(project agent-memory layout)")
    parser.add_argument("path", nargs="?", help="check one file (ad-hoc CLI)")
    args = parser.parse_args(argv)

    if args.hook:
        return cmd_hook()
    if args.root:
        return cmd_root(args.root)
    if args.path:
        return cmd_file(args.path)
    mode = "staged" if args.staged else "all"
    return _scan(mode)


if __name__ == "__main__":
    sys.exit(main())
