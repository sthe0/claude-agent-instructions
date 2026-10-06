#!/usr/bin/env python3
"""Mechanical inventory of the lexical call sites in non-test Python scripts.

Why this exists: `memory-global/leaves/regex-not-for-semantic-classification.md`
lists the places where a lexical measure (a regex, a difflib ratio, a
word-overlap helper) decides a question of meaning. A hand-built list is only
existential evidence about the sites someone thought to grep for; this tool
enumerates the whole domain below, so the claim "no other site remains" is
checked against a complete list, one classified verdict per site.

Domain (what the AST walk enumerates, per call, keyed
`<path>::<qualname>::<kind>` with a hit count):
  * `re.<fn>` -- compile/match/search/fullmatch/findall/finditer/sub/subn/split
    on the `re` module, import aliases resolved (`import re as r`,
    `from re import search as s`);
  * `pattern.<method>` -- search/match/fullmatch/findall/finditer/sub/subn on
    any receiver (so calls on compiled patterns are seen), and `split` on a
    receiver resolved to an `re.compile` result;
  * `difflib.<name>` -- any call on the difflib module, a name imported from it
    and the `.ratio()` family;
  * `lexical.<fn>` -- the word-overlap helpers nominate, term_score, tokenize,
    called by bare name, through a module attribute or through an alias.

Classes: structural (decides a fixed syntax or format, not meaning),
candidate-generation (only nominates candidates for a fail-open judge) and
meaning-decision (the lexical measure itself decides a question of meaning;
it needs an `open_violation` reference to a bullet under the leaf's
`### Open violations` heading that names the file and the qualname as a
backtick code span). A key's class is the strictest class among its hits
(structural < candidate-generation < meaning-decision); a key of more than one
hit carries `hit_classes`, one class per hit in source order.

Files: every git-tracked non-test `*.py` of the checkout rooted at the working
directory (the index, so an untracked file is not walked), every non-test
`*.py` under it when it is not a git checkout. A path is a test path when a
directory component is named `tests`.

BLIND SPOT -- not covered, stated so the check is trusted no further than it
earns: keyword-list membership, substring (`in`) tests and set overlap have no
call shape to key on; non-Python (shell) scripts are never read (the
scripts/**/*.sh files, e.g. a grep over push output); dynamic pattern
construction -- a regex applied through `getattr(re, name)(...)`, a matcher
function passed around as a value, `eval`/`exec` -- is not named by syntax and
is not enumerated.

Modes: `--check` (exit 1 with one `unclassified:`, `stale:`, `count-changed:`,
`hit-classes-mismatch:`, `class-not-strictest:`, `bad-entry:` or
`meaning-decision-without-open-violation:` line per offending key), `--json`
(the enumeration, with line numbers).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

RECORD = "docs/operations/lexical-meaning-inventory.toml"
LEAF = "memory-global/leaves/regex-not-for-semantic-classification.md"

CLASSES = ("structural", "candidate-generation", "meaning-decision")
_RE_FUNCS = frozenset(
    {"compile", "match", "search", "fullmatch", "findall", "finditer", "sub", "subn", "split"}
)
_PATTERN_METHODS = frozenset(
    {"search", "match", "fullmatch", "findall", "finditer", "sub", "subn"}
)
_RATIO_METHODS = frozenset({"ratio", "quick_ratio", "real_quick_ratio"})
_LEXICAL = frozenset({"nominate", "term_score", "tokenize"})


def _clean_git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _is_test_path(rel: Path) -> bool:
    return "tests" in rel.parts[:-1]


def _git_tracked_py(root: Path) -> list[Path] | None:
    """Tracked `*.py` files when `root` is itself the top of a git checkout."""
    env = _clean_git_env()
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, check=True, text=True, env=env,
        ).stdout.strip()
        if Path(top).resolve() != root.resolve():
            return None
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
            capture_output=True, check=True, env=env,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [root / name for name in listing.decode("utf-8").split("\0") if name]


def _enumeration_files(root) -> list[Path]:
    root = Path(root)
    tracked = _git_tracked_py(root)
    candidates = tracked if tracked is not None else list(root.rglob("*.py"))
    return sorted(
        p for p in candidates if p.is_file() and not _is_test_path(p.relative_to(root))
    )


class _Imports:
    def __init__(self, tree: ast.AST) -> None:
        self.re_modules: set[str] = set()
        self.difflib_modules: set[str] = set()
        self.re_names: dict[str, str] = {}
        self.difflib_names: dict[str, str] = {}
        self.lexical_names: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "re":
                        self.re_modules.add(a.asname or "re")
                    elif a.name == "difflib":
                        self.difflib_modules.add(a.asname or "difflib")
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    local = a.asname or a.name
                    if node.module == "re" and a.name in _RE_FUNCS:
                        self.re_names[local] = a.name
                    elif node.module == "difflib":
                        self.difflib_names[local] = a.name
                    elif a.name in _LEXICAL:
                        self.lexical_names[local] = a.name


def _terminal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _Walker:
    def __init__(self, tree: ast.AST) -> None:
        self.imports = _Imports(tree)
        self.compiled = self._compiled_names(tree)
        self.hits: dict[tuple[str, str], list[tuple[int, int]]] = {}
        self._scope: list[str] = []

    def _is_re_compile(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        f = node.func
        if isinstance(f, ast.Attribute):
            return (
                f.attr == "compile"
                and isinstance(f.value, ast.Name)
                and f.value.id in self.imports.re_modules
            )
        return isinstance(f, ast.Name) and self.imports.re_names.get(f.id) == "compile"

    def _compiled_names(self, tree: ast.AST) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and self._is_re_compile(node.value):
                for t in node.targets:
                    n = _terminal(t)
                    if n:
                        names.add(n)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if self._is_re_compile(node.value):
                    n = _terminal(node.target)
                    if n:
                        names.add(n)
        return names

    def kind(self, call: ast.Call) -> str | None:
        imp, f = self.imports, call.func
        if isinstance(f, ast.Attribute):
            if isinstance(f.value, ast.Name):
                if f.value.id in imp.re_modules and f.attr in _RE_FUNCS:
                    return "re." + f.attr
                if f.value.id in imp.difflib_modules:
                    return "difflib." + f.attr
            if f.attr in _LEXICAL:
                return "lexical." + f.attr
            if f.attr in _PATTERN_METHODS:
                return "pattern." + f.attr
            if f.attr == "split" and (
                self._is_re_compile(f.value) or _terminal(f.value) in self.compiled
            ):
                return "pattern.split"
            if f.attr in _RATIO_METHODS:
                return "difflib." + f.attr
        elif isinstance(f, ast.Name):
            if f.id in imp.re_names:
                return "re." + imp.re_names[f.id]
            if f.id in imp.difflib_names:
                return "difflib." + imp.difflib_names[f.id]
            if f.id in imp.lexical_names:
                return "lexical." + imp.lexical_names[f.id]
            if f.id in _LEXICAL:
                return "lexical." + f.id
        return None

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            outer = list(node.decorator_list)
            if not isinstance(node, ast.ClassDef):
                outer += node.args.defaults + [d for d in node.args.kw_defaults if d]
            else:
                outer += node.bases + [k.value for k in node.keywords]
            for child in outer:
                self.visit(child)
            self._scope.append(node.name)
            for child in node.body:
                self.visit(child)
            self._scope.pop()
            return
        if isinstance(node, ast.Call):
            k = self.kind(node)
            if k:
                qual = ".".join(self._scope) or "<module>"
                self.hits.setdefault((qual, k), []).append((node.lineno, node.col_offset))
        for child in ast.iter_child_nodes(node):
            self.visit(child)


def enumerate_file(path: Path) -> dict[tuple[str, str], list[tuple[int, int]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    w = _Walker(tree)
    w.visit(tree)
    return {k: sorted(v) for k, v in w.hits.items()}


def enumerate_sites(root) -> tuple[dict[str, list[int]], list[str]]:
    """({key: [line, ...] in source order}, [unparseable path, ...])."""
    root = Path(root)
    sites: dict[str, list[int]] = {}
    broken: list[str] = []
    for p in _enumeration_files(root):
        rel = p.relative_to(root).as_posix()
        try:
            hits = enumerate_file(p)
        except (SyntaxError, UnicodeDecodeError):
            broken.append(rel)
            continue
        for (qual, kind), where in hits.items():
            sites[f"{rel}::{qual}::{kind}"] = [line for line, _ in where]
    return sites, broken


def _strictest(classes) -> str:
    return max(classes, key=CLASSES.index)


def open_violation_bullets(text: str) -> list[str]:
    m = re.search(r"^### Open violations\s*$(.*?)(?=^#{2,3} |\Z)", text, re.M | re.S)
    out: list[str] = []
    live = False
    for line in (m.group(1) if m else "").splitlines():
        if line.startswith("- "):
            out.append(line.strip())
            live = True
        elif live and line.strip() and line[:1].isspace():
            out[-1] += " " + line.strip()
        else:
            live = False
    return out


def _squash(text: str) -> str:
    return " ".join(text.split())


def _reference_resolves(key: str, ref, bullets: list[str]) -> bool:
    if not isinstance(ref, str) or not ref.strip():
        return False
    path, _, rest = key.partition("::")
    qual = rest.rpartition("::")[0]
    want = _squash(ref)
    return any(
        want in _squash(b) and path in b and "`" + qual + "`" in b for b in bullets
    )


def check(root) -> list[str]:
    root = Path(root)
    sites, broken = enumerate_sites(root)
    problems = [f"unparseable: {p}" for p in broken]
    rec_path = root / RECORD
    record = {}
    if rec_path.is_file():
        record = tomllib.loads(rec_path.read_text(encoding="utf-8")).get("sites", {})
    leaf_path = root / LEAF
    bullets = (
        open_violation_bullets(leaf_path.read_text(encoding="utf-8"))
        if leaf_path.is_file()
        else []
    )
    for key in sorted(sites):
        if key not in record:
            problems.append(f"unclassified: {key}")
    for key in sorted(record):
        entry = record[key]
        if key not in sites:
            problems.append(f"stale: {key}")
            continue
        hits = len(sites[key])
        cls, ground = entry.get("class"), entry.get("ground")
        if cls not in CLASSES or not isinstance(ground, str) or not ground.strip():
            problems.append(f"bad-entry: {key}")
            continue
        if entry.get("hits") != hits:
            problems.append(f"count-changed: {key}")
        hc = entry.get("hit_classes")
        if hc is None and hits == 1:
            hc = [cls]
        if (
            not isinstance(hc, list)
            or len(hc) != hits
            or any(c not in CLASSES for c in hc)
        ):
            problems.append(f"hit-classes-mismatch: {key}")
            continue
        if cls != _strictest(hc):
            problems.append(f"class-not-strictest: {key}")
        if cls == "meaning-decision" and not _reference_resolves(
            key, entry.get("open_violation"), bullets
        ):
            problems.append(f"meaning-decision-without-open-violation: {key}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--check", action="store_true", help="verify the committed record")
    ap.add_argument("--json", action="store_true", help="print the enumeration")
    ap.add_argument("--root", default=".", help="tree to read (default: working directory)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    if args.json:
        sites, broken = enumerate_sites(root)
        json.dump({"sites": sites, "unparseable": broken}, sys.stdout, indent=1, sort_keys=True)
        print()
        return 0
    if args.check:
        problems = check(root)
        for line in problems:
            print(line)
        if problems:
            return 1
        print("lexical-meaning-inventory --check: OK")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
