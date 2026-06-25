#!/usr/bin/env python3
"""Verify experience leaves carry an explicit user-confirmation quote.

Rule (CLAUDE.md § On task resolution):
  An experience leaf — any markdown file inside an `experience/` directory —
  may only be written after the user explicitly confirms task resolution.
  The frontmatter must carry the user's literal confirmation quote:

      resolution_confirmed_by_user: "<user quote>"

  Writing a leaf on assumed resolution is a recurring failure mode (see
  memory-global/leaves/experience/2026-05-25-resolution-gate-confirm-before-record.md).
  This check makes the order "confirm → record" mechanical instead of
  relying on prose recall.

Invocation modes:
  (no args)   Scan all tracked experience leaves in the repo. Used by
              verify-all.py in its default mode.
  --staged    Check files staged for commit. Used by the pre-commit hook.
              Validates the staged blob (git show :path), not the working
              tree.
  --hook      PreToolUse mode. Reads the tool-input JSON on stdin and
              validates if it is a Write tool call targeting
              `**/experience/*.md`. Exit 2 (block + stderr to model)
              on violation. Beyond the frontmatter-presence check, this mode
              also validates the `resolution_confirmed_by_user` quote against
              the session transcript (`transcript_path` in the payload): a
              quote absent from every user message is treated as fabricated
              and blocked. When the transcript cannot be resolved the check
              degrades to advisory (warn, never block).
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

EXPERIENCE_PATH_RE = re.compile(r"(^|/)experience/(?!MEMORY\.md$)[^/]+\.md$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
FIELD_RE = re.compile(
    r"^resolution_confirmed_by_user\s*:\s*(.*?)\s*$", re.MULTILINE
)
SCHEMA_RE = re.compile(r"^schema\s*:\s*(.*?)\s*$", re.MULTILINE)
TICKET_RE = re.compile(r"^ticket\s*:\s*(.*?)\s*$", re.MULTILINE)

# Required `## ` sections for a standalone difficulty/v1 leaf. Each tuple is
# (canonical name, regex matching the heading). See
# memory-global/leaves/experience-leaf-schema.md.
V1_SECTIONS = [
    ("## Difficulty", re.compile(r"^##\s+Difficulty\b", re.MULTILINE)),
    ("## Order & criterion", re.compile(r"^##\s+Order\b", re.MULTILINE)),
    ("## Contexts", re.compile(r"^##\s+Contexts?\b", re.MULTILINE)),
    ("## Cost", re.compile(r"^##\s+Cost\b", re.MULTILINE)),
]


def is_experience_leaf(path: str) -> bool:
    return bool(EXPERIENCE_PATH_RE.search(path))


def _normalize(text: str) -> str:
    """Collapse whitespace + lowercase so quote matching tolerates reflowing."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _iter_user_text(transcript_path: str):
    """Yield user-authored prose from a session transcript JSONL.

    Only `type == "user"` records, and within them only string content or
    `text` blocks — tool_result blocks are the harness echoing tool output,
    not something the user typed, so they cannot stand in for a confirmation.
    """
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if rec.get("type") != "user":
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    yield block
                elif isinstance(block, dict) and block.get("type") == "text":
                    yield block.get("text", "")


def transcript_confirms(quote: str, transcript_path: str | None):
    """Validate the confirmation quote actually appears in the session transcript.

    Returns (status, detail):
      (True,  ...)  quote found in a user message (normalized substring)
      (False, ...)  transcript resolved but quote absent — likely fabricated
      (None,  ...)  transcript unresolvable — graceful degrade, advisory only

    The (None, ...) degrade is deliberate: outside `--hook` mode there is no
    session context, and even in-hook the path may be missing on older
    harnesses. We never block when we cannot actually look.
    """
    if not transcript_path:
        return None, "no transcript_path available"
    if not Path(transcript_path).exists():
        return None, f"transcript not found: {transcript_path}"
    needle = _normalize(quote)
    if not needle:
        return None, "empty confirmation quote after normalization"
    for text in _iter_user_text(transcript_path):
        if needle in _normalize(text):
            return True, "quote found in transcript"
    return False, "confirmation quote not present in any user message of the transcript"


def _fm_field(fm_body: str, regex: re.Pattern) -> str:
    m = regex.search(fm_body)
    return m.group(1).strip().strip("\"'") if m else ""


