#!/usr/bin/env python3
"""Mechanically enumerate observable self-friction difficulties.

Difficulty removed: proactive self-diagnosis (CLAUDE.md § When the work is
stuck) has a DECIDABLE rule part — which memory/instruction signals count as
self-friction, at what threshold — that used to live only as a forgettable
"notice when..." directive. This script determinizes that rule part; the
PERCEPTION part (is a flagged item worth re-norming, and how) stays the
model's, surfaced as a normal difficulty declaration once this scanner's
worklist reaches a session (see plan-activity-ontology.md —
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
  oversized-leaf        a difficulty/v1 experience leaf past a byte cap or a
                        context-count cap, both derived from the corpus's own
                        log-scale far-out fence (docs/operations/
                        memory-leaf-size-calibration.json); the action it names
                        is the curator split (memory mode).
  crutch-defer-overdue  a gen_crutch_registry.py CLAUDE_MD_OVERRIDES entry
                        disposed "defer" that has gone unreviewed past a
                        horizon, keyed to that entry's own deferred_since date
                        (an authored literal), computed live.
  crutch-registry-drift  the count of semantic-unguarded hard sinks
                        verify-semantic-gates.py finds on the live tree has
                        risen since the last scan (tracked in a small history
                        file).
  writer-gate-unresolved
                        the published-text writer gate failed open on a
                        publication whose body it could not resolve, within a
                        recent window. The gate records each such fall-through
                        to its own advisory sink (published_body.record_advisory)
                        and then allows -- correctly, since a missing observable
                        must not wedge a turn -- but nothing read that sink, so
                        every fail-open was invisible by construction: a route
                        the gate cannot resolve went on passing unresolved
                        publications for as long as nobody happened to look.
                        Surfacing it here is what turns the sink from an audit
                        trail into a signal.
  resource-estimate-drift
                        one executor kind whose resource request (the
                        RESOURCE-REQUEST consult's rows) left out writes its
                        children then made, in at least
                        `resource-estimate-drift-repeat` different tasks (config
                        key). The engine records each stage's request-vs-fact
                        comparison in ~/.local/log/claude-resource-estimation.jsonl;
                        the finding names the missing resource classes and the
                        kind's SKILL.md § Resource request as the place to fix.

The DECIDABLE rule is mechanized here; the PERCEPTION (is a flagged candidate
worth re-norming, and how) stays the model's — this scanner only detects and
lists, it never merges, moves, or edits.

Never edits repo or memory content. `main()` prints the worklist and exits 1
if non-empty (0 if clean) — for use as a scriptable check; the SessionStart
hook that drives this at session start is always fail-open regardless of this
exit code. One narrow exception: scan_crutch_regressions persists a small
trend counter under ~/.local/state (external runtime bookkeeping, not repo or
memory content) — see that function's own docstring.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

sys.path.insert(0, str(SCRIPT_DIR))
from lib import config_root, resource_estimation_ledger  # noqa: E402
from lib.hook_script import hook_script_path  # noqa: E402

# Mirrors memory-global/MEMORY.md's own "keep this index under ~200 lines" note
# (CONFIRMED real cap, verified 2026-07-23 — see that file's line 5).
MEMORY_INDEX_LINE_THRESHOLD = 200

# Heuristic default: two leaves whose frontmatter name+description token sets
# overlap at or above this Jaccard similarity are flagged as near-duplicates
# ("generalize and group" candidates). Tunable via --near-dup-threshold.
NEAR_DUPLICATE_JACCARD_THRESHOLD = 0.6

# Window for the writer-gate fail-open scan. Seven days is one working week: long
# enough that a single unresolved publication is still on screen at the next
# session, short enough that a route fixed last month stops being reported.
WRITER_GATE_ADVISORY_WINDOW_DAYS = 7

# Fallbacks only for a config.md that lacks the rows; the live values are read
# from config.md and were derived by scripts/memory-leaf-size-calibrate.py.
OVERSIZED_LEAF_DEFAULT_MAX_BYTES = 74000
OVERSIZED_LEAF_DEFAULT_CONTEXTS_MULTIPLE = 27
PRINCIPLE_PROMOTION_DEFAULT = 3
OVERSIZED_LEAF_ACTION = "action: curator split"

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


def measure_leaf(text: str) -> "tuple[int, int]":
    """(UTF-8 bytes, count of `### ` lines inside the `## Contexts` section).

    Fenced code blocks are skipped, and a heading such as `## Contexts (12)` still opens the section.
    """
    contexts = 0
    in_contexts = False
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("## "):
            in_contexts = re.fullmatch(r"## Contexts(\s.*)?", line.strip()) is not None
        elif in_contexts and line.startswith("### "):
            contexts += 1
    return len(text.encode("utf-8")), contexts


def is_difficulty_leaf(text: str) -> bool:
    """True when the leaf's own frontmatter declares `schema: difficulty/v1` (quotes, comment, BOM tolerated)."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        key, _, value = line.partition(":")
        if key.strip() == "schema":
            return value.split("#", 1)[0].strip().strip("\"'") == "difficulty/v1"
    return False


