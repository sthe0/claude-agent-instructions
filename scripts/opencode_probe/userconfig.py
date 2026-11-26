"""Snapshot and compare of the user's own OpenCode directories.

The probe never writes there. Config directories are byte-compared against a snapshot taken
before the first OpenCode run. The data, state and cache directories hold what the user's own
OpenCode service writes at any time, so they are not byte-compared: ``user-config --compare``
fails when any file in them holds a probe run-root path, which a leaked probe session records.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import core

SNAPSHOT_NAME = "user-config.sha256"
DATA_DIRS = (".local/share/opencode", ".local/state/opencode", ".cache/opencode")
CHUNK = 1 << 20


def user_dirs(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    return [home / ".config" / "opencode", home / ".opencode"]


def data_dirs(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    return [home / rel for rel in DATA_DIRS]


def probe_root_needles() -> tuple[bytes, ...]:
    """The run-root prefix as the state dir is spelled and as it resolves: ``make_root`` resolves
    its path, so a leaked session may hold either spelling."""
    state = core.state_dir()
    return tuple(sorted({str(base / "runs" / "root.").encode() for base in (state, state.resolve())}))


def _holds(path: Path, needles: tuple[bytes, ...]) -> bool:
    keep = max(len(n) for n in needles) - 1
    tail = b""
    with path.open("rb") as handle:
        while block := handle.read(CHUNK):
            window = tail + block
            if any(n in window for n in needles):
                return True
            tail = block[-keep:]
    return False


def leaked_probe_paths(home: Path | None = None, needle: bytes | tuple[bytes, ...] | None = None) -> list[str]:
    """Files under the user's data, state and cache directories that name a probe run root."""
    needles = (needle,) if isinstance(needle, bytes) else needle or probe_root_needles()
    leaked = []
    for base in data_dirs(home):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.is_symlink() and _holds(path, needles):
                leaked.append(str(path.relative_to(home or Path.home())))
    return leaked


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
    leaked = leaked_probe_paths(home)
    if changed or added or removed or leaked:
        for label, names in (("changed", changed), ("added", added), ("removed", removed),
                             ("probe path found in", leaked)):
            for name in names[:10]:
                print(f"user-config compare: {label}: {name}")
        return 1
    print(f"user-config compare: {len(actual)} config files byte-identical to the snapshot; "
          "no probe path in the data, state or cache dirs")
    return 0
