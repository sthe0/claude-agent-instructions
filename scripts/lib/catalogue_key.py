"""The narrowed result key of a registered mutation catalogue (stage 13 of the landing-speedup plan).

Difficulty removed: a catalogue test copies the whole ``scripts/`` tree into a scratch directory, so the
dependency tracer records every file there and any edit of the repository reruns the catalogue, though what
decides its verdict is far smaller: the files its mutants rewrite and the tests that must kill them.

``scripts/tests/mutation_catalogues.toml`` registers a catalogue: its module, and the in-suite tests that run it.
The result key of a registered test is taken over the declared set, never over what a run happened to read:

  - the files the module rewrites (its own ``rewritten_files()``),
  - the catalogue module and its static import closure,
  - the in-suite test files and the files of the tests the module names (``named_tests()``), with their
    static import closure,
  - the repository files any of those files names by a literal path (string constants of their syntax trees).

``narrow`` is the one function ``ResultStore.put`` and ``ResultStore.lookup`` both use: digests from the
clean candidate tree at put, from the candidate's tree at lookup. A test the registry does not name, a catalogue
whose module cannot be resolved, one whose test files cannot be scanned and one whose named test is not
defined where its id says are not narrowed: they run whenever the full key differs.

A catalogue pass older than ``suite-gate-catalogue-max-age-h`` (config.md) is never reused: what the declared set
cannot see (what rewritten code calls at runtime) ages out within a day. A run that fails a catalogue deletes
its stored passes.

``CORE_CATALOGUE_KEY_EXCLUDE`` (a repository-relative path) removes one path from every set. It exists
for the negative control of the scenario that proves the set holds every rewritten file; the gate
refuses to start with it set unless its result store is a private copy (``control_refusal``).
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

REGISTRY_PATH = "scripts/tests/mutation_catalogues.toml"
EXCLUDE_ENV = "CORE_CATALOGUE_KEY_EXCLUDE"
MAX_AGE_KEY = "suite-gate-catalogue-max-age-h"
DEFAULT_MAX_AGE_H = 24
RESOLVE_TIMEOUT_S = 120
MAX_LITERAL_CHARS = 300
SEARCH_DIRS = ("scripts/tests", "scripts/lib", "scripts")

FILE = "file"
Dep = tuple[str, str, str]

_RESOLVE_CODE = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "module = __import__(sys.argv[2])\n"
    "print(json.dumps({'rewritten': list(module.rewritten_files()), 'named': list(module.named_tests())}))\n"
)


@dataclasses.dataclass(frozen=True)
class Subject:
    """One registered catalogue: the in-suite tests that run it, the declared file set its verdict depends on,
    and the tests that kill its mutants. ``rewritten``, ``module_closure``, ``named_closure`` and ``literal``
    are the parts of ``paths`` by origin."""
    name: str
    tests: tuple[str, ...]
    paths: frozenset[str]
    named: tuple[str, ...]
    rewritten: frozenset[str] = frozenset()
    module_closure: frozenset[str] = frozenset()
    named_closure: frozenset[str] = frozenset()
    literal: frozenset[str] = frozenset()

    def uncovered(self, rewritten: Sequence[str]) -> list[str]:
        """The files of ``rewritten`` the set does not hold: an edit of one would not rerun the catalogue."""
        return sorted(set(rewritten) - self.paths)

    def covers(self, test_id: str) -> bool:
        """``tests`` entries are ``file::name`` ids (a parametrized test is covered by its bracketed ids)."""
        return any(test_id == entry or test_id.startswith(f"{entry}[") for entry in self.tests)


@dataclasses.dataclass(frozen=True)
class Registry:
    subjects: tuple[Subject, ...] = ()
    excluded: str = ""

    def find(self, test_id: str) -> Subject | None:
        return next((s for s in self.subjects if s.covers(test_id)), None)


def narrow(subject: Subject, resolve: Callable[[str, str], str]) -> list[Dep]:
    """The dependencies a catalogue's key is taken over: its declared set at the digests ``resolve`` gives, sorted."""
    return [(FILE, path, resolve(FILE, path)) for path in sorted(subject.paths)]


def max_age_s(config_path: str | Path | None = None) -> float:
    """The reuse bound of a catalogue pass in seconds: the config.md row, else 24 hours."""
    hours = DEFAULT_MAX_AGE_H
    try:
        scripts = str(Path(__file__).resolve().parents[1])
        if scripts not in sys.path:
            sys.path.append(scripts)
        from agentctl.config import parse_config_md

        value = float(parse_config_md(Path(config_path) if config_path else None).get(MAX_AGE_KEY, ""))
        hours = value if value > 0 else hours
    except (ImportError, OSError, ValueError):
        pass
    return hours * 3600


def control_refusal(env: Mapping[str, str], private_store: bool) -> str:
    """Why the gate must not start: the control-only key exclusion is set and the store is not a private copy
    (``private_store``: ``CORE_RESULT_STORE`` is set and names a store other than the live one)."""
    if env.get(EXCLUDE_ENV) and not private_store:
        return (f"{EXCLUDE_ENV} is a control-only setting and the live result store would learn keys it "
                "narrowed on purpose; point CORE_RESULT_STORE at a private copy of the store")
    return ""


