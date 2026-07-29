#!/usr/bin/env python3
"""Mechanically enumerate crutch candidates across two domains. No classification.

Difficulty removed: memory-global/leaves/regex-not-for-semantic-classification.md
audited the hook suite by hand, bounded to three enforcement contracts (PreToolUse
deny, Stop block, exit code 2), and closed with a universal claim ("no further
semantic hard-block exists") that a hand-built, recall-bounded list cannot support.
A universally-quantified claim is only discharged against a MECHANICALLY enumerated
domain (CLAUDE.md § On task resolution). This script is that enumerator, widened
past the three-contract boundary to also catch a regex feeding a hard BEHAVIOUR
(a routed/dispatched/suppressed/recorded outcome, not only a deny/block/exit), and
extended to a second domain this repo has never mechanically enumerated at all:
candidate DECIDABLE-RULE statements left as prose in CLAUDE.md / SKILL.md /
policy.md / memory leaves, which are the other named crutch class (a rule never
determinized at all, rather than determinized at the wrong level).

This script CLASSIFIES NOTHING. It emits candidates and mechanical attributes
only — domain membership and a same-scope structural signal, never a verdict on
whether a site is a legitimate structural check or an unguarded semantic one, and
never a verdict on whether a prose statement is genuine perception or should be
mechanized. That judgment is scripts/crutch_registry.toml (stage 2), a SEPARATE
pass over this script's own output — fusing the two would let the judgment hide
inside the enumerator, which is the exact defect being audited (CLAUDE.md
preamble, "Separate rule from perception; determinize the rule at its proper
structural level").

Domain A — CODE SITES (enumerate_code_sites): AST-walk scripts/**/*.py. A "scope"
is a module or a function/method body, EXCLUDING nested function bodies (each
nested def is its own scope). A scope becomes a candidate site iff it locally
constructs a regex (`re.compile`/`search`/`match`/`fullmatch`/`findall`/
`finditer`/`sub`/`subn`), OR references a module/class-level bound
`NAME = re.compile(...)` by name (`NAME.search(...)` etc. — one hop, so a
helper that reuses a regex compiled at module scope is still credited with it),
OR locally reaches one of the hard-outcome sinks below, OR is a function/method
whose OWN NAME matches the hard-behaviour verb set (a `*_blockers`/`is_blocked`
function IS the sink its name says it is, whether or not it happens to call
another hard-behaviour-named function in its own body). For each candidate
scope this records: the regex source text found (if any), the outcome_class
reached (highest-priority sink found; "none" if no sink), whether a
`judge_*`-named call appears in the same scope (the fail-open-judge guard this
repo's advisor module implements), and whether the file imports a module whose
name looks like a detector (`*detect*`) — the one-hop "import edge" signal for a
scope whose regex lives in an imported helper rather than inline (this repo's own
hook-suite shape: the enforcement dict lives in the hook, the regex lives in a
sibling `*_detect.py` module the hook imports and calls by name).

Hard-outcome sinks, in priority order (a scope's outcome_class is the
highest-priority sink reached; ties are impossible, priority is total):
  1. pretooluse_deny  — a dict literal with key "permissionDecision" == "deny"
  2. stop_block        — a dict literal with key "decision" == "block"
  3. exit_code_2        — a call to sys.exit(2) / exit(2)
  4. hard_behaviour     — a call whose name matches a curated verb set (the
                           widening past the three enforcement contracts: a
                           regex that selects a route/specialization, suppresses
                           or emits a directive, or writes recorded state, without
                           itself denying or blocking) OR a function/method
                           definition whose OWN name matches that same set
  5. none               — regex present, no sink reached (e.g. a log line)

A scope-local view severs almost every regex from the sink it ultimately
feeds: measured on this repo's own tree, even WITH one-hop bound-name
propagation only 4 of 745 scope-local sites pair a regex and a hard sink in
the SAME scope, because the real shape is usually a same-file call chain
(`main()` builds the deny dict, `decide()` calls `_deny_msg()`, a sibling
helper does the actual `re.sub(...)`) or a same-file dataflow through a
shared object (hook-turn-end-gate.py's guardians read a frozen `TurnContext`
field that `build_context()` — a different scope — populated by calling the
judge; the guardian itself calls no regex/judge function at all).
`enumerate_code_file_rollups` (below) closes that gap: it folds every scope's
regex/sink/judge/import signal into ONE row per file, so the pairing a
classifier needs exists even when no single scope carries both halves — at
the honest cost of losing WHICH regex feeds WHICH sink when a file carries
more than one of either.

Domain B — PROSE SITES (enumerate_prose_sites): parse CLAUDE.md, skills/**/
SKILL.md, skills/**/policy.md and memory-global/leaves/**/*.md by heading
structure (fenced code blocks skipped). Every line/table-cell/list-item carrying
an obligation-modal keyword (must, never, always, required, shall, should,
forbidden, do not, ...) is emitted as ONE candidate per matching sentence
fragment, tagged with its heading path and the matched modal. Nothing is
filtered by topic — a topic filter here would itself be a regex deciding
meaning, the exact anti-pattern this script exists to avoid reintroducing one
structural level up (see CLAUDE.md preamble). The modal-keyword regex is a
high-recall PREFILTER building an enumeration domain for a later classification
pass (stage 2) — legitimate per the same boundary this script's own domain-A
analysis applies to hard blocks: a prefilter feeding a judgment pass is not the
anti-pattern; a regex feeding a hard block/behaviour DIRECTLY is.

Honest limits (stated so no one over-trusts this):
  - Domain A is a SYNTACTIC check over each file's own source. It does not
    follow calls more than one import-hop deep, and it cannot see dynamic
    construction (`getattr`, `exec`, a pattern assembled at runtime, an aliased
    `import re as rx`). Regex/sink attribution is SCOPE-LOCAL (plus the one-hop
    bound-name resolution described above); `enumerate_code_file_rollups`
    widens the join to file granularity but still cannot say WHICH of a file's
    several regexes feeds WHICH of its several sinks when both are plural. The
    same file-granularity limit holds for `judge_guarded`: the rollup reports
    that SOME judge_* call exists in the file, not that a PARTICULAR guardian is
    judge-guarded — a sibling guardian's judge keeps the file flag True even if
    one guardian loses its own, so per-guardian judge loss is not observable here.
  - The own-definition-name hard-behaviour check (`*_blockers`, `is_blocked`,
    ...) is a NAME-lexicon match, same auditable-not-complete status as
    `_HARD_BEHAVIOUR_TOKENS` itself: a differently-named function that reaches
    an identical sink (e.g. a guardian called `check_self_improvement` instead
    of `self_improvement_blockers`) is invisible to this signal, and the
    lexicon is this repo's own naming convention, not a general one.
  - Domain B's modal-keyword lexicon is fixed and English-only; a normative
    statement phrased without one of these keywords (an imperative sentence with
    no modal, e.g. "Ask when: ...") is invisible to it. This is a RECALL limit
    on the prefilter, stated rather than assumed away — stage 2's classification
    pass sees only what this prefilter surfaces.
  - Neither domain traces cross-file semantic equivalence (two prose statements
    saying the same rule in different words are two separate candidates).
  - A crutch expressed without a regex or a modal keyword at all (a hand-written
    character-by-character string scan standing in for a regex; an obligation
    phrased as a plain declarative with no modal) is invisible to this script.

Usage:
    crutch-inventory.py                 # print the JSONL inventory to stdout
    crutch-inventory.py --check         # exit 1 if any site lacks a registry
                                         # disposition, or the registry is stale
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
REGISTRY_PATH = SCRIPTS_DIR / "crutch_registry.toml"

# --- Domain A: code sites ---------------------------------------------------

_REGEX_ATTRS = frozenset({
    "compile", "match", "search", "fullmatch", "findall", "finditer", "sub", "subn",
})

# Curated hard-BEHAVIOUR verb tokens: a call whose name, split on "_", shares a
# token with this set is treated as reaching a hard outcome even without a
# deny/block/exit — the deliberate widening past the three enforcement
# contracts (CLAUDE.md preamble; this file's own module docstring). Curated
# once and stated here, same spirit as ast_purity.py's IMPURE_ROOTS: a fixed,
# auditable vocabulary, not a claim of completeness.
_HARD_BEHAVIOUR_TOKENS = frozenset({
    "dispatch", "route", "spawn", "suppress", "emit", "block", "blocked",
    "blockers", "deny", "select",
})
_HARD_BEHAVIOUR_EXACT = frozenset({"record_result"})

_SINK_PRIORITY = {
    "pretooluse_deny": 0,
    "stop_block": 1,
    "exit_code_2": 2,
    "hard_behaviour": 3,
    "none": 4,
}


@dataclass(frozen=True)
class CodeSite:
    id: str
    domain: str  # always "code"
    file: str
    scope: str
    line: int
    pattern_source: str
    outcome_class: str
    outcome_detail: str
    judge_guarded: bool
    imports_detector_module: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_hard_behaviour_name(name: str) -> bool:
    if name in _HARD_BEHAVIOUR_EXACT:
        return True
    tokens = set(name.lower().split("_"))
    return bool(tokens & _HARD_BEHAVIOUR_TOKENS)


@dataclass
class _ScopeData:
    line: int
    regex: list[str] = field(default_factory=list)
    sinks: list[tuple[str, str]] = field(default_factory=list)
    judge_guarded: bool = False


class _BoundRegexCollector(ast.NodeVisitor):
    """Collects module/class-level `NAME = re.compile(...)` bindings only.
    Does NOT descend into function/method bodies — an assignment inside a def
    is a local variable, not a bound name other scopes can reference (see
    module docstring, Domain A's "one hop" bound-name resolution)."""

    def __init__(self) -> None:
        self.bound: dict[str, str] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return  # do not descend

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "compile"
            and _is_name(node.value.func.value, "re")
        ):
            try:
                src = ast.unparse(node.value)
            except Exception:
                src = "re.compile(...)"
            self.bound[node.targets[0].id] = src[:200]
        self.generic_visit(node)