def parse_config_int(config_text: str, key: str) -> "int | None":
    """Value of the first digit cell in the config row whose KEY cell is `key`, else None."""
    for line in config_text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) > 3 and line.lstrip().startswith("|") and cells[1].strip("`") == key:
            for cell in cells[2:]:
                if cell.strip("`").isdigit():
                    return int(cell.strip("`"))
    return None


def read_config_int(key: str, default: int, config_path: "Path | None" = None) -> int:
    path = config_path if config_path is not None else REPO_ROOT / "config.md"
    try:
        value = parse_config_int(path.read_text(encoding="utf-8"), key)
    except OSError:
        value = None
    return default if value is None else value


def scan_oversized_leaves(
    memory_root: Path,
    max_bytes: "int | None" = None,
    max_contexts: "int | None" = None,
) -> "list[Difficulty]":
    if max_bytes is None:
        max_bytes = read_config_int("memory-leaf-max-bytes", OVERSIZED_LEAF_DEFAULT_MAX_BYTES)
    if max_contexts is None:
        max_contexts = read_config_int(
            "memory-leaf-max-contexts-multiple", OVERSIZED_LEAF_DEFAULT_CONTEXTS_MULTIPLE
        ) * read_config_int("principle-promotion-threshold", PRINCIPLE_PROMOTION_DEFAULT)
    out: "list[Difficulty]" = []
    leaves = memory_root / "leaves"
    if not leaves.is_dir():
        return out
    for md in sorted(leaves.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not is_difficulty_leaf(text):
            continue
        size, contexts = measure_leaf(text)
        if size > max_bytes or contexts > max_contexts:
            out.append(
                Difficulty(
                    "oversized-leaf",
                    str(md.relative_to(memory_root)),
                    f"{size} bytes (cap {max_bytes}), {contexts} contexts (cap {max_contexts}); "
                    + OVERSIZED_LEAF_ACTION,
                )
            )
    return out


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
                    script = hook_script_path(hk.get("command", ""))
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
    """User + project + agent-home settings.json / settings.local.json that
    exist on disk, de-duplicated by resolved path (a project symlink to a
    shared file scanned once). $CLAUDE_PROJECT_DIR (else cwd) supplies the
    project location; config_root.agent_home() supplies the agent-home
    location — all generic Claude Code settings homes, not arc/composed-dir
    specifics."""
    home = Path.home()
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    proj_dir = Path(proj) if proj else Path.cwd()
    agent_home = config_root.agent_home()
    candidates = [
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        proj_dir / ".claude" / "settings.json",
        proj_dir / ".claude" / "settings.local.json",
        agent_home / "settings.json",
        agent_home / "settings.local.json",
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


# Each "defer" entry carries its own deferred_since (an authored-literal
# YYYY-MM-DD date in gen_crutch_registry.py's CLAUDE_MD_OVERRIDES) so the
# horizon below is keyed per-entry, not to any file's mtime.
CRUTCH_DEFER_HORIZON_DAYS = 90
CRUTCH_REGISTRY_HISTORY_PATH = Path.home() / ".local" / "state" / "claude-crutch-registry-history.json"


def _load_verify_semantic_gates(repo_root: Path):
    """Fail-open: a syntax/import error in verify-semantic-gates.py must not
    propagate through scan() and take down every other scanner's findings
    for the session — this module's own "never re-derive, always fail-open"
    norm (see scan_crutch_regressions' docstring) applies to the load step
    too, not only to the calls that follow it."""
    path = repo_root / "scripts" / "verify-semantic-gates.py"
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("self_diagnose_verify_semantic_gates", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def _parse_deferred_since(value) -> "float | None":
    """Parse a registry entry's deferred_since (YYYY-MM-DD, an authored
    literal — see gen_crutch_registry.py's CLAUDE_MD_OVERRIDES) into a Unix
    epoch. Returns None on anything missing/unparsable so a malformed or
    absent date fails open (no finding) rather than crashing the scanner."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%d"))
    except ValueError:
        return None


def _load_claude_md_deferrals(repo_root: Path) -> "dict[str, dict]":
    """The "defer" entries of gen_crutch_registry.py's CLAUDE_MD_OVERRIDES,
    keyed by a short hash of the sentence. Fail-open: a missing or broken
    generator yields {}, never an exception."""
    path = repo_root / "scripts" / "gen_crutch_registry.py"
    if not path.is_file():
        return {}
    try:
        scripts_dir = str(path.parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        spec = importlib.util.spec_from_file_location("self_diagnose_gen_crutch_registry", path)
        if spec is None or spec.loader is None:
            return {}
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        deferrals = {}
        for sentence, override in mod.CLAUDE_MD_OVERRIDES.items():
            if override[1] != "defer":
                continue
            deferrals[hashlib.sha256(sentence.encode("utf-8")).hexdigest()[:16]] = {
                "file": "CLAUDE.md",
                "ground": override[2],
                "deferred_since": override[4] if len(override) == 5 else None,
            }
        return deferrals
    except Exception:
        return {}


def scan_crutch_regressions(
    repo_root: Path,
    history_path: "Path | None" = None,
    defer_horizon_days: int = CRUTCH_DEFER_HORIZON_DAYS,
) -> "list[Difficulty]":
    """Advisory-only: crutch deferrals past a review horizon, and a rising
    count of verify-semantic-gates.py's semantic-unguarded hard sinks.

    Computed from the live tree, nothing stored: the deferrals are
    gen_crutch_registry.py's CLAUDE_MD_OVERRIDES entries disposed "defer",
    each keyed to its own deferred_since; the sink count is verify-semantic-
    gates.py's find_unguarded_hard_sinks (never re-deriving its AST logic
    here), tracked in a small persisted count. A scripts/crutch_registry.toml
    left in the checkout is never read.

    Deliberate, narrow exception to this module's "never edits anything"
    invariant (see module docstring): history_path is external runtime
    bookkeeping (a trend counter under ~/.local/state), not repo or memory
    content, so persisting it does not touch anything else this scanner is
    meant to leave untouched. Fail-open: any I/O/parse/import error yields [].
    """
    out: "list[Difficulty]" = []
    vsg = _load_verify_semantic_gates(repo_root)
    if vsg is None:
        return out

    now = time.time()
    for eid, entry in sorted(_load_claude_md_deferrals(repo_root).items()):
        since_epoch = _parse_deferred_since(entry.get("deferred_since"))
        if since_epoch is None:
            continue
        age_days = (now - since_epoch) / 86400
        if age_days < defer_horizon_days:
            continue
        ground = str(entry.get("ground", ""))[:120]
        out.append(
            Difficulty(
                "crutch-defer-overdue",
                f"CLAUDE.md#{eid}",
                f"deferred entry ({entry.get('file', '?')}) unreviewed for >= {defer_horizon_days}d "
                f"(deferred_since {entry.get('deferred_since')}, {age_days:.0f}d ago); ground: {ground}",
            )
        )

    try:
        inv = vsg._load_inventory_module()
        unguarded, _all_code = vsg.find_unguarded_hard_sinks(inv, repo_root / "scripts")
        missing = unguarded
    except Exception:
        return out

    hpath = history_path if history_path is not None else CRUTCH_REGISTRY_HISTORY_PATH
    prev_count = None
    try:
        if hpath.is_file():
            prev_count = json.loads(hpath.read_text(encoding="utf-8")).get("missing_count")
    except (OSError, json.JSONDecodeError, AttributeError):
        prev_count = None

    current_count = len(missing)
    if isinstance(prev_count, int) and current_count > prev_count:
        out.append(
            Difficulty(
                "crutch-registry-drift",
                "scripts/verify-semantic-gates.py",
                f"semantic-unguarded hard sinks rose from {prev_count} to {current_count}",
            )
        )

    try:
        hpath.parent.mkdir(parents=True, exist_ok=True)
        hpath.write_text(json.dumps({"missing_count": current_count}), encoding="utf-8")
    except OSError:
        pass

    return out


def writer_gate_advisory_sink() -> Path:
    """The published-text gate's own advisory sink, resolved by the gate's own
    `advisory_sink()` rather than recomputed here -- so a rename, a relocation,
    or the test-isolation env override cannot leave this scanner reading a path
    nothing writes, or the real sink while the writer is redirected elsewhere."""
    sys.path.insert(0, str(SCRIPT_DIR))
    from lib import published_body  # noqa: PLC0415 -- lazy: only this scan needs it

    return published_body.advisory_sink()


def scan_writer_gate_advisories(
    sink: "Path | None" = None, window_days: int = WRITER_GATE_ADVISORY_WINDOW_DAYS
) -> "list[Difficulty]":
    """Report the published-text writer gate's recent fail-opens.

    Read-only and fail-open itself: an unreadable sink, an unparsable line, or a
    timestamp in a shape this cannot read is skipped, never raised -- a scanner
    that crashes on its own diagnostics file is worse than one that reports
    nothing. Groups by the gate's `shape` field so the finding names WHICH body
    shape went unresolved, which is the part that tells a reader whether to
    register a route, widen a resolver, or accept the fall-through."""
    try:
        sink = sink if sink is not None else writer_gate_advisory_sink()
    except Exception:
        return []
    try:
        lines = sink.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    cutoff = time.time() - window_days * 86400
    by_shape: "dict[str, int]" = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("kind") != "UNRESOLVED":
            continue
        stamp = record.get("timestamp")
        if not isinstance(stamp, str):
            continue
        try:
            when = datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            continue
        if when < cutoff:
            continue
        shape = record.get("shape")
        key = "unknown" if shape is None else str(shape)
        by_shape[key] = by_shape.get(key, 0) + 1

    if not by_shape:
        return []
    total = sum(by_shape.values())
    breakdown = ", ".join(f"shape {k}×{v}" for k, v in sorted(by_shape.items()))
    return [
        Difficulty(
            "writer-gate-unresolved",
            str(sink),
            f"{total} publication(s) in the last {window_days}d whose body the gate "
            f"could not resolve, so it failed open ({breakdown})",
        )
    ]


RESOURCE_ESTIMATE_DRIFT_DEFAULT_REPEAT = 2


def scan_resource_estimate_drift(
    ledger_file: "Path | None" = None, config_path: "Path | None" = None
) -> "list[Difficulty]":
    """Report an executor kind whose resource request under-estimated in several different tasks.

    Reads the request-vs-fact ledger (lib/resource_estimation_ledger.py). Counting is per kind
    and over distinct task ids, never plan versions or stages, so one task's replans cannot
    fire it; the threshold is `resource-estimate-drift-repeat`. The proposed edit is the
    kind's own skill, which owns the request; the edit itself stays the self-improvement
    cycle's. Fail-open: an unreadable ledger reports nothing."""
    repeat = read_config_int(
        "resource-estimate-drift-repeat", RESOURCE_ESTIMATE_DRIFT_DEFAULT_REPEAT, config_path)
    tasks: "dict[str, set]" = {}
    missing: "dict[str, dict[str, set]]" = {}
    for row in resource_estimation_ledger.read_rows(ledger_file):
        kind, under = row.get("kind"), row.get("under")
        if not isinstance(kind, str) or not isinstance(under, list) or not under:
            continue
        tasks.setdefault(kind, set()).add(row.get("task_id"))
        for item in under:
            if isinstance(item, dict):
                missing.setdefault(kind, {}).setdefault(str(item.get("class")), set()).add(str(item.get("text")))
    out = []
    for kind in sorted(tasks):
        if len(tasks[kind]) < repeat:
            continue
        classes = "; ".join(
            f"{cls} ({len(texts)}: {', '.join(sorted(texts)[:3])})" for cls, texts in sorted(missing[kind].items()))
        out.append(Difficulty(
            "resource-estimate-drift",
            f"skills/specializations/{kind}/SKILL.md",
            f"the {kind} executor's resource request left out writes its children then made, in "
            f"{len(tasks[kind])} different tasks; missing resource classes: {classes}. "
            f"action: edit {kind} SKILL.md § Resource request so the consult names them",
        ))
    return out


def default_memory_roots() -> "list[Path]":
    roots: "list[Path]" = []
    global_mem = config_root.agent_home() / "memory-global"
    if global_mem.is_dir():
        roots.append(global_mem)
    roots.extend(config_root.project_memory_dirs())
    cwd_mem = Path.cwd() / ".claude" / "agent-memory"
    if cwd_mem.is_dir():
        roots.append(cwd_mem)
    return roots


def _qualify(root: Path, sub: str) -> str:
    """Root-qualify a scanner's path, unless it is already absolute.

    Each scanner reports memory-root-RELATIVE paths, and one root's `leaves/x.md`
    is another's, so `scan` prefixes the root to keep the finding identifiable
    across roots — that prefix is also what makes the store's (kind, path) key
    stable and what the advisory tier filter resolves. But `scan_orphans` reports
    the ROOT ITSELF for no-root-index, and prefixing an absolute path with its own
    parent produced `/…/memory-global//…/memory-global`: a path naming nothing,
    which no reader can act on and no filter can classify."""
    return sub if Path(sub).is_absolute() else f"{root}/{sub}"


def scan(
    memory_roots: "list[Path]",
    repo_root: "Path | None",
    threshold: int = MEMORY_INDEX_LINE_THRESHOLD,
    settings_paths: "list[Path] | None" = None,
    scan_hooks: bool = True,
    near_dup_threshold: float = NEAR_DUPLICATE_JACCARD_THRESHOLD,
    scan_advisories: bool = True,
) -> "list[Difficulty]":
    out: "list[Difficulty]" = []
    if scan_advisories:
        out.extend(scan_writer_gate_advisories())
        out.extend(scan_resource_estimate_drift())
    if scan_hooks:
        paths = settings_paths if settings_paths is not None else default_settings_paths()
        out.extend(scan_broken_hooks(paths))
    for root in memory_roots:
        for d in scan_oversized_indexes(root, threshold):
            out.append(Difficulty(d.kind, _qualify(root, d.path), d.detail))
        for d in scan_oversized_leaves(root):
            out.append(Difficulty(d.kind, _qualify(root, d.path), d.detail))
        for d in scan_dangling_pointers(root):
            out.append(Difficulty(d.kind, _qualify(root, d.path), d.detail))
        for d in scan_near_duplicates(root, near_dup_threshold):
            out.append(Difficulty(d.kind, _qualify(root, d.path), d.detail))
        for d in scan_orphans(root):
            out.append(Difficulty(d.kind, _qualify(root, d.path), d.detail))
    if repo_root is not None:
        out.extend(scan_ceiling_proximity(repo_root))
        out.extend(scan_crutch_regressions(repo_root))
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
    parser.add_argument("--no-advisories", action="store_true", help="skip the published-text writer-gate fail-open scan")
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
        scan_advisories=not args.no_advisories,
    )

    if args.json:
        print(json.dumps([asdict(d) for d in findings], indent=2))
    else:
        for d in findings:
            print(d.as_line())

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
