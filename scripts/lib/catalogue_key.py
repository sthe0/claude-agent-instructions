"""The narrowed result key of a registered mutation catalogue (stage 13 of the landing-speedup plan).

Difficulty removed: a catalogue test copies the whole ``scripts/`` tree into a scratch directory, so the
dependency tracer records every file there and any edit of the repository reruns the catalogue, though what
decides its verdict is far smaller: the catalogue module, the files it rewrites, what the module imports, and
whatever the tests that must kill its mutants read.

``scripts/tests/mutation_catalogues.toml`` registers a catalogue: its module, and the in-suite tests that run it.
For one registered test the result key is taken over the recorded dependencies that lie inside

  - the catalogue module, the in-suite test files, and every file the module rewrites (its own
    ``rewritten_files()``),
  - the static import closure of the module,
  - the dependencies stored for the tests the module names (``named_tests()``): the files those tests read.

``narrow`` is the one function ``ResultStore.put`` and ``ResultStore.lookup`` both use, so what is stored is
what is looked up. A test the registry does not name, a catalogue whose module cannot be read, and a
catalogue whose named tests have no stored result are not narrowed: they run whenever the full key differs, and
a catalogue with an unknown named test is never stored.

A catalogue pass older than ``suite-gate-catalogue-max-age-h`` (config.md) is never reused: a code change
the narrowed set cannot see ages out within a day.

``CORE_CATALOGUE_KEY_EXCLUDE`` (a repository-relative path) removes one path from every narrowed set. It exists
for the negative control of the scenario that proves the narrowed set holds every rewritten file; the gate
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
from typing import Callable, Mapping, Sequence

REGISTRY_PATH = "scripts/tests/mutation_catalogues.toml"
EXCLUDE_ENV = "CORE_CATALOGUE_KEY_EXCLUDE"
MAX_AGE_KEY = "suite-gate-catalogue-max-age-h"
DEFAULT_MAX_AGE_H = 24
RESOLVE_TIMEOUT_S = 120
SEARCH_DIRS = ("scripts/tests", "scripts/lib", "scripts")

Dep = tuple[str, str, str]

_RESOLVE_CODE = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "module = __import__(sys.argv[2])\n"
    "print(json.dumps({'rewritten': list(module.rewritten_files()), 'named': list(module.named_tests())}))\n"
)


@dataclasses.dataclass(frozen=True)
class Subject:
    """One registered catalogue: the in-suite tests that run it, the files its verdict depends on by name, and the tests that kill its mutants."""
    name: str
    tests: tuple[str, ...]
    paths: frozenset[str]
    named: tuple[str, ...]

    def uncovered(self, rewritten: Sequence[str]) -> list[str]:
        """The files of ``rewritten`` the narrowed set does not hold: an edit of one would not rerun the catalogue."""
        return sorted(set(rewritten) - self.paths)

    def covers(self, test_id: str) -> bool:
        return any(test_id == entry or (("::" not in entry) and test_id.startswith(f"{entry}::"))
                   for entry in self.tests)


@dataclasses.dataclass(frozen=True)
class Registry:
    subjects: tuple[Subject, ...] = ()
    excluded: str = ""

    def find(self, test_id: str) -> Subject | None:
        return next((s for s in self.subjects if s.covers(test_id)), None)

    def named_ids(self) -> frozenset[str]:
        return frozenset(i for s in self.subjects for i in s.named)

    def narrow(self, subject: Subject, deps: Sequence[Dep], named: Mapping[str, Sequence[Dep]]) -> list[Dep] | None:
        """The dependencies a catalogue's key is taken over, sorted; None when a named test has no known dependencies."""
        extra: list[Dep] = []
        for test_id in subject.named:
            if test_id not in named:
                return None
            extra.extend(named[test_id])
        allowed = set(subject.paths) | {path for _kind, path, _digest in extra}
        allowed.discard(self.excluded)
        kept = {(kind, path): digest for kind, path, digest in deps if path in allowed}
        for kind, path, digest in extra:
            if path in allowed:
                kept.setdefault((kind, path), digest)
        return sorted((kind, path, digest) for (kind, path), digest in kept.items())


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
    """Why the gate must not start: the control-only key exclusion is set and the store is not a private copy."""
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
        try:
            tree = ast.parse((core / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
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


def load(core_root: str | Path, env: Mapping[str, str] | None = None,
         declared: Callable[[Path, str], tuple[list[str], list[str]] | None] = _declared) -> Registry:
    """The registry of the tree at ``core_root``; a catalogue that cannot be resolved is left out (it always runs)."""
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
        paths = ({module, *test_files, *rewritten} | set(import_closure(core, module))) - {excluded}
        subjects.append(Subject(name, tests, frozenset(paths), tuple(sorted(named))))
    return Registry(tuple(subjects), excluded)
