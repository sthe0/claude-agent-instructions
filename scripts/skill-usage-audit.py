#!/usr/bin/env python3
"""Audit: which user-invocable skills are actually being used.

Scans recent session transcripts (under `~/.claude/projects/<hash>/*.jsonl`)
and counts:
  - Skills explicitly invoked via the `Skill` tool.
  - Skills mentioned by name in any `tool_use.input` (catches Bash invocations
    of scripts/CLIs whose binaries match a skill slug, e.g. `tracker-cli.sh`).

Cross-checks the count against the skill catalog visible at session start —
the list of skill slugs is parsed from each transcript's initial system
reminder ("The following skills are available for use with the Skill tool").

Output: a markdown table sorted by invocations, with a `recommendation`
column suggesting `keep` / `review` / `consider removing` based on usage in
the audit window. The actual removal decision stays with the user — this
script is informational, see `skill-catalog-curation.md`.

Default window: last 30 days. Override with `--days N` or `--since YYYY-MM-DD`.
Default scope: every project under `~/.claude/projects/`. Override with
`--cwd <abs-path>` to limit to one project.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
# Two layouts seen in jsonl transcripts:
#   1. attachment of type "skill_listing" carrying the bullet list as a string.
#   2. Inline system-reminder text starting with "The following skills are available...".
CATALOG_ATTACHMENT_TYPE = "skill_listing"
CATALOG_HEADER = "The following skills are available for use with the Skill tool"
SKILL_LINE_RE = re.compile(r"^- ([a-z][a-z0-9:_-]+):", re.MULTILINE)

# A skill is "used" when:
#   1. Skill tool was called with name == slug.
#   2. The slug appears as a token in any tool_use input (best-effort, only
#      checks whitespace-separated tokens with the exact slug or `slug-cli`).
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]+")


def parse_ts(s: str) -> dt.datetime | None:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def iter_messages(jsonl: Path):
    try:
        with jsonl.open() as f:
            for line in f:
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except OSError:
        return


def extract_skills_from_catalog(text: str) -> set[str]:
    """Find skill slugs in a 'skills are available' system reminder block."""
    if CATALOG_HEADER not in text:
        return set()
    block = text[text.index(CATALOG_HEADER):]
    return set(SKILL_LINE_RE.findall(block))


def scan_session(jsonl: Path, cutoff: dt.datetime | None):
    """Return (catalog_slugs, skill_use_counts, mention_counts) for one session.

    Catalog discovery scans the raw jsonl text (cheap; the sentinel header is
    rare so the substring search is fast). Skill-tool invocations and other
    mentions are discovered by iterating typed messages.
    """
    catalog: set[str] = set()
    use_counts: dict[str, int] = defaultdict(int)
    mention_counts: dict[str, int] = defaultdict(int)
    in_window = False

    # Catalog discovery: prefer the skill_listing attachment; fall back to
    # in-message system reminder text.
    for m in iter_messages(jsonl):
        att = m.get("attachment")
        if isinstance(att, dict) and att.get("type") == CATALOG_ATTACHMENT_TYPE:
            content = att.get("content") or ""
            if isinstance(content, str):
                catalog |= set(SKILL_LINE_RE.findall(content))
        msg = m.get("message", {}) if isinstance(m.get("message"), dict) else {}
        c = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(c, str) and CATALOG_HEADER in c:
            catalog |= extract_skills_from_catalog(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = part.get("text", "")
                    if CATALOG_HEADER in txt:
                        catalog |= extract_skills_from_catalog(txt)

    for m in iter_messages(jsonl):
        ts = parse_ts(m.get("timestamp") or "")
        if ts and cutoff and ts < cutoff:
            continue
        in_window = True
        msg = m.get("message", {}) if isinstance(m.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            name = c.get("name", "")
            inp = c.get("input") or {}
            if name == "Skill":
                slug = inp.get("skill") or ""
                if slug:
                    use_counts[slug] += 1
            # Mention scan over all tool inputs.
            tokens = set(TOKEN_RE.findall(json.dumps(inp)))
            for slug in catalog:
                if slug in tokens or f"{slug}-cli" in tokens:
                    mention_counts[slug] += 1

    if not in_window:
        return None, {}, {}
    return catalog, use_counts, mention_counts


def find_transcripts(cwd: str | None) -> list[Path]:
    if cwd:
        sanitized = cwd.replace("/", "-")
        base = PROJECTS_ROOT / sanitized
        return sorted(base.glob("*.jsonl"))
    return sorted(PROJECTS_ROOT.glob("*/*.jsonl"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="Window size in days (default 30)")
    ap.add_argument("--since", help="Window start (YYYY-MM-DD), overrides --days")
    ap.add_argument("--cwd", help="Limit to one project cwd (abs path)")
    ap.add_argument("--top", type=int, default=0, help="Show only top N rows (0 = all)")
    args = ap.parse_args()

    cutoff: dt.datetime | None
    if args.since:
        cutoff = dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
    elif args.days:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    else:
        cutoff = None

    transcripts = find_transcripts(args.cwd)
    if not transcripts:
        print("No transcripts found.", file=sys.stderr)
        return 1

    full_catalog: set[str] = set()
    total_uses: dict[str, int] = defaultdict(int)
    total_mentions: dict[str, int] = defaultdict(int)
    sessions_scanned = 0

    for jsonl in transcripts:
        catalog, uses, mentions = scan_session(jsonl, cutoff)
        if catalog is None:
            continue
        sessions_scanned += 1
        full_catalog |= catalog
        for k, v in uses.items():
            total_uses[k] += v
        for k, v in mentions.items():
            total_mentions[k] += v

    def recommendation(slug: str) -> str:
        u = total_uses.get(slug, 0)
        mm = total_mentions.get(slug, 0)
        if u >= 1:
            return "keep"
        if mm >= 3:
            return "review (mentioned but not invoked)"
        return "consider removing from catalog"

    rows = []
    for slug in sorted(full_catalog):
        rows.append(
            (
                slug,
                total_uses.get(slug, 0),
                total_mentions.get(slug, 0),
                recommendation(slug),
            )
        )
    rows.sort(key=lambda r: (-r[1], -r[2], r[0]))
    if args.top > 0:
        rows = rows[: args.top]

    window_desc = (
        f"since {cutoff.date().isoformat()}" if cutoff else "all time"
    )
    print(f"# Skill usage audit ({window_desc}, {sessions_scanned} sessions)\n")
    print("| skill | Skill-invocations | other-mentions | recommendation |")
    print("|---|---:|---:|---|")
    for slug, u, m, rec in rows:
        print(f"| `{slug}` | {u} | {m} | {rec} |")

    return 0


if __name__ == "__main__":
    sys.exit(main())