def _collect_bound_regexes(tree: ast.Module) -> dict[str, str]:
    collector = _BoundRegexCollector()
    collector.visit(tree)
    return collector.bound


class _ScopeCollector(ast.NodeVisitor):
    """Collects per-scope regex/sink/judge signals. A scope is a module or a
    function/method body, excluding nested function bodies (each nested def
    opens its own scope) — see module docstring, Domain A."""

    def __init__(self, bound_regex: dict[str, str] | None = None) -> None:
        self.scopes: dict[str, _ScopeData] = {"<module>": _ScopeData(line=1)}
        self._stack: list[str] = []
        self._bound_regex = bound_regex or {}

    def _scope_name(self) -> str:
        return ".".join(self._stack) if self._stack else "<module>"

    def _scope(self, line: int) -> _ScopeData:
        name = self._scope_name()
        if name not in self.scopes:
            self.scopes[name] = _ScopeData(line=line)
        return self.scopes[name]

    def _visit_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        data = self.scopes.setdefault(self._scope_name(), _ScopeData(line=node.lineno))
        if _is_hard_behaviour_name(node.name):
            # The function's OWN name, not just names it calls — a `*_blockers`
            # function IS the sink even when its body only reads booleans off a
            # context object and calls nothing hard-behaviour-named itself
            # (this repo's hook-turn-end-gate.py guardians: self_improvement_blockers
            # et al. build no dict/exit/hard-behaviour call locally; the enforcement
            # dict is built centrally in decide(), keyed off their return value).
            data.sinks.append(("hard_behaviour", f"function named {node.name}()"))
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_def(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _call_name(node.func)
        if name is not None:
            scope = self._scope(node.lineno)
            if name in _REGEX_ATTRS and isinstance(node.func, ast.Attribute):
                if _is_name(node.func.value, "re"):
                    try:
                        src = ast.unparse(node)
                    except Exception:
                        src = f"re.{name}(...)"
                    scope.regex.append(src[:200])
                elif (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id in self._bound_regex
                ):
                    # One-hop bound-name propagation: NAME.search(...) where
                    # NAME = re.compile(...) was bound at module/class level —
                    # attribute the pattern to the scope that USES the name,
                    # not only the scope that compiled it.
                    scope.regex.append(self._bound_regex[node.func.value.id])
            if name.startswith("judge_"):
                scope.judge_guarded = True
            is_exit = name == "exit" or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "exit"
                and _is_name(node.func.value, "sys")
            )
            if is_exit and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 2:
                scope.sinks.append(("exit_code_2", "sys.exit(2)"))
            elif _is_hard_behaviour_name(name):
                scope.sinks.append(("hard_behaviour", f"call to {name}()"))
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:  # noqa: N802
        scope = self._scope(node.lineno)
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(value, ast.Constant)):
                continue
            if key.value == "permissionDecision" and value.value == "deny":
                scope.sinks.append(("pretooluse_deny", "permissionDecision=deny"))
            elif key.value == "decision" and value.value == "block":
                scope.sinks.append(("stop_block", "decision=block"))
        self.generic_visit(node)


