#!/usr/bin/env python3
"""Verify system-knowledge leaves lead with the difficulty they remove.

Rule (CLAUDE.md § Memory § `system-knowledge/` leaves):
  "Lead each leaf with the difficulty it removes — describe the
  component/process by the divergence it exists to resolve (its functional
  ground), not as a free-floating fact."

A leaf that opens with a bare fact instead of the desired-vs-actual
divergence is the recurring failure this check makes mechanical (observed
2026-06-11: two freshly written system-knowledge leaves both opened with a
system fact; the difficulty was recoverable but not the lead, and a
checkbox-style review passed them). A leaf passes if it names the difficulty
in EITHER of two established conventions:

  (a) frontmatter `description:` names it — e.g. "Difficulty it removes — …"
      (the difficulty sits in the recall surface loaded for relevance);
  (b) the body opens, BEFORE the first `## ` section, with a blockquote:
        > **Difficulty (functional ground):** ...
        > **Затруднение (functional ground):** ...   # project memory, RU

A bare fact in both the description and the body lead is the violation.

Scope: `**/system-knowledge/*.md` (excluding the `MEMORY.md` sub-index).
This deliberately does NOT cover generic `leaves/*.md` (reference / feedback
runbooks where a forced difficulty blockquote would be noise) — narrow to the
surface CLAUDE.md already mandates. Experience leaves are covered separately
by verify-experience-leaf.py (their `## Difficulty` section).

Stub tolerance: a Write that carries only frontmatter + an H1 (a skeleton
being built incrementally) is not yet a "developed" leaf and passes — the
check fires once the leaf has a `## ` section or >= 10 non-empty body lines,
so incremental authoring is not blocked while a completed leaf is caught.

Invocation modes (mirrors verify-experience-leaf.py):
  (no args)   Scan all tracked system-knowledge leaves. Used by verify-all.py.
  --staged    Check files staged for commit (pre-commit hook); validates the
              staged blob, not the working tree.
  --hook      PreToolUse mode. Reads tool-input JSON on stdin; validates a
              Write targeting `**/system-knowledge/*.md`. Exit 2 (block +
              stderr to the model) on violation. This is the ONLY enforcement
              point for project memory (a separate git repo whose pre-commit
              does not run verify-all).
  <path>      Ad-hoc CLI check for a single file path.

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

SK_PATH_RE = re.compile(r"(^|/)system-knowledge/(?!MEMORY\.md$)[^/]+\.md$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
DESC_RE = re.compile(r"^description\s*:\s*(.*?)\s*$", re.MULTILINE)
H1_RE = re.compile(r"^#\s", re.MULTILINE)
SECTION_RE = re.compile(r"^##\s", re.MULTILINE)
# A leaf satisfies "lead with the difficulty" in EITHER of two established
# conventions:
#   (a) the difficulty is named in the frontmatter `description` (the recall
#       surface), e.g. "Difficulty it removes — …";
#   (b) the body opens, before the first `## ` section, with a blockquote
#       `> **Difficulty …` / `> **Затруднение …`.
DESC_MARKER_RE = re.compile(
    r"Difficulty it removes|Difficulty\s*[—:-]|Затруднение|functional ground",
    re.IGNORECASE)
BODY_MARKER_RE = re.compile(r"^>\s*\*\*(Difficulty|Затруднение)\b", re.MULTILINE)

DEVELOPED_MIN_LINES = 10


def is_sk_leaf(path: str) -> bool:
    return bool(SK_PATH_RE.search(path))


def check_content(content: str) -> str | None:
    """Return None if OK (or not a developed leaf), else a human-readable error."""
    fm = FRONTMATTER_RE.match(content)
    fm_body = fm.group(1) if fm else ""
    body = content[fm.end():] if fm else content
    # (a) difficulty named in the frontmatter description (recall surface).
    desc_m = DESC_RE.search(fm_body)
    desc = desc_m.group(1).strip().strip("\"'") if desc_m else ""
    if DESC_MARKER_RE.search(desc):
        return None
    if not H1_RE.search(body):
        return None  # skeleton / no heading yet — fail open
    sections = list(SECTION_RE.finditer(body))
    nonempty = [ln for ln in body.splitlines() if ln.strip()]
    if not sections and len(nonempty) < DEVELOPED_MIN_LINES:
        return None  # stub being built incrementally — do not block yet
    # (b) difficulty-lead blockquote in the body, before the first section.
    lead_end = sections[0].start() if sections else len(body)
    if BODY_MARKER_RE.search(body[:lead_end]):
        return None
    return ("difficulty not named — neither a `description:` naming the "
            "difficulty it removes nor a body-lead blockquote "
            "(`> **Difficulty …`/`> **Затруднение …`) before the first `## ` "
            "section; lead with the divergence the leaf removes, not a bare fact")


def _list_paths(mode: str) -> list[str]:
    if mode == "staged":
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    else:
        cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line and is_sk_leaf(line)]


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
        print(f"verify-difficulty-lead: OK — no system-knowledge leaves ({mode} mode)")
        return 0
    failed: list[str] = []
    for path in paths:
        content, read_err = _read_blob(path, mode)
        if read_err is not None:
            print(f"verify-difficulty-lead: FAIL {path}: {read_err}")
            failed.append(path)
            continue
        err = check_content(content or "")
        if err:
            print(f"verify-difficulty-lead: FAIL {path}: {err}")
            failed.append(path)
        else:
            print(f"verify-difficulty-lead: OK {path}")
    if failed:
        print(f"\n{len(failed)} system-knowledge leaf/leaves do not lead with a "
              "difficulty marker. Add a blockquote near the top, before the first\n"
              "section:\n"
              "  > **Difficulty (functional ground):** desired … ; actual … .\n"
              "CLAUDE.md § Memory: lead each leaf with the difficulty it removes.")
        return 1
    return 0


def cmd_hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"verify-difficulty-lead: hook input not valid JSON: {e}", file=sys.stderr)
        return 0  # fail open on parse error
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "") or ""
    if tool != "Write" or not is_sk_leaf(file_path):
        return 0
    err = check_content(tool_input.get("content", "") or "")
    if err is None:
        return 0
    print(
        "verify-difficulty-lead: BLOCK\n"
        f"  Write target: {file_path}\n"
        f"  reason: {err}\n"
        "  rule: CLAUDE.md § Memory — system-knowledge leaves lead with the\n"
        "        difficulty (functional ground) they remove.\n"
        "  recovery: EITHER name it in the frontmatter `description:`\n"
        "    (\"Difficulty it removes — …\"), OR add a body lead before the\n"
        "    first `## ` section:\n"
        "      > **Difficulty (functional ground):** desired … ; actual … .\n"
        "  then retry the Write.\n",
        file=sys.stderr,
    )
    return 2


def cmd_file(path_str: str) -> int:
    p = Path(path_str)
    if not is_sk_leaf(str(p)):
        print(f"verify-difficulty-lead: SKIP {p} (not a system-knowledge leaf path)")
        return 0
    if not p.exists():
        print(f"verify-difficulty-lead: FAIL {p}: file not found", file=sys.stderr)
        return 1
    err = check_content(p.read_text(encoding="utf-8"))
    if err:
        print(f"verify-difficulty-lead: FAIL {p}: {err}")
        return 1
    print(f"verify-difficulty-lead: OK {p}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true",
                       help="check files staged for commit (pre-commit / verify-all)")
    group.add_argument("--hook", action="store_true",
                       help="PreToolUse hook mode: JSON tool input on stdin")
    parser.add_argument("path", nargs="?", help="check one file (ad-hoc CLI)")
    args = parser.parse_args(argv)

    if args.hook:
        return cmd_hook()
    if args.path:
        return cmd_file(args.path)
    mode = "staged" if args.staged else "all"
    return _scan(mode)


if __name__ == "__main__":
    sys.exit(main())
