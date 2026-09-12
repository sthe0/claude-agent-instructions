#!/usr/bin/env python3
"""Hard ceiling on instruction-file line counts.

Goal: prevent uncontrolled growth of the always-loaded policy surface
(`CLAUDE.md`, cursor mirror) and skill prompts. Limits live in `config.md`,
checked here. Above the limit → exit 1.

How to fix a FAIL: extract a section into `memory-global/leaves/<slug>.md`
(or a sibling `policy.md` for a skill body) and replace it in the parent
file with a one-line pointer.

Governed files (per `config.md` keys):

  CLAUDE.md                              claude-md-max-lines, claude-md-max-chars
  README.md                              readme-max-lines
  cursor/rules/claude-code-sync.mdc      cursor-mirror-max-lines
  skills/*/SKILL.md                      skill-md-max-lines
  skills/specializations/*/SKILL.md      skill-md-max-lines
  skills/*/policy.md                     policy-md-max-lines
  skills/specializations/*/policy.md     policy-md-max-lines
  memory-global/MEMORY.md                memory-index-max-bytes

`memory-global/MEMORY.md` is measured in UTF-8 BYTES, not lines and not chars:
the harness caps that file at 25000 bytes on the AutoMem context-assembly path
and truncates it SILENTLY, so a line-count check reports safety it cannot
establish (see the `memory-index-max-bytes` row in config.md).

CLAUDE.md also has a char ceiling (`claude-md-max-chars`), measured in UTF-16
code units — the unit the harness's own `content.length` check uses, not
UTF-8 bytes. Crossing it produces a display-only `/doctor` warning, not
truncation (verified against the installed client bundle and an over-limit
sentinel test; see the `claude-md-max-chars` row in config.md). The
line-count guard does not catch char growth (a few long lines can cross the
char budget while staying well under the line limit), so it is checked
explicitly.

Every `skills/*/SKILL.md` and `skills/specializations/*/SKILL.md` frontmatter
`description:` value is ALSO checked against `skill-description-max-chars` —
that description is always-visible index cost (loaded into every session's
skill list), unlike the skill body, which loads only on invocation.

At or above WARN_FRACTION of any ceiling a non-fatal WARN line is printed
(exit code unchanged): a limit that only signals at 100% is discovered as a
crisis; the early warning turns it into routine maintenance.

`--surface-report` prints a separate, report-only view: the aggregate
always-loaded surface (CLAUDE.md + config.md + memory-global/MEMORY.md + the
sum of all skill descriptions) against the ADVISORY (never FAIL)
`always-loaded-surface-advisory-chars` ceiling, plus a per-surface
breakdown and a PRICE block that converts the total into a measured
tokens-per-step cost against a live transcript window (see
always-loaded-surface-advisory-chars in config.md) — this block always reads
live session transcripts, independent of `--include-dynamic`. It never
changes the exit code and runs instead of (not alongside) the
governed-ceiling checks above. With `--include-dynamic` it additionally
reports OBSERVED UserPromptSubmit hook-injection volume from recent session
transcripts, labelled DYNAMIC — never summed into the static total; that
additional scan is the only transcript read gated by the flag.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_MD = REPO_ROOT / "config.md"

# The harness's OWN approximation for every current model (see
# claude-md-max-chars in config.md) — not a tokenizer result. Named in every
# PRICE line that uses it so the approximation is never mistaken for a
# measured token count.
CHARS_PER_TOKEN = 3

# The trailing window priced by default — matches the window the opening
# token-economy analysis used, so the PRICE block's share is comparable to it.
PRICE_WINDOW_DAYS = 14

# The unit the marginal price is quoted in. 1000 chars is roughly a paragraph of
# instruction prose — the size of an edit someone actually contemplates making,
# which is the decision this price exists to inform.
PRICE_MARGIN_CHARS = 1000

# (glob pattern relative to repo root, config-key for the limit).
GOVERNED = [
    ("CLAUDE.md", "claude-md-max-lines"),
    ("README.md", "readme-max-lines"),
    ("cursor/rules/claude-code-sync.mdc", "cursor-mirror-max-lines"),
    ("skills/*/SKILL.md", "skill-md-max-lines"),
    ("skills/specializations/*/SKILL.md", "skill-md-max-lines"),
    ("skills/*/policy.md", "policy-md-max-lines"),
    ("skills/specializations/*/policy.md", "policy-md-max-lines"),
]

# The always-visible frontmatter description of every skill (loaded into the
# skill index on every session, unlike the lazily-loaded body).
SKILL_DESCRIPTION_GLOBS = [
    "skills/*/SKILL.md",
    "skills/specializations/*/SKILL.md",
]

CONFIG_KEY_RE = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|")
FRONTMATTER_DESC_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)

# Fraction of a ceiling at which a non-fatal WARN is emitted.
WARN_FRACTION = 0.90


def check_level(value: int, limit: int) -> str:
    """Classify a measured value against its ceiling: 'ok' | 'warn' | 'fail'."""
    if value > limit:
        return "fail"
    if value >= limit * WARN_FRACTION:
        return "warn"
    return "ok"


def parse_config_md() -> dict[str, str]:
    constants: dict[str, str] = {}
    for line in CONFIG_MD.read_text(encoding="utf-8").splitlines():
        m = CONFIG_KEY_RE.match(line)
        if m:
            constants[m.group(1)] = m.group(2)
    return constants


def extract_frontmatter_description(path: Path) -> str | None:
    """The `description:` value from a file's `---`-delimited frontmatter, or
    None if there is no frontmatter or no description line — single-line
    values only (mirrors self-diagnose.py's own `_FM_DESC_RE`)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    m = FRONTMATTER_DESC_RE.search(text[:end])
    return m.group(1) if m else None


def _surface_files() -> list[tuple[str, Path]]:
    """(label, path) for the --surface-report breakdown — built from the
    current REPO_ROOT/CONFIG_MD globals at call time (not a module-level
    constant) so tests can point them at a throwaway tree."""
    return [
        ("CLAUDE.md", REPO_ROOT / "CLAUDE.md"),
        ("config.md", CONFIG_MD),
        ("memory-global/MEMORY.md", REPO_ROOT / "memory-global" / "MEMORY.md"),
    ]


def _iter_skill_descriptions() -> list[tuple[Path, str]]:
    """(path, description) for every skill whose frontmatter has one —
    shared by the enforced per-skill cap check and the --surface-report
    aggregate so the two never drift apart on which files count."""
    out: list[tuple[Path, str]] = []
    for glob_pat in SKILL_DESCRIPTION_GLOBS:
        for path in sorted(REPO_ROOT.glob(glob_pat)):
            if not path.is_file():
                continue
            desc = extract_frontmatter_description(path)
            if desc is not None:
                out.append((path, desc))
    return out


def _skill_description_total() -> tuple[int, int]:
    """(summed chars, file count) over every skill's frontmatter description."""
    descriptions = _iter_skill_descriptions()
    return sum(len(desc) for _, desc in descriptions), len(descriptions)


def _load_config_root():
    """Import lib/config_root.py lazily, and tolerate its absence.

    This script is deliberately COPYABLE on its own: hook-instruction-grooming-
    due.py runs `<repo>/scripts/lint-prose-length.py` out of whatever tree
    CLAUDE_INSTRUCTIONS_REPO names, and that tree need not carry a `lib/`
    sibling. A module-level `from lib import config_root` therefore turns a
    missing sibling into an import error for EVERY governed-file check, not just
    the one dynamic scan that needs it.
    """
    path = Path(__file__).resolve().parent / "lib" / "config_root.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("lint_prose_length_config_root", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_cost_report():
    """Import scripts/cost-report.py by path (repo idiom: self-diagnose.py's
    own _load_lint_prose_length) — reuses its JSONL iterator instead of
    re-deriving it here. The transcript ROOTS come from lib/config_root, not
    from this module."""
    path = REPO_ROOT / "scripts" / "cost-report.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("lint_prose_length_cost_report", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scan_dynamic_injection(max_sessions: int = 20) -> dict[str, int] | None:
    """OBSERVED UserPromptSubmit hook-injection volume over the most recent
    session transcripts — DYNAMIC data, never summed into the static
    surface total. Returns None if no transcripts are found at all (fresh
    machine / no history); a dict with n_events == 0 if sessions were
    scanned but none carried a UserPromptSubmit injection."""
    cost_report = _load_cost_report()
    config_root = _load_config_root()
    if cost_report is None or config_root is None:
        return None
    files = sorted(
        config_root.iter_transcripts(),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_sessions]
    if not files:
        return None
    sizes: list[int] = []
    for path in files:
        for d in cost_report._iter_jsonl(path):
            if d.get("type") != "attachment":
                continue
            att = d.get("attachment")
            if not isinstance(att, dict) or att.get("hookEvent") != "UserPromptSubmit":
                continue
            content = att.get("content")
            if isinstance(content, str) and content:
                sizes.append(len(content))
    if not sizes:
        return {"n_events": 0, "n_sessions": len(files), "mean": 0, "max": 0}
    return {
        "n_events": len(sizes),
        "n_sessions": len(files),
        "mean": sum(sizes) // len(sizes),
        "max": max(sizes),
    }


def surface_breakdown() -> tuple[list[tuple[str, int]], int]:
    """((label, chars) rows, summed total) for the always-loaded surface.

    The single source of the surface number: `cmd_surface_report` renders what
    this returns, and out-of-tree consumers (rule-salience-report.py's Phase-3
    pressure arm) read it instead of re-deriving or scraping stdout. Dynamic
    hook-injection volume is deliberately not part of it — it is never summed
    into the static total."""
    rows: list[tuple[str, int]] = []
    for label, path in _surface_files():
        n = len(path.read_text(encoding="utf-8")) if path.is_file() else 0
        rows.append((label, n))

    skill_total, skill_count = _skill_description_total()
    rows.append((f"skill descriptions ({skill_count} skills)", skill_total))

    return rows, sum(n for _, n in rows)


def _parse_iso_timestamp(raw: str) -> datetime.datetime | None:
    """Best-effort ISO-8601 parse of a transcript row's timestamp, tolerant of
    the trailing 'Z' Python's fromisoformat rejects on older interpreters."""
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def price_window_stats(n_days: int = PRICE_WINDOW_DAYS) -> dict[str, int] | None:
    """(n_days, n_steps, total_tokens) aggregated from live session
    transcripts over the trailing n_days — the measured window the PRICE
    block prices the surface against. Reuses the same loader idiom as
    scan_dynamic_injection() (_load_cost_report / _load_config_root /
    cost_report._iter_jsonl) rather than a second transcript-reading path.

    One API response emits several transcript rows sharing an identical
    `message.id` and `usage` object, so rows are deduplicated by that id —
    a naive per-row sum roughly doubles every count. A step's token size is
    input_tokens + cache_read_input_tokens + cache_creation_input_tokens
    (all three cost quota tokens on the input side; output_tokens is
    excluded because the surface priced here rides the INPUT context, not
    generation).

    Returns None if no transcript data is found at all (fresh machine / no
    history within the window) — never a fabricated zero.
    """
    cost_report = _load_cost_report()
    config_root = _load_config_root()
    if cost_report is None or config_root is None:
        return None
    files = config_root.iter_transcripts()
    if not files:
        return None

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=n_days)
    seen_ids: set[str] = set()
    total_tokens = 0
    n_steps = 0
    found_any = False
    for path in files:
        for d in cost_report._iter_jsonl(path):
            if d.get("type") != "assistant":
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not usage:
                continue
            ts_raw = d.get("timestamp") or msg.get("ts")
            ts = _parse_iso_timestamp(ts_raw) if isinstance(ts_raw, str) else None
            if ts is None or ts < cutoff:
                continue
            found_any = True
            msg_id = msg.get("id")
            if msg_id:
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
            total_tokens += (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
            )
            n_steps += 1
    if not found_any:
        return None
    return {"n_days": n_days, "n_steps": n_steps, "total_tokens": total_tokens}


