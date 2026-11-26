"""Locations, phase discovery and the per-phase suite hash of the OpenCode probe suite.

A phase is a subdirectory of the probe directory whose ``__init__.py`` assigns
``NEGATIVE_ANCHOR``. Phases are found by reading those files as text (``ast``), never by
importing them, so a phase added as a directory needs no edit of the shared core and the
hash can be computed without running any case.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROBE_DIR.parent.parent
PIN_NAME = "pin.toml"
DEFAULT_OPENCODE_BIN = Path.home() / ".opencode" / "bin" / "opencode"


class InternalError(Exception):
    """A defect of the suite or its tree, not an observation about OpenCode (exit 2)."""


def state_dir() -> Path:
    """The probe state directory; ``AGENT_OPENCODE_PROBE_STATE`` overrides the default."""
    override = os.environ.get("AGENT_OPENCODE_PROBE_STATE")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "opencode-probe"


def opencode_bin() -> str:
    return os.environ.get("AGENT_OPENCODE_BIN") or str(DEFAULT_OPENCODE_BIN)


def binary_problem() -> str | None:
    """Why the binary under test cannot be launched, or None."""
    binary = opencode_bin()
    if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        return f"opencode live: binary {binary} is not an executable file"
    return None


def read_pin(probe_dir: Path = PROBE_DIR) -> dict:
    path = probe_dir / PIN_NAME
    if not path.is_file():
        raise InternalError(f"{path} is missing")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _module_constants(path: Path, names: tuple[str, ...]) -> dict:
    """Literal values of the module-level assignments to ``names`` (read, not imported)."""
    found: dict = {}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in names:
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    raise InternalError(f"{path}: {target.id} must be a literal")
    return found


@dataclass(frozen=True)
class Phase:
    name: str
    path: Path
    anchor: str
    subject_files: tuple[str, ...]
    case_files: tuple[Path, ...] = field(default=())


def discover_phases(probe_dir: Path = PROBE_DIR) -> list[Phase]:
    """Every subdirectory declaring NEGATIVE_ANCHOR; ``mechanisms`` first, the rest by name."""
    phases = []
    for child in sorted(probe_dir.iterdir()):
        init = child / "__init__.py"
        if not child.is_dir() or not init.is_file():
            continue
        consts = _module_constants(init, ("NEGATIVE_ANCHOR", "SUBJECT_FILES"))
        if "NEGATIVE_ANCHOR" not in consts:
            continue
        cases = tuple(sorted(p for p in child.glob("c_*.py")))
        phases.append(Phase(child.name, child, consts["NEGATIVE_ANCHOR"],
                            tuple(consts.get("SUBJECT_FILES", ())), cases))
    phases.sort(key=lambda p: (p.name != "mechanisms", p.name))
    return phases


def get_phase(name: str, probe_dir: Path = PROBE_DIR) -> Phase:
    for phase in discover_phases(probe_dir):
        if phase.name == name:
            return phase
    raise InternalError(f"no phase {name!r} in {probe_dir}")


def select_phases(selector: str, probe_dir: Path = PROBE_DIR) -> list[Phase]:
    if selector == "all":
        return discover_phases(probe_dir)
    return [get_phase(selector, probe_dir)]


@dataclass(frozen=True)
class CaseInfo:
    case_id: str
    path: Path
    k7: bool
    rests_on_plugin_deny: bool
    predicate: str


def case_info(path: Path) -> CaseInfo:
    consts = _module_constants(path, ("CASE_ID", "K7", "REST_ON_PLUGIN_DENY"))
    for key in ("CASE_ID", "K7"):
        if key not in consts:
            raise InternalError(f"{path}: {key} is missing")
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    return CaseInfo(consts["CASE_ID"], path, bool(consts["K7"]),
                    bool(consts.get("REST_ON_PLUGIN_DENY", False)), doc)


def phase_cases(phase: Phase) -> list[CaseInfo]:
    return [case_info(p) for p in phase.case_files]


def load_case(info: CaseInfo):
    """The case module, imported from its file."""
    spec = importlib.util.spec_from_file_location(f"opencode_probe_case_{info.case_id}", info.path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hash_files(root: Path, files: list[Path], digest) -> None:
    for path in sorted(files):
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")


def _tree_files(directory: Path) -> list[Path]:
    return [p for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.parts
            and p.suffix != ".pyc"]


def shared_core_files(probe_dir: Path = PROBE_DIR) -> list[Path]:
    """Every file of the probe directory (stub, record code, CLI, isolated-run helper, the
    base plugin and asset directories) except the pin and the phase packages."""
    phase_dirs = {phase.path for phase in discover_phases(probe_dir)}
    files = []
    for path in probe_dir.iterdir():
        if path.is_file() and path.name != PIN_NAME and path.suffix != ".pyc":
            files.append(path)
        elif path.is_dir() and path not in phase_dirs and path.name != "__pycache__":
            files.extend(_tree_files(path))
    return files


def phase_hash(phase: Phase, probe_dir: Path = PROBE_DIR, repo_root: Path | None = None) -> str:
    """sha256 over the phase's own files, the shared core and the phase's subject files."""
    repo_root = repo_root or probe_dir.parent.parent
    digest = hashlib.sha256()
    digest.update(b"core\0")
    _hash_files(probe_dir, shared_core_files(probe_dir), digest)
    digest.update(b"phase\0" + phase.name.encode() + b"\0")
    _hash_files(phase.path, _tree_files(phase.path), digest)
    digest.update(b"subject\0")
    for rel in sorted(phase.subject_files):
        subject = repo_root / rel
        if not subject.is_file():
            raise InternalError(f"subject file {rel} of phase {phase.name} is missing")
        digest.update(rel.encode() + b"\0" + subject.read_bytes() + b"\0")
    return digest.hexdigest()
