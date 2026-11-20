"""Snapshot and compare of the user's own OpenCode config directories.

The probe never writes there; the snapshot, taken before the first OpenCode run, lets
``user-config --compare`` show that it did not.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from . import core

SNAPSHOT_NAME = "user-config.sha256"


def user_dirs(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    return [home / ".config" / "opencode", home / ".opencode"]


def digest_tree(home: Path | None = None) -> dict[str, str]:
    """{path relative to home: sha256 of the bytes} for every file of the user's OpenCode
    config directories, ``~/.opencode/bin`` excluded (it holds the installed binary)."""
    home = home or Path.home()
    out: dict[str, str] = {}
    for base in user_dirs(home):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(home)
            if rel.parts[:2] == (".opencode", "bin"):
                continue
            if path.is_file() and not path.is_symlink():
                out[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
            elif path.is_symlink():
                out[str(rel)] = "symlink:" + str(path.readlink())
    return out


def real_config_key_names(home: Path | None = None) -> list[str]:
    """Top-level key names (never values) of the user's global OpenCode config file, so a
    case can shape its stand-in after it; empty when there is none or it is not plain JSON."""
    import json

    home = home or Path.home()
    for name in ("opencode.json", "opencode.jsonc"):
        path = home / ".config" / "opencode" / name
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                return []
            return sorted(data) if isinstance(data, dict) else []
    return []


def list_top_level(home: Path | None = None) -> int:
    """Top-level entries of the user's OpenCode config directories with file counts."""
    from collections import Counter

    counts = Counter("/".join(Path(rel).parts[:3]) for rel in digest_tree(home))
    for name, count in sorted(counts.items()):
        print(f"{count:6d}  {name}")
    return 0


def show_dependency_file(rel: str, home: Path | None = None) -> int:
    """Print a file under ``~/.opencode/node_modules`` (installed plugin type definitions,
    no credentials live there); with a directory, print its file names."""
    home = home or Path.home()
    base = (home / ".opencode" / "node_modules").resolve()
    path = (base / rel).resolve()
    if base not in path.parents and path != base:
        print("show: path outside ~/.opencode/node_modules")
        return 1
    if path.is_dir():
        for child in sorted(path.rglob("*")):
            if child.is_file():
                print(child.relative_to(base))
        return 0
    sys.stdout.write(path.read_text(encoding="utf-8", errors="replace"))
    return 0


def snapshot_path() -> Path:
    return core.state_dir() / SNAPSHOT_NAME


def _render(digests: dict[str, str]) -> str:
    return "".join(f"{sha}  {rel}\n" for rel, sha in sorted(digests.items()))


def write_snapshot(home: Path | None = None) -> int:
    digests = digest_tree(home)
    path = snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(digests), encoding="utf-8")
    print(f"user-config snapshot: {len(digests)} files -> {path}")
    return 0


def compare(home: Path | None = None) -> int:
    path = snapshot_path()
    if not path.is_file():
        print(f"user-config compare: no snapshot at {path}; run `user-config --snapshot` "
              "before the first OpenCode run")
        return 1
    expected = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        sha, _, rel = line.partition("  ")
        expected[rel] = sha
    actual = digest_tree(home)
    changed = sorted(rel for rel in expected.keys() & actual.keys() if expected[rel] != actual[rel])
    added = sorted(actual.keys() - expected.keys())
    removed = sorted(expected.keys() - actual.keys())
    if changed or added or removed:
        for label, names in (("changed", changed), ("added", added), ("removed", removed)):
            for name in names[:10]:
                print(f"user-config compare: {label}: {name}")
        return 1
    print(f"user-config compare: {len(actual)} files byte-identical to the snapshot")
    return 0
