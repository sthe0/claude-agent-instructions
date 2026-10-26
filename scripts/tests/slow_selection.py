#!/usr/bin/env python3
"""Change-aware selection of the slow tests declared in slow_tests.toml.

A slow test runs when one of its guard globs matches a path changed against the base;
otherwise it is skipped. The base is AGENTCTL_SLOW_BASE when set, else the merge base
of HEAD with origin/main; when that merge base cannot be computed or equals HEAD,
every slow test runs. `--run-slow` or AGENTCTL_SLOW_TESTS=all runs them all. The full
rule, and why a plan check pins AGENTCTL_SLOW_BASE, is in the header of slow_tests.toml.

conftest.py imports the two pytest hooks below; the CLI prints the same decision:

    python3 scripts/tests/slow_selection.py [--base REF | --changed FILE] --format lines

prints `<run|skip> <node id>` per registry entry. `--changed FILE` takes the changed
paths from FILE, one per line (/dev/null: nothing changed).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent.parent
REGISTRY_PATH = TESTS_DIR / "slow_tests.toml"
RUN_ALL_ENV = "AGENTCTL_SLOW_TESTS"
BASE_ENV = "AGENTCTL_SLOW_BASE"
UPSTREAM = "origin/main"
GIT_TIMEOUT_S = 10
RUN_SLOW_OPTION = "--run-slow"
MARKER_LINE = "slow: listed in scripts/tests/slow_tests.toml; runs only when a guard path changed (see that file)"
_REPO_LOCATING_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


@dataclass(frozen=True)
class Entry:
    node: str
    guards: "tuple[str, ...]"
    catalogue_targets: "dict | None" = None

    def matches(self, node: str) -> bool:
        return node == self.node or node.startswith(self.node + "[") or node.startswith(self.node + "::")

    def guarded(self, changed: "frozenset[str]") -> bool:
        return any(fnmatchcase(path, glob) for path in changed for glob in self.guards)


@dataclass(frozen=True)
class Selection:
    """`changed is None` means every slow test runs; `reason` says why or against what."""

    changed: "frozenset[str] | None"
    reason: str

    def runs(self, entry: Entry) -> bool:
        return self.changed is None or entry.guarded(self.changed)

    @property
    def skip_reason(self) -> str:
        return (f"slow test skipped: none of its guard paths changed against {self.reason}; "
                f"run with {RUN_SLOW_OPTION} or {RUN_ALL_ENV}=all (registry scripts/tests/slow_tests.toml)")


def load_registry(path: Path = REGISTRY_PATH) -> "list[Entry]":
    with path.open("rb") as fh:
        groups = tomllib.load(fh).get("slow", [])
    return [Entry(node, tuple(g["guards"]), g.get("catalogue_targets"))
            for g in groups for node in g["nodes"]]


def _run_all(reason: str) -> Selection:
    return Selection(None, reason)


def compute_selection(root: Path = REPO_ROOT, env: "dict[str, str] | None" = None,
                      run_all: bool = False, runner=subprocess.run) -> Selection:
    env = os.environ if env is None else env
    if run_all:
        return _run_all(f"{RUN_SLOW_OPTION} given")
    if env.get(RUN_ALL_ENV) == "all":
        return _run_all(f"{RUN_ALL_ENV}=all")
    git_env = {k: v for k, v in env.items() if k not in _REPO_LOCATING_GIT_ENV}

    def git(*args: str) -> "subprocess.CompletedProcess[str] | None":
        proc = runner(["git", "-C", str(root), "-c", "core.quotepath=off", *args],
                      capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=git_env)
        return proc if proc.returncode == 0 else None

    try:
        explicit = env.get(BASE_ENV)
        if explicit:
            resolved = git("rev-parse", "--verify", "--end-of-options", explicit + "^{commit}")
            if resolved is None:
                return _run_all(f"{BASE_ENV}={explicit} does not name a commit")
            base, label = resolved.stdout.strip(), f"{BASE_ENV}={explicit}"
        else:
            merge_base = git("merge-base", "HEAD", UPSTREAM)
            head = git("rev-parse", "HEAD")
            if merge_base is None or head is None:
                return _run_all(f"no merge base of HEAD with {UPSTREAM}")
            base = merge_base.stdout.strip()
            if base == head.stdout.strip():
                return _run_all(f"the merge base with {UPSTREAM} equals HEAD")
            label = f"the merge base {base[:12]} with {UPSTREAM}"
        diff = git("diff", "--name-only", "--no-renames", "-z", base, "--")
        untracked = git("ls-files", "-o", "--exclude-standard", "-z")
    except (OSError, subprocess.SubprocessError) as exc:
        return _run_all(f"git failed ({type(exc).__name__})")
    if diff is None or untracked is None:
        return _run_all("git diff or git ls-files failed")
    changed = frozenset(p for p in (diff.stdout + untracked.stdout).split("\0") if p)
    return Selection(changed, label)


def selection_from_list(path: Path) -> Selection:
    lines = path.read_text(encoding="utf-8").splitlines()
    return Selection(frozenset(line.strip() for line in lines if line.strip()), f"the paths listed in {path}")


def item_node(item, root: Path = REPO_ROOT) -> "str | None":
    try:
        rel = Path(item.path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    _, sep, rest = item.nodeid.partition("::")
    return f"{rel}::{rest}" if sep else rel


def entry_for(node: "str | None", entries: "list[Entry]") -> "Entry | None":
    if node is None:
        return None
    return next((e for e in entries if e.matches(node)), None)


def resolve_catalogue_targets(entry: Entry, root: Path = REPO_ROOT) -> "set[str]":
    """Repo-relative paths a catalogue mutates plus the test files that catch them."""
    spec = entry.catalogue_targets or {}
    if not spec:
        return set()
    control = _load_module(root / spec["module"])
    if spec["kind"] == "gc":
        case_root = spec["case_root"]
        return {control.MODULE_RELPATH, control.SWEEP_RELPATH} | {f"{case_root}/{f}" for f in control.CASE_FILES}
    relpath_root, ids_root = spec["relpath_root"], spec["ids_root"]
    paths = set()
    for mutant in control.CATALOGUE.values():
        paths.add(_join(relpath_root, mutant.relpath))
        paths.update(_join(ids_root, node.partition("::")[0]) for node in mutant.ids)
    return paths


def _join(prefix: str, rel: str) -> str:
    return f"{prefix}/{rel}" if prefix else rel


def _load_module(path: Path):
    name = path.stem
    loaded = sys.modules.get(name)
    if loaded is not None and Path(getattr(loaded, "__file__", "") or "").resolve() == path.resolve():
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _MarkerRegistration:
    @staticmethod
    def pytest_configure(config):
        config.addinivalue_line("markers", MARKER_LINE)


def pytest_addoption(parser, pluginmanager):
    parser.addoption(RUN_SLOW_OPTION, action="store_true", default=False,
                     help="run every test listed in scripts/tests/slow_tests.toml, changed or not")
    # conftest re-exports _xdist_auto's pytest_configure, so the marker is registered
    # through a separate plugin rather than a second conftest pytest_configure.
    if not pluginmanager.has_plugin("slow-selection-marker"):
        pluginmanager.register(_MarkerRegistration(), "slow-selection-marker")


_session_selection: "dict[bool, Selection]" = {}


def pytest_collection_modifyitems(config, items):
    import pytest

    entries = load_registry()
    run_all = bool(config.getoption(RUN_SLOW_OPTION))
    for item in items:
        entry = entry_for(item_node(item), entries)
        if entry is None:
            continue
        item.add_marker(pytest.mark.slow)
        if run_all not in _session_selection:
            _session_selection[run_all] = compute_selection(run_all=run_all)
        selection = _session_selection[run_all]
        if not selection.runs(entry):
            item.add_marker(pytest.mark.skip(reason=selection.skip_reason))


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Print the run/skip decision for each slow test.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--base", help=f"compare against REF (as {BASE_ENV}=REF would)")
    source.add_argument("--changed", type=Path, help="read the changed paths from FILE, one per line")
    parser.add_argument("--format", choices=("lines",), default="lines")
    args = parser.parse_args(argv)
    if args.changed is not None:
        selection = selection_from_list(args.changed)
    else:
        env = dict(os.environ)
        if args.base:
            env[BASE_ENV] = args.base
        selection = compute_selection(env=env)
    for entry in load_registry():
        print(f"{'run' if selection.runs(entry) else 'skip'} {entry.node}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