_DETECTOR_IMPORT_RE = re.compile(r"detect", re.IGNORECASE)


def _imports_detector_module(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_DETECTOR_IMPORT_RE.search(alias.name) for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _DETECTOR_IMPORT_RE.search(node.module):
                return True
    return False


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def _best_sink(sinks: list[tuple[str, str]]) -> tuple[str, str]:
    if not sinks:
        return "none", ""
    best = min(sinks, key=lambda s: _SINK_PRIORITY[s[0]])
    return best


def _collect_file(path: Path) -> tuple[str, _ScopeCollector, bool] | None:
    """Parse one file and collect its per-scope regex/sink/judge signals plus
    its own detector-import flag. Returns None for an unreadable/unparseable
    file. Shared by enumerate_code_sites (per-scope, filtered to candidates)
    and enumerate_code_file_rollups (per-file, unfiltered aggregate) so both
    domains see the exact same underlying scope data."""
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None
    bound_regex = _collect_bound_regexes(tree)
    collector = _ScopeCollector(bound_regex)
    collector.visit(tree)
    detector_import = _imports_detector_module(tree)
    rel = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)
    return rel, collector, detector_import


def enumerate_code_sites(root: Path) -> list[CodeSite]:
    """Enumerate Domain-A candidate sites under `root` (scanned recursively for
    *.py). A candidate site is any scope that locally constructs a regex, or
    locally reaches a hard-outcome sink, or both. Deterministic: files and
    scopes are visited and returned in a fixed sorted order."""
    sites: list[CodeSite] = []
    for path in sorted(root.rglob("*.py")):
        collected = _collect_file(path)
        if collected is None:
            continue
        rel, collector, detector_import = collected
        for scope_name, data in sorted(collector.scopes.items()):
            if not data.regex and not data.sinks:
                continue
            outcome_class, outcome_detail = _best_sink(data.sinks)
            sites.append(
                CodeSite(
                    id=_stable_id("code", rel, scope_name),
                    domain="code",
                    file=rel,
                    scope=scope_name,
                    line=data.line,
                    pattern_source="; ".join(data.regex),
                    outcome_class=outcome_class,
                    outcome_detail=outcome_detail,
                    judge_guarded=data.judge_guarded,
                    imports_detector_module=detector_import,
                )
            )
    sites.sort(key=lambda s: (s.file, s.scope))
    return sites