def compute_price(
    surface_chars: int,
    n_days: int,
    n_steps: int,
    total_tokens: int,
    chars_per_token: int = CHARS_PER_TOKEN,
    margin_chars: int = PRICE_MARGIN_CHARS,
) -> dict[str, float]:
    """Pure arithmetic over an already-measured window — split out from
    price_window_stats() so the price derivation is unit-testable without
    transcripts. total_tokens == 0 degrades to 0% shares rather than raising,
    since a genuinely empty (but non-None) window is a valid input."""
    surface_tokens = surface_chars / chars_per_token
    tokens_per_step = surface_tokens
    share_pct = (tokens_per_step * n_steps / total_tokens * 100) if total_tokens else 0.0

    margin_tokens_per_step = margin_chars / chars_per_token
    margin_share_pct = (
        (margin_tokens_per_step * n_steps / total_tokens * 100) if total_tokens else 0.0
    )
    return {
        "surface_tokens": surface_tokens,
        "tokens_per_step": tokens_per_step,
        "share_pct": share_pct,
        "margin_chars": margin_chars,
        "margin_tokens_per_step": margin_tokens_per_step,
        "margin_share_pct": margin_share_pct,
    }


def cmd_surface_report(constants: dict[str, str], include_dynamic: bool) -> int:
    """Report-only: the aggregate always-loaded surface plus its breakdown.
    Never fails — the aggregate is disclosed, not enforced (see
    always-loaded-surface-advisory-chars in config.md)."""
    breakdown, total = surface_breakdown()

    print("lint-prose-length: always-loaded-surface-report")
    for label, n in breakdown:
        print(f"  {label}: {n} chars")
    print(f"  TOTAL: {total} chars")

    price_window = price_window_stats()
    if price_window is None:
        print("  PRICE: no transcript data — cannot price")
    else:
        price = compute_price(
            total,
            price_window["n_days"],
            price_window["n_steps"],
            price_window["total_tokens"],
        )
        print(
            "  PRICE (Core-repo surface only, the scope surface_breakdown() "
            "measures — the live session surface is larger, e.g. it also "
            "carries a project's own CLAUDE.md and memory index):"
        )
        print(
            f"    window: {price_window['n_days']} days, "
            f"{price_window['n_steps']} steps, "
            f"{price_window['total_tokens']} tokens (measured from live "
            "session transcripts)"
        )
        print(
            f"    surface: {total} chars / charsPerToken={CHARS_PER_TOKEN} "
            "(the harness's own approximation for every current model, not "
            f"a tokenizer result) = {price['surface_tokens']:.0f} tokens"
        )
        print(
            f"    cost: {price['tokens_per_step']:.0f} tokens per step (it "
            f"rides every step) = {price['share_pct']:.4f}% of the measured "
            f"{price_window['n_days']}-day window — denominated in tokens, "
            "not dollars: transcripts carry no per-class cost field (only "
            "token counts), and under flat billing quota tokens, not "
            "dollars, are the scarce resource; cache-write, cache-read and "
            "fresh-input tokens are weighted equally here, a deliberate "
            "simplification whose known cost is that it under-states "
            "cache-write pressure (cache-write list-prices higher per "
            "token than fresh input or cache-read)"
        )
        print(
            f"    marginal price per {price['margin_chars']} chars: "
            f"{price['margin_tokens_per_step']:.0f} tokens per step = "
            f"{price['margin_share_pct']:.4f}% of the measured "
            f"{price_window['n_days']}-day window"
        )

    advisory_raw = constants.get("always-loaded-surface-advisory-chars")
    if advisory_raw is not None:
        try:
            advisory = int(advisory_raw)
        except ValueError:
            advisory = 0
        if advisory:
            level = check_level(total, advisory)
            if level in ("warn", "fail"):
                print(
                    f"  ADVISORY: {total} chars is {total * 100 // advisory}% of "
                    f"always-loaded-surface-advisory-chars ({advisory}) — "
                    "advisory only, does not fail"
                )

    if include_dynamic:
        dyn = scan_dynamic_injection()
        if dyn is None or dyn["n_events"] == 0:
            print("  DYNAMIC (OBSERVED UserPromptSubmit injection): no transcript data found")
        else:
            print(
                f"  DYNAMIC (OBSERVED UserPromptSubmit injection): mean {dyn['mean']} chars, "
                f"max {dyn['max']} chars, over {dyn['n_events']} firing(s) across "
                f"{dyn['n_sessions']} session(s) scanned — NOT summed into TOTAL"
            )

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--staged",
        action="store_true",
        help="(accepted for verify-all parity; this lint always reads from disk)",
    )
    parser.add_argument(
        "--surface-report",
        action="store_true",
        help="report-only: print the aggregate always-loaded surface and a "
        "per-surface breakdown; never changes the exit code",
    )
    parser.add_argument(
        "--include-dynamic",
        action="store_true",
        help="with --surface-report, also print OBSERVED UserPromptSubmit "
        "hook-injection volume from recent transcripts (DYNAMIC, not summed "
        "into the static total); without --surface-report this is ignored",
    )
    args = parser.parse_args(argv)

    constants = parse_config_md()

    if args.surface_report:
        return cmd_surface_report(constants, include_dynamic=args.include_dynamic)

    failures: list[str] = []
    warnings: list[str] = []
    scanned = 0

    # Char-size ceiling for CLAUDE.md, measured in UTF-16 code units — the unit
    # the harness's own content.length check uses. The line-count guard above
    # does not catch char growth (a few long lines can cross the char budget
    # while staying well under the line limit), so check chars explicitly.
    char_key = "claude-md-max-chars"
    raw_chars = constants.get(char_key)
    if raw_chars is None:
        failures.append(f"config.md missing key: {char_key}")
    else:
        try:
            char_limit = int(raw_chars)
        except ValueError:
            failures.append(f"config.md key {char_key} is not an integer: {raw_chars!r}")
        else:
            claude_md = REPO_ROOT / "CLAUDE.md"
            if claude_md.is_file():
                scanned += 1
                nchars = len(claude_md.read_text(encoding="utf-8"))
                level = check_level(nchars, char_limit)
                if level == "fail":
                    failures.append(
                        f"CLAUDE.md: {nchars} chars, limit {char_limit} ({char_key})"
                    )
                elif level == "warn":
                    warnings.append(
                        f"CLAUDE.md: {nchars} chars, {nchars * 100 // char_limit}% "
                        f"of limit {char_limit} ({char_key})"
                    )

    # Byte-size ceiling for the global memory index, measured in UTF-8 BYTES —
    # the axis the harness's AutoMem cap truncates on, silently. It is NOT a
    # GOVERNED entry: that table's loop measures line counts, and lines carry no
    # information about this cap (on 2026-08-31 the file stood at 104 of 200
    # lines while already over the byte cap, its last two pointers dropped).
    byte_key = "memory-index-max-bytes"
    raw_bytes = constants.get(byte_key)
    if raw_bytes is None:
        failures.append(f"config.md missing key: {byte_key}")
    else:
        try:
            byte_limit = int(raw_bytes)
        except ValueError:
            failures.append(f"config.md key {byte_key} is not an integer: {raw_bytes!r}")
        else:
            memory_index = REPO_ROOT / "memory-global" / "MEMORY.md"
            if memory_index.is_file():
                scanned += 1
                nbytes = len(memory_index.read_bytes())
                level = check_level(nbytes, byte_limit)
                if level == "fail":
                    failures.append(
                        f"memory-global/MEMORY.md: {nbytes} bytes, "
                        f"limit {byte_limit} ({byte_key})"
                    )
                elif level == "warn":
                    warnings.append(
                        f"memory-global/MEMORY.md: {nbytes} bytes, "
                        f"{nbytes * 100 // byte_limit}% of limit {byte_limit} ({byte_key})"
                    )

    # Per-skill frontmatter description ceiling — always-visible index cost
    # (loaded into every session's skill list), unlike the skill body, which
    # loads only on invocation.
    desc_key = "skill-description-max-chars"
    raw_desc_limit = constants.get(desc_key)
    if raw_desc_limit is None:
        failures.append(f"config.md missing key: {desc_key}")
    else:
        try:
            desc_limit = int(raw_desc_limit)
        except ValueError:
            failures.append(f"config.md key {desc_key} is not an integer: {raw_desc_limit!r}")
        else:
            for path, desc in _iter_skill_descriptions():
                scanned += 1
                n = len(desc)
                rel = path.relative_to(REPO_ROOT)
                level = check_level(n, desc_limit)
                if level == "fail":
                    failures.append(
                        f"{rel}: {n} chars description, limit {desc_limit} ({desc_key})"
                    )
                elif level == "warn":
                    warnings.append(
                        f"{rel}: {n} chars description, {n * 100 // desc_limit}% "
                        f"of limit {desc_limit} ({desc_key})"
                    )

    for glob_pat, key in GOVERNED:
        raw = constants.get(key)
        if raw is None:
            failures.append(f"config.md missing key: {key}")
            continue
        try:
            limit = int(raw)
        except ValueError:
            failures.append(f"config.md key {key} is not an integer: {raw!r}")
            continue
        for path in sorted(REPO_ROOT.glob(glob_pat)):
            if not path.is_file():
                continue
            scanned += 1
            n = len(path.read_text(encoding="utf-8").splitlines())
            rel = path.relative_to(REPO_ROOT)
            level = check_level(n, limit)
            if level == "fail":
                failures.append(f"{rel}: {n} lines, limit {limit} ({key})")
            elif level == "warn":
                warnings.append(
                    f"{rel}: {n} lines, {n * 100 // limit}% of limit {limit} ({key})"
                )

    for w in warnings:
        print(f"lint-prose-length: WARN — {w}")

    if failures:
        print(f"lint-prose-length: FAIL — {len(failures)} issue(s)")
        for f in failures:
            print(f"  {f}")
        print()
        print(
            "To fix: extract a section to memory-global/leaves/<slug>.md "
            "(or a sibling policy.md for a skill body) and replace it with "
            "a one-line pointer in the parent file."
        )
        return 1

    print(f"lint-prose-length: OK — {scanned} file(s) within ceilings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