def check_content(content: str) -> str | None:
    """Return None if OK, otherwise a human-readable error."""
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return "no YAML frontmatter block at top of file"
    fm_body = fm.group(1)
    if not _fm_field(fm_body, FIELD_RE):
        return ("frontmatter missing/empty required field "
                "`resolution_confirmed_by_user`")

    # Schema-versioned leaves get the structural check; legacy leaves
    # (no `schema:` field) keep the confirmation-only check — grandfathered.
    schema = _fm_field(fm_body, SCHEMA_RE)
    if "difficulty/v1" not in schema:
        return None

    body = content[fm.end():]
    if _fm_field(fm_body, TICKET_RE):
        # Ticket-driven thin leaf: the record lives in the ticket. Require the
        # ticket id/url to appear in the body as the pointer; sections relaxed.
        ticket = _fm_field(fm_body, TICKET_RE)
        if ticket not in body:
            return (f"ticket leaf must reference its ticket ({ticket}) in the "
                    "body as a pointer to the full record")
        return None

    missing = [name for name, rx in V1_SECTIONS if not rx.search(body)]
    if missing:
        return ("schema:difficulty/v1 leaf missing required section(s): "
                + ", ".join(missing))
    return None


def _list_paths(mode: str) -> list[str]:
    if mode == "staged":
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    else:
        cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [
        line for line in out.stdout.splitlines()
        if line and is_experience_leaf(line)
    ]


def _read_blob(path: str, mode: str) -> tuple[str | None, str | None]:
    """Return (content, error). For --staged read git index; else read disk."""
    if mode == "staged":
        blob = subprocess.run(
            ["git", "show", f":{path}"],
            capture_output=True, text=True, check=False,
        )
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
        print(f"verify-experience-leaf: OK — no experience leaves ({mode} mode)")
        return 0
    failed: list[str] = []
    for path in paths:
        content, read_err = _read_blob(path, mode)
        if read_err is not None:
            print(f"verify-experience-leaf: FAIL {path}: {read_err}")
            failed.append(path)
            continue
        err = check_content(content or "")
        if err:
            print(f"verify-experience-leaf: FAIL {path}: {err}")
            failed.append(path)
        else:
            print(f"verify-experience-leaf: OK {path}")
    if failed:
        print(
            f"\n{len(failed)} experience leaf/leaves missing user-confirmation "
            f"frontmatter. Add\n"
            f"  resolution_confirmed_by_user: \"<user's literal confirmation quote>\"\n"
            f"to the frontmatter. The field exists because writing a leaf on\n"
            f"assumed resolution is a recurring failure mode."
        )
        return 1
    return 0


def cmd_hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"verify-experience-leaf: hook input not valid JSON: {e}", file=sys.stderr)
        return 0  # do not block on input parse error — fail open
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "") or ""
    if tool != "Write" or not is_experience_leaf(file_path):
        return 0
    content = tool_input.get("content", "") or ""
    err = check_content(content)
    if err is not None:
        print(
            "verify-experience-leaf: BLOCK\n"
            f"  Write target: {file_path}\n"
            f"  reason: {err}\n"
            "  rule: CLAUDE.md § On task resolution — experience leaves must include\n"
            "        resolution_confirmed_by_user: \"<user quote>\" in YAML frontmatter.\n"
            "  recovery: confirm resolution with the user, copy their literal\n"
            "            confirmation into the field, retry the Write.\n",
            file=sys.stderr,
        )
        return 2

    # Frontmatter is well-formed. Second teeth: the quote must actually be
    # something the user said in this session — a frontmatter field is trivial
    # to fabricate, the transcript is not. Graceful degrade when unresolvable.
    fm = FRONTMATTER_RE.match(content)
    quote = _fm_field(fm.group(1), FIELD_RE) if fm else ""
    status, detail = transcript_confirms(quote, payload.get("transcript_path"))
    if status is False:
        print(
            "verify-experience-leaf: BLOCK\n"
            f"  Write target: {file_path}\n"
            f"  reason: {detail}\n"
            f"  quote: {quote!r}\n"
            "  rule: CLAUDE.md § On task resolution — the confirmation quote must be\n"
            "        the user's literal words, present in the session transcript.\n"
            "  recovery: confirm resolution with the user, copy their actual\n"
            "            confirmation verbatim into the field, retry the Write.\n",
            file=sys.stderr,
        )
        return 2
    if status is None:
        print(
            f"verify-experience-leaf: ADVISORY — could not verify quote against "
            f"transcript ({detail}); allowing write.",
            file=sys.stderr,
        )
    return 0


def cmd_file(path_str: str) -> int:
    p = Path(path_str)
    if not is_experience_leaf(str(p)):
        print(f"verify-experience-leaf: SKIP {p} (not an experience leaf path)")
        return 0
    if not p.exists():
        print(f"verify-experience-leaf: FAIL {p}: file not found", file=sys.stderr)
        return 1
    err = check_content(p.read_text(encoding="utf-8"))
    if err:
        print(f"verify-experience-leaf: FAIL {p}: {err}")
        return 1
    print(f"verify-experience-leaf: OK {p}")
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