@dataclass(frozen=True)
class FileRollup:
    id: str
    domain: str  # always "code_file_rollup"
    file: str
    scopes_with_sink: tuple[str, ...]
    regex_patterns: tuple[str, ...]
    sink_classes: tuple[str, ...]
    outcome_class: str
    outcome_detail: str
    judge_guarded: bool
    imports_detector_module: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scopes_with_sink"] = list(d["scopes_with_sink"])
        d["regex_patterns"] = list(d["regex_patterns"])
        d["sink_classes"] = list(d["sink_classes"])
        return d


def enumerate_code_file_rollups(root: Path) -> list[FileRollup]:
    """One record per file, folding EVERY scope's regex/sink/judge signal
    together — the fix for the scope-local severing problem described in the
    module docstring. Unlike enumerate_code_sites, this aggregates ALL of a
    file's scopes, including ones with neither local regex nor local sink
    (e.g. a scope that only calls a judge_* function): such a scope is not a
    CANDIDATE site on its own, but its judge_guarded signal is still real
    information about the file as a whole and must not be dropped — dropping
    it is exactly how hook-turn-end-gate.py's build_context() (the only scope
    that calls any judge_*/detect function in that file) would otherwise
    vanish without a trace."""
    rollups: list[FileRollup] = []
    for path in sorted(root.rglob("*.py")):
        collected = _collect_file(path)
        if collected is None:
            continue
        rel, collector, detector_import = collected
        regex_patterns: list[str] = []
        sinks: list[tuple[str, str]] = []
        judge_guarded = False
        scopes_with_sink: list[str] = []
        for scope_name, data in sorted(collector.scopes.items()):
            for pattern in data.regex:
                if pattern not in regex_patterns:
                    regex_patterns.append(pattern)
            if data.sinks:
                scopes_with_sink.append(scope_name)
            sinks.extend(data.sinks)
            judge_guarded = judge_guarded or data.judge_guarded
        if not regex_patterns and not sinks:
            continue
        outcome_class, outcome_detail = _best_sink(sinks)
        sink_classes = sorted(
            {s[0] for s in sinks if s[0] != "none"}, key=lambda c: _SINK_PRIORITY[c]
        )
        rollups.append(
            FileRollup(
                id=_stable_id("code_file_rollup", rel),
                domain="code_file_rollup",
                file=rel,
                scopes_with_sink=tuple(scopes_with_sink),
                regex_patterns=tuple(sorted(regex_patterns)),
                sink_classes=tuple(sink_classes),
                outcome_class=outcome_class,
                outcome_detail=outcome_detail,
                judge_guarded=judge_guarded,
                imports_detector_module=detector_import,
            )
        )
    rollups.sort(key=lambda r: r.file)
    return rollups