def _module_name(path: str) -> str:
    return Path(path).stem


def _import_targets(core: Path, importer: str, tree: ast.AST) -> set[str]:
    """Repository-relative files the imports of ``tree`` can resolve to, within the repository."""
    here = Path(importer).parent
    roots = [here.as_posix(), *SEARCH_DIRS]
    names: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.append((base, node.level))
            names += [(f"{base}.{alias.name}" if base else alias.name, node.level) for alias in node.names]
    found: set[str] = set()
    for dotted, level in names:
        parts = [p for p in dotted.split(".") if p]
        if level:
            anchor = here
            for _ in range(level - 1):
                anchor = anchor.parent
            search = [anchor.as_posix()]
        else:
            search = roots
        for root in search:
            for depth in range(1, len(parts) + 1):
                prefix = Path(root, *parts[:depth])
                for candidate in (prefix.with_suffix(".py"), prefix / "__init__.py"):
                    if (core / candidate).is_file():
                        found.add(candidate.as_posix())
    return found


def import_closure(core: str | Path, start: str) -> frozenset[str]:
    """``start`` and every repository file reachable from it by a static ``import`` (dynamic imports are not followed)."""
    core = Path(core)
    seen: set[str] = set()
    queue = [start]
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        tree = _tree_of(core, rel)
        if tree is not None:
            queue.extend(_import_targets(core, rel, tree) - seen)
    return frozenset(seen)


def _declared(core: Path, module: str) -> tuple[list[str], list[str]] | None:
    """(rewritten files, named test ids) the catalogue module reports, read in a child interpreter."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop(EXCLUDE_ENV, None)
    try:
        proc = subprocess.run(
            [sys.executable, "-P", "-c", _RESOLVE_CODE, str(core / Path(module).parent), _module_name(module)],
            cwd=core, capture_output=True, text=True, env=env, timeout=RESOLVE_TIMEOUT_S)
        if proc.returncode != 0:
            return None
        found = json.loads(proc.stdout)
        return [str(p) for p in found["rewritten"]], [str(i) for i in found["named"]]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
        return None


def _tree_of(core: Path, rel: str) -> ast.AST | None:
    try:
        return ast.parse((core / rel).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None


def _defines(tree: ast.AST, name: str) -> bool:
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name for n in ast.walk(tree))


def literal_files(core: Path, sources: Iterable[str]) -> frozenset[str]:
    """Repository files that string constants in ``sources`` name: relative to the repository root or to the
    source's own directory; regular files only (a directory is not a dependency), inside the repository.
    A named ``.py`` file brings its import closure."""
    found: set[str] = set()
    for rel in sources:
        tree = _tree_of(core, rel)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            text = node.value.strip()
            if not text or "\n" in text or len(text) > MAX_LITERAL_CHARS or text.startswith("/"):
                continue
            for base in ("", Path(rel).parent.as_posix()):
                candidate = os.path.normpath(os.path.join(base, text))
                if candidate.startswith(".."):
                    continue
                try:
                    if (core / candidate).is_file():
                        found.add(Path(candidate).as_posix())
                except (OSError, ValueError):
                    continue
    for rel in [f for f in found if f.endswith(".py")]:
        found |= import_closure(core, rel)
    return frozenset(found)


def _named_scannable(core: Path, named: Sequence[str]) -> bool:
    """Every named test is defined, by the last part of its id, in a test file that parses."""
    for test_id in named:
        file, _, rest = test_id.partition("::")
        tree = _tree_of(core, file)
        if tree is None or not rest or not _defines(tree, rest.split("::")[-1].split("[")[0]):
            return False
    return True


def load(core_root: str | Path, env: Mapping[str, str] | None = None,
         declared: Callable[[Path, str], tuple[list[str], list[str]] | None] = _declared) -> Registry:
    """The registry of the tree at ``core_root``; a catalogue that cannot be resolved or scanned is left out (it always runs)."""
    core = Path(core_root)
    excluded = (os.environ if env is None else env).get(EXCLUDE_ENV, "")
    try:
        entries = tomllib.loads((core / REGISTRY_PATH).read_text(encoding="utf-8")).get("catalogue", [])
    except (OSError, tomllib.TOMLDecodeError):
        return Registry((), excluded)
    subjects = []
    for entry in entries:
        try:
            module, tests = str(entry["module"]), tuple(str(t) for t in entry["tests"])
            name = str(entry["name"])
        except (KeyError, TypeError):
            continue
        reported = declared(core, module)
        if reported is None:
            continue
        rewritten, named = reported
        test_files = {t.partition("::")[0] for t in tests}
        named_files = {t.partition("::")[0] for t in named}
        if not named or not _named_scannable(core, named) or any(_tree_of(core, f) is None for f in test_files):
            continue
        module_closure = import_closure(core, module)
        named_closure = frozenset().union(*(import_closure(core, f) for f in test_files | named_files))
        literal = literal_files(core, module_closure | named_closure)
        paths = ({module, *rewritten} | module_closure | named_closure | literal) - {excluded}
        subjects.append(Subject(name, tests, frozenset(paths), tuple(sorted(named)),
                                frozenset(rewritten) - {excluded}, module_closure - {excluded},
                                named_closure - {excluded}, literal - {excluded}))
    return Registry(tuple(subjects), excluded)
