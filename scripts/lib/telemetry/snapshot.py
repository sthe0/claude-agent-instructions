"""A frozen copy of the import sources, and reading the sources through it.

Difficulty removed: the import sources grow while this machine's sessions run, so two imports
of "the sources" read two different inputs and cannot show the importer idempotent. `take` copies
every source a full import reads once — each append-only ledger and transcript cut at the size it
had when measured, each engine state file whole — and writes a manifest mapping every copy to the
live path it came from. While a snapshot is active (`import --sources DIR`), the importer and the
token collector list sources from the manifest and open the copies, but name every row, offset
and back-reference by the live path, so a later live import of the same lines adds nothing.

`take(since_s=…)` copies only the sources written since a landing, and `check_space` refuses
before any copy unless the free space also covers twice the live record; reconcile.py compares an
import of such a slice with the live record.

Spools are not part of a snapshot: an import run on a scratch record points
AGENTCTL_TELEMETRY_SPOOL at a scratch directory of its own.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

MANIFEST = "manifest.json"
FREE_SPACE_MARGIN_BYTES = 64 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
LEDGER, STATE, TRANSCRIPT = "ledger", "state", "transcript"


class SnapshotError(Exception):
    pass


def _sources() -> "list[tuple[str, Path]]":
    from lib import config_root
    from lib.telemetry import importer, tokens
    found = {}
    for unit in importer._file_units():
        found.setdefault(unit.path, (LEDGER, Path(unit.path)))
    try:
        for path in sorted(config_root.agentctl_state_dir().glob("*.json")):
            found.setdefault(str(path), (STATE, path))
    except OSError:
        pass
    for path, _, _ in tokens.transcripts():
        found.setdefault(str(path), (TRANSCRIPT, path))
    return [found[key] for key in sorted(found)]


def _copy_prefix(source: Path, target: Path, size: int) -> None:
    remaining = size
    with open(source, "rb") as src, open(target, "wb") as dst:
        while remaining > 0:
            chunk = src.read(min(COPY_CHUNK_BYTES, remaining))
            if not chunk:
                break
            dst.write(chunk)
            remaining -= len(chunk)


class InsufficientSpace(SnapshotError):
    def __init__(self, free: int, need: int, where) -> None:
        super().__init__(f"insufficient_space: {need} bytes needed on {where}, {free} free")
        self.free, self.need = free, need


def take(out_dir, since_s: "float | None" = None, check_space: bool = False) -> dict:
    """Copy every import source into `out_dir` (which must not exist yet) and write its manifest.

    `since_s`: only the sources last written at or after this epoch time, each still cut at the
    size it had when measured. `check_space`: refuse before any copy (InsufficientSpace) unless
    the free bytes cover the copies plus twice the live record, room for a scratch record an
    import of the copies builds. Raises SnapshotError when the filesystem lacks room."""
    from lib import config_root
    out = Path(out_dir)
    if out.exists():
        raise SnapshotError(f"{out} already exists")
    measured, skipped = [], []
    for kind, path in _sources():
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        except OSError as err:
            skipped.append({"live": str(path), "error": repr(err)})
        else:
            if since_s is None or stat.st_mtime >= since_s:
                measured.append((kind, path, stat.st_size))
    cut = sum(size for _, _, size in measured)
    free = shutil.disk_usage(out.parent).free
    if check_space:
        record = config_root.telemetry_db()
        need = cut + 2 * (record.stat().st_size if record.exists() else 0)
        if free < need:
            raise InsufficientSpace(free, need, out.parent)
    need = cut + FREE_SPACE_MARGIN_BYTES
    if free < need:
        raise SnapshotError(f"{need} bytes needed on {out.parent}, {free} free")
    (out / "files").mkdir(parents=True)
    files = []
    for number, (kind, path, size) in enumerate(measured):
        copy = Path("files") / f"{number:06d}{path.suffix}"
        try:
            if kind == STATE:
                shutil.copyfile(path, out / copy)
            else:
                _copy_prefix(path, out / copy, size)
            size = (out / copy).stat().st_size
        except OSError as err:
            skipped.append({"live": str(path), "error": repr(err)})
            continue
        files.append({"kind": kind, "live": str(path), "copy": str(copy), "size": size})
    manifest = {"taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": files,
                "skipped": skipped}
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return {"out": str(out), "files": len(files), "bytes": sum(f["size"] for f in files), "skipped": skipped}


class Snapshot:
    def __init__(self, root) -> None:
        self.root = Path(root)
        manifest = json.loads((self.root / MANIFEST).read_text(encoding="utf-8"))
        self.files = {entry["live"]: entry for entry in manifest["files"]}

    def copy_of(self, live_path) -> "Path | None":
        entry = self.files.get(str(live_path))
        return self.root / entry["copy"] if entry else None

    def live_paths(self, kind: str) -> "list[Path]":
        return sorted(Path(live) for live, entry in self.files.items() if entry["kind"] == kind)


_active: "Snapshot | None" = None


def activate(root) -> Snapshot:
    global _active
    _active = Snapshot(root)
    return _active


def deactivate() -> None:
    global _active
    _active = None


def active() -> "Snapshot | None":
    return _active


def readable_path(live_path) -> "str | None":
    """The file to open for a source named by its live path: the live path itself without a
    snapshot, its copy with one, None when the snapshot holds no copy of it."""
    if _active is None:
        return str(live_path)
    copy = _active.copy_of(live_path)
    return str(copy) if copy is not None else None


def open_source(live_path):
    """open(…, "rb") of the file `readable_path` names; FileNotFoundError when there is none."""
    path = readable_path(live_path)
    if path is None:
        raise FileNotFoundError(os.fspath(live_path))
    return open(path, "rb")