# --- Domain B: prose sites ---------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-ZА-ЯЁ0-9`"\'(\[])')

# Obligation-modal lexicon. Deliberately not filtered by topic — every match
# is emitted as a candidate; false positives are for stage 2 to dismiss as
# "not-normative", not for this prefilter to pre-judge.
_MODAL_RE = re.compile(
    r"\b("
    r"must not|must|never|always|required|mandatory|forbidden|"
    r"shall not|shall|should not|should|do not|don't|"
    r"cannot|can't|prohibited|disallowed|non-skippable|non-negotiable|"
    r"is required|are required"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProseSite:
    id: str
    domain: str  # always "prose"
    file: str
    line: int
    heading_path: str
    sentence: str
    matched_modal: str

    def to_dict(self) -> dict:
        return asdict(self)


def _strip_line_noise(line: str) -> str:
    line = _LIST_MARKER_RE.sub("", line)
    line = line.strip()
    if line.startswith("|") and line.endswith("|"):
        cells = [c.strip() for c in line.strip("|").split("|")]
        line = " ".join(c for c in cells if c and not set(c) <= {"-", ":"})
    return line


def _iter_statement_units(path: Path) -> list[tuple[int, list[str], str]]:
    """Yield (line_no, heading_path, statement_text) for every non-code,
    non-heading, non-blank line, split into sentence-like fragments."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return []
    out: list[tuple[int, list[str], str]] = []
    heading_stack: list[tuple[int, str]] = []
    in_fence = False
    for lineno, raw in enumerate(lines, start=1):
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading_match = _HEADING_RE.match(raw)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            heading_stack = [h for h in heading_stack if h[0] < level]
            heading_stack.append((level, title))
            continue
        text = _strip_line_noise(raw)
        if not text:
            continue
        path_now = [h[1] for h in heading_stack]
        for fragment in _SENTENCE_SPLIT_RE.split(text):
            fragment = fragment.strip()
            if fragment:
                out.append((lineno, path_now, fragment))
    return out


def enumerate_prose_sites(paths: list[Path]) -> list[ProseSite]:
    """Enumerate Domain-B candidates across the given markdown files.
    Deterministic: files are processed in the order given; callers wanting a
    stable overall order should pass a pre-sorted list (discover_prose_paths
    does)."""
    sites: list[ProseSite] = []
    for path in paths:
        rel = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)
        for lineno, heading_path, fragment in _iter_statement_units(path):
            modal_match = _MODAL_RE.search(fragment)
            if not modal_match:
                continue
            heading_str = " > ".join(heading_path)
            sites.append(
                ProseSite(
                    id=_stable_id("prose", rel, heading_str, fragment),
                    domain="prose",
                    file=rel,
                    line=lineno,
                    heading_path=heading_str,
                    sentence=fragment,
                    matched_modal=modal_match.group(1).lower(),
                )
            )
    sites.sort(key=lambda s: (s.file, s.line, s.id))
    return sites


def discover_prose_paths(repo_root: Path) -> list[Path]:
    """The real-usage file list for Domain B, per the stage-1 material:
    CLAUDE.md, skills/**/SKILL.md, skills/**/policy.md, memory-global/leaves/**."""
    paths: set[Path] = set()
    claude_md = repo_root / "CLAUDE.md"
    if claude_md.exists():
        paths.add(claude_md)
    skills_dir = repo_root / "skills"
    if skills_dir.exists():
        paths.update(skills_dir.rglob("SKILL.md"))
        paths.update(skills_dir.rglob("policy.md"))
    leaves_dir = repo_root / "memory-global" / "leaves"
    if leaves_dir.exists():
        paths.update(leaves_dir.rglob("*.md"))
    return sorted(paths)


# --- Registry check ----------------------------------------------------------


def _load_registry(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    entries = data.get("entry", [])
    return {e["id"]: e for e in entries if isinstance(e, dict) and "id" in e}


def _index_by_id(records: list) -> dict[str, object]:
    """Key records by their stable id, failing LOUD on a collision instead of
    silently keeping whichever record happened to be last — a silent collapse
    would let --check report a site as covered when it never actually
    inspected the colliding one."""
    index: dict[str, object] = {}
    for record in records:
        if record.id in index:
            other = index[record.id]
            raise ValueError(
                f"id collision {record.id!r}: {other.file!r} and {record.file!r} "
                "hashed to the same stable id"
            )
        index[record.id] = record
    return index


def run_check(root: Path, registry_path: Path) -> int:
    code_sites = enumerate_code_sites(root)
    file_rollups = enumerate_code_file_rollups(root)
    prose_sites = enumerate_prose_sites(discover_prose_paths(REPO_ROOT))
    all_sites = _index_by_id([*code_sites, *file_rollups, *prose_sites])
    registry = _load_registry(registry_path)

    missing = sorted(set(all_sites) - set(registry))
    stale = sorted(set(registry) - set(all_sites))
    undispositioned = sorted(
        eid for eid, e in registry.items()
        if eid in all_sites and not e.get("disposition")
    )
    bad_defer = sorted(
        eid for eid, e in registry.items()
        if e.get("disposition") == "defer" and not e.get("reason")
    )

    print(
        f"crutch-inventory --check: {len(code_sites)} code site(s), "
        f"{len(file_rollups)} file-rollup(s), {len(prose_sites)} prose site(s)"
    )
    print(f"registry: {len(registry)} entrie(s) at {registry_path}")
    print(f"missing from registry: {len(missing)}")
    for eid in missing[:20]:
        print(f"  MISSING {eid}  {all_sites[eid].file}")
    if len(missing) > 20:
        print(f"  ... and {len(missing) - 20} more")
    print(f"stale registry entries (site no longer exists): {len(stale)}")
    for eid in stale[:20]:
        print(f"  STALE {eid}")
    print(f"undispositioned registry entries: {len(undispositioned)}")
    print(f"defer entries missing a reason: {len(bad_defer)}")

    ok = not (missing or stale or undispositioned or bad_defer)
    print("crutch-inventory --check: OK" if ok else "crutch-inventory --check: FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify registry completeness; no JSONL output")
    parser.add_argument(
        "--root",
        type=Path,
        default=SCRIPTS_DIR,
        help=(
            "root to scan for Domain-A code sites and file rollups. Domain-B "
            "prose sites are NOT affected by this flag — they are always the "
            "fixed file set named in the module docstring (CLAUDE.md, "
            "skills/**/SKILL.md, skills/**/policy.md, memory-global/leaves/**), "
            "discovered from the repository root regardless of --root."
        ),
    )
    args = parser.parse_args(argv)

    if args.check:
        return run_check(args.root, REGISTRY_PATH)

    code_sites = enumerate_code_sites(args.root)
    file_rollups = enumerate_code_file_rollups(args.root)
    prose_sites = enumerate_prose_sites(discover_prose_paths(REPO_ROOT))
    for site in [*code_sites, *file_rollups, *prose_sites]:
        print(json.dumps(site.to_dict(), sort_keys=True))
    print(
        f"crutch-inventory: {len(code_sites)} code site(s), "
        f"{len(file_rollups)} file-rollup(s), {len(prose_sites)} prose site(s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
