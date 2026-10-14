"""Content-addressed store of plan files (ADR-0011).

A hypertext plan names the files it is made of by relative path, so an older version of it
(an approved snapshot, a submitted version) read later from another directory, or after a
referenced file was edited, moved or deleted, would resolve against the wrong bytes. The
store keeps every file of every plan version the engine accepted under its own sha256, in
`<plans_dir>/objects/`, and an older version is loaded by resolving its references against
the directory of the session's registered plan path with a reader that serves the bytes
from the store by hash: what a version means no longer depends on what is on disk.
"""
from __future__ import annotations

import hashlib
import os
import tomllib
from pathlib import Path

from . import plan_refs
from .plan import PlanDoc, PlanError, load_plan


OBJECTS_DIR_ENV = "AGENTCTL_PLAN_OBJECTS_DIR"


def objects_dir() -> Path:
    from lib import config_root

    override = os.environ.get(OBJECTS_DIR_ENV)
    return Path(override) if override else config_root.plans_dir() / "objects"


_HEX = frozenset("0123456789abcdef")


def _object_path(sha256: str, directory: Path | None) -> Path:
    # The name becomes a path: anything but a sha256 hex digest could leave the store.
    if len(sha256) != 64 or not _HEX.issuperset(sha256):
        raise PlanError(f"{sha256!r} is not a sha256 hex digest")
    return (directory or objects_dir()) / sha256[:2] / sha256


def _intact(target: Path, digest: str) -> bool:
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest() == digest
    except OSError:
        return False


def put(data: bytes, *, directory: str | Path | None = None) -> str:
    """Store `data` under its sha256 (idempotent) and return the digest. An object already
    there whose bytes do not hash to its name (a write torn by a crash) is rewritten."""
    digest = hashlib.sha256(data).hexdigest()
    target = _object_path(digest, Path(directory) if directory else None)
    if _intact(target, digest):
        return digest
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.{os.getpid()}.partial")
    try:
        partial.write_bytes(data)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return digest


def get(sha256: str, *, directory: str | Path | None = None) -> bytes:
    """The bytes stored under `sha256`; a `PlanError` naming the hash when the object is
    absent or does not hash to its name."""
    target = _object_path(sha256, Path(directory) if directory else None)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise PlanError(f"plan object {sha256} is not in the store {target.parent.parent}: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != sha256:
        raise PlanError(f"plan object {sha256} in the store {target.parent.parent} is corrupt: "
                        "its bytes do not hash to its name")
    return data


def reader(directory: str | Path | None = None, *, fall_back_to_disk: bool = False):
    """A `read(path, sha256) -> bytes` callable for `plan_refs.resolve_refs`. It serves
    bytes from the store by hash and ignores `path`, so a missing object is refused
    rather than answered from whatever file the path names now. `fall_back_to_disk` is
    for storing a version that is still on disk, where the resolver's own hash check
    guards the bytes."""
    def read(path: Path, sha256: str) -> bytes:
        try:
            return get(sha256, directory=directory)
        except PlanError:
            if not fall_back_to_disk:
                raise
        try:
            return Path(path).read_bytes()
        except OSError as exc:
            raise plan_refs._Unreadable(f"cannot read the referenced file {path}: {exc}") from exc
    return read


def store_plan(data: bytes, registered_path: str | Path, *,
               directory: str | Path | None = None) -> list[str]:
    """Put a plan version's index bytes and every file it references into the store and
    return their digests. `registered_path` is the plan path the session registered: the
    references in `data` resolve relative to its directory, whichever file `data` came from."""
    registered = Path(registered_path)
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PlanError(f"malformed TOML in plan version of {registered}: {exc}") from exc
    seen: dict[str, bytes] = {}
    disk_or_store = reader(directory, fall_back_to_disk=True)

    def collect(path: Path, sha256: str) -> bytes:
        seen[sha256] = disk_or_store(path, sha256)
        return seen[sha256]

    plan_refs.resolve_refs(raw, registered.parent, None, read=collect, index_path=registered)
    return [put(data, directory=directory)] + [put(b, directory=directory) for b in seen.values()]


def load_baseline(path: str | Path, registered_path: str | Path | None, *,
                  strict: bool = True, directory: str | Path | None = None) -> PlanDoc:
    """Load an older version of the session's plan (a snapshot). References resolve
    relative to the registered plan path and their bytes come from the store. The
    registered plan itself, or a session with no registered path, loads from disk."""
    if not registered_path or Path(path).resolve() == Path(registered_path).resolve():
        return load_plan(path, strict=strict)
    registered = Path(registered_path)
    return load_plan(
        path, strict=strict, base_dir=registered.parent,
        root=plan_refs.default_root(registered), read=reader(directory))


def visited_refs(plan_path: str | Path) -> plan_refs.Visited:
    """`(position, resolved path, sha256)` of every reference of the plan at `plan_path`,
    in document order."""
    index = Path(plan_path)
    try:
        raw = tomllib.loads(index.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PlanError(f"cannot read plan file {index}: {exc}") from exc
    return plan_refs.resolve_refs(raw, index.parent, None, index_path=index)[1]
