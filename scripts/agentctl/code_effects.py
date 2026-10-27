"""Opaque code, read: what the code of an opaque Bash segment changes.

Difficulty removed: `segment_resolver` leaves `python3 -c`, a heredoc fed to `python3 -` and
`python3 script.py` OPAQUE -- no argv parser can say what the code writes -- and those three shapes
were most of the ~700 permission stops of the denial scan. The code text is available to the checker,
so it is read before the call instead of refused (or asked about) after it.

Rule/perception split. The model only PERCEIVES: `advisor.judge_code_effects` is given the code as
quoted data and names the file and git-ref changes it sees. Everything that decides is rule:
  * `collect` decides WHICH text is read -- the code itself plus the local-import closure inside the
    venue -- and accepts only code it can show in full, exactly as the interpreter will run it;
  * `effects_of` refuses when anything else on the same command line could change that text (or the
    git branch the judge was told) before the code runs;
  * the judge answer is validated against a closed schema;
  * whether each named effect is covered is `resources.covers`, called by the hook, not here.

FAIL-CLOSED and ALLOW-LIST shaped, end to end: what is accepted is listed, everything else is
UNDETERMINABLE, and there is no path from a judge failure to an empty effect list. Accepted:
  * a python interpreter command line whose options are all in `_SAFE_FLAG_LETTERS` (every other option --
    -I -P -E -S -s -W -X -x ... -- changes the import path or the code that runs, and is not modelled);
  * a hook environment with no PYTHONPATH, PYTHONHOME or PYTHONSAFEPATH;
  * code that touches `sys` only through `_SYS_SAFE` attributes and names no module or builtin of
    `_DYNAMIC_MODULES` / `_DYNAMIC_NAMES` (no import machinery, exec/eval, getattr, chdir, site);
  * each local import found as one regular package (`__init__.py`) or `.py` module in the one local search
    directory (`sys.path[0]`; every other path change is refused, so there is no second directory to
    disagree) -- a bare directory (a namespace package, which an installed package of the same name
    overrides) or a compiled or bytecode sibling is refused;
  * an import that is stdlib, an installed package (listed in the header, text not shown) or a file inside
    the venue; size caps `MAX_FILES` / `MAX_TOTAL_BYTES`.

What the judge sees is exactly what the cache key covers: the prompt data is one blob (`Collected.code`)
holding the venue-relative text of every file of the closure and the list of third-party modules, and
the key is the sha256 of that blob plus the venue, the current branch, the model and the prompt text.
Editing an imported local module therefore changes the key.

The text is read at hook time, not run time. `Collected.files` / `Collected.probed` name every file read and
every path the import search looked at, and `effects_of(segment, line_segments)` returns UNDETERMINABLE
when another segment of the line is not fully resolved or writes (or may write) one of them.

Branch. The judge is told the venue's current branch and the key covers it, so a library commit is
attributed to a branch the checker read rather than guessed; a sibling segment that may switch the branch
(it writes the worktree root, as `git checkout` / `git add` do) makes a vcs_ref answer UNDETERMINABLE.

Injection. The defence is structural: the code is delimited quoted data, the answer must match a closed
schema (an extra field or non-JSON is UNDETERMINABLE) and its effects become typed resources only after
validation. Residual risk, stated plainly: a model that follows an instruction hidden in the code can
still emit a valid but too narrow effects list; nothing here can detect a lie that fits the schema.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from lib import config_root

from . import advisor, resources, segment_resolver, tool_contracts

#: Size caps on what a judge call reads. Over a cap is UNDETERMINABLE, never a truncated read.
MAX_FILES = 8
MAX_TOTAL_BYTES = 32 * 1024

CACHE_DIRNAME = "code-effects-cache"

#: The only interpreter option letters modelled: unbuffered, no .pyc, optimize, quiet. `c` ends the group.
_SAFE_FLAG_LETTERS = frozenset("uBOq")
#: Environment variables that move the import search path or the cwd entry; set in the hook's environment
#: the checker cannot tell what the Bash shell's interpreter will see.
_PATH_ENV_VARS = ("PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH")
#: Modules that import, load or run code by name or path at run time, or change the search path.
_DYNAMIC_MODULES = frozenset({
    "importlib", "runpy", "builtins", "imp", "pkgutil", "zipimport", "site", "sitecustomize", "usercustomize",
})
#: Names (plain or attribute) that run text built at run time, reach modules by string, or move the cwd
#: the `-c` / stdin import entry is relative to.
_DYNAMIC_NAMES = frozenset({
    "exec", "eval", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals", "vars",
    "chdir", "fchdir", "__builtins__", "__loader__", "__spec__", "__path__", "__dict__",
})
#: The only `sys` attributes code may use; `sys.path`, `sys.modules`, `sys.meta_path` ... are not on it.
_SYS_SAFE = frozenset({
    "argv", "exit", "stdin", "stdout", "stderr", "platform", "version", "version_info", "maxsize", "byteorder",
    "executable", "stdlib_module_names",
})
#: Suffixes other than `.py` that Python imports before or beside a source module.
_OTHER_MODULE_SUFFIXES = tuple(s for s in importlib.machinery.all_suffixes() if s != ".py")


class Collected(NamedTuple):
    """The exact text the judge will read, its sha256, and what the text depends on."""

    code: str
    sha256: str
    #: realpaths of every file read (a script included): a write to one of them changes `code`
    files: tuple
    #: every path the import search looked at, found or not: creating one would change what is imported
    probed: tuple
    #: the venue's current branch (the judge is told it and the cache key covers it), or None
    branch: "str | None"


@dataclass(frozen=True)
class Undeterminable:
    """No allow may follow: the effects cannot be named from text that was read in full."""

    reason: str


@dataclass(frozen=True)
class Effects:
    """The judge's validated answer as typed resources (possibly none: the code only reads)."""

    resources: tuple
    code_sha256: str
    cached: bool
    cache_path: str | None


class _Refuse(Exception):
    pass


class _PendingImport(NamedTuple):
    parts: list
    names: list
    label: str
    level: int
    base: "Path | None"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- collect: which text is read ------------------------------------------------------------------

def _check_flags(flags: Sequence[str]) -> None:
    for flag in flags:
        if flag.startswith("--"):
            raise _Refuse(f"the python option {flag!r} is not modelled")
        for letter in flag[1:]:
            if letter == "c":
                break
            if letter not in _SAFE_FLAG_LETTERS:
                raise _Refuse(f"the python option {flag!r} changes the import path or the code that runs, and is not modelled")


def _check_environment() -> None:
    for name in _PATH_ENV_VARS:
        if os.environ.get(name):
            raise _Refuse(f"{name} is set in this environment: the import search path of the interpreter is unknown")


def _check_code(tree: ast.AST, label: str) -> None:
    """Allow-list over what the code may reference: no import machinery, no run-time-built code, `sys` only
    through `_SYS_SAFE`. Anything else could make the interpreter run text that was not read here."""
    def refuse(what: str) -> "_Refuse":
        return _Refuse(f"{label} {what}")

    sys_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _DYNAMIC_MODULES:
                    raise refuse(f"imports {alias.name!r}, which loads or runs code by name")
                if alias.name == "sys":
                    sys_names.add(alias.asname or "sys")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module.split(".")[0] in _DYNAMIC_MODULES:
                raise refuse(f"imports from {module!r}, which loads or runs code by name")
            for alias in node.names:
                if alias.name in _DYNAMIC_NAMES or (alias.name == "sys" and module != "sys") or (
                    module == "sys" and alias.name not in _SYS_SAFE
                ):
                    raise refuse(f"imports {alias.name!r} from {module!r}, which reaches the import machinery or run-time code")
    allowed_sys_uses: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in _DYNAMIC_NAMES or node.attr == "sys":
                raise refuse(f"references {node.attr!r}, which reaches run-time code or the import machinery")
            if isinstance(node.value, ast.Name) and node.value.id in sys_names:
                if node.attr not in _SYS_SAFE:
                    raise refuse(f"uses sys.{node.attr}, which can change what is imported")
                allowed_sys_uses.add(id(node.value))
        elif isinstance(node, ast.Name) and node.id in _DYNAMIC_NAMES:
            raise refuse(f"references {node.id!r}, which runs text built at run time")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in sys_names and id(node) not in allowed_sys_uses:
            raise refuse("uses the sys module other than through a plain attribute read")


class _Closure:
    """The import closure of one Python entry, inside the venue `root`."""

    def __init__(self, root: Path, cwd: Path) -> None:
        self.root = root
        self.cwd = cwd
        self.files: dict[Path, str] = {}
        self.probed: dict[str, None] = {}
        self.third_party: set[str] = set()
        self.total = 0

    def run(self, label: str, text: "str | None", entry_file: "Path | None", search_dir: Path) -> str:
        """Read the entry (`text`, or `entry_file`) and everything it imports; return the blob the judge reads.

        `search_dir` is the only absolute-import directory ahead of the standard library (`sys.path[0]`);
        every other `sys.path` change is refused, so there is never a second local directory to disagree."""
        self.search_dir = search_dir
        entry_real = None
        if entry_file is not None:
            entry_real = Path(os.path.realpath(entry_file))
            text = self.read(entry_real)
        else:
            self.total = len(text.encode("utf-8"))
            if self.total > MAX_TOTAL_BYTES:
                raise _Refuse(f"the code to read exceeds {MAX_TOTAL_BYTES} bytes")
        pending: list[_PendingImport] = []
        self.visit(entry_real, text, label, pending)
        while pending:
            self.resolve(pending.pop(0), pending)
        sections = [f"=== {label} ===\n{text}"]
        sections += [f"=== {self.relative(path)} ===\n{body}" for path, body in self.files.items() if path != entry_real]
        third_party = ", ".join(sorted(self.third_party)) or "none"
        return f"THIRD-PARTY MODULES (text not shown): {third_party}\n" + "\n".join(sections)

    def read(self, path: Path) -> str:
        real = Path(os.path.realpath(path))
        if real in self.files:
            return self.files[real]
        if real != self.root and self.root not in real.parents:
            raise _Refuse(f"{path} lies outside the venue")
        if len(self.files) >= MAX_FILES:
            raise _Refuse(f"the import closure holds more than {MAX_FILES} files")
        try:
            size = real.stat().st_size
            if self.total + size > MAX_TOTAL_BYTES:
                raise _Refuse(f"the code to read exceeds {MAX_TOTAL_BYTES} bytes")
            text = real.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _Refuse(f"{path} cannot be read as text: {exc}") from exc
        self.total += len(text.encode("utf-8"))
        self.files[real] = text
        return text

    def visit(self, path: "Path | None", text: str, label: str, pending: list) -> None:
        """Parse `text`, refuse what the allow-list does not accept, queue its imports."""
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError) as exc:
            raise _Refuse(f"{label} is not parseable Python, so its imports cannot be listed: {exc}") from exc
        _check_code(tree, label)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    pending.append(_PendingImport(alias.name.split("."), [], label, 0, None))
            elif isinstance(node, ast.ImportFrom):
                parts = node.module.split(".") if node.module else []
                pending.append(_PendingImport(
                    parts, [a.name for a in node.names], label, node.level, path.parent if path is not None else None))

    def resolve(self, item: _PendingImport, pending: list) -> None:
        if item.level:
            base = item.base
            if base is None:
                raise _Refuse(f"{item.label} uses a relative import outside a file")
            for _ in range(item.level - 1):
                base = base.parent
            self._follow(item, base, pending, relative=True)
        else:
            self._follow(item, self.search_dir, pending, relative=False)

    def _follow(self, item: _PendingImport, directory: Path, pending: list, *, relative: bool) -> None:
        if not item.parts:
            for name in item.names:
                self._submodule(directory, name, pending)
            return
        current = None
        for index, part in enumerate(item.parts):
            found = self._locate(part, current if current is not None else directory)
            if found is None:
                if index == 0 and not relative:
                    self._external(part, item.label)
                    return
                raise _Refuse(f"{item.label} imports {'.'.join(item.parts)!r}, which cannot be located")
            kind, location = found
            if kind == "package":
                self._include(location / "__init__.py", pending)
                current = location
            else:
                self._include(location, pending)
                current = None
                break
        if current is not None:
            for name in item.names:
                self._submodule(current, name, pending)

    def _submodule(self, directory: Path, name: str, pending: list) -> None:
        found = self._locate(name, directory)
        if found is None:
            return
        kind, location = found
        self._include(location / "__init__.py" if kind == "package" else location, pending)

    def _locate(self, name: str, directory: Path) -> "tuple[str, Path] | None":
        """The one regular package or `.py` module `name` is in `directory`, None when absent; a bare
        directory or a compiled / bytecode sibling is refused (what runs then is not text read here)."""
        for suffix in ("", ".py", *_OTHER_MODULE_SUFFIXES):
            self.probed[str(directory / f"{name}{suffix}")] = None
        if (directory / name / "__init__.py").is_file():
            return "package", directory / name
        if any((directory / f"{name}{suffix}").exists() for suffix in _OTHER_MODULE_SUFFIXES):
            raise _Refuse(f"{name!r} has a compiled or bytecode module beside it, whose text cannot be read")
        if (directory / f"{name}.py").is_file():
            return "module", directory / f"{name}.py"
        if (directory / name).is_dir():
            raise _Refuse(f"{name!r} matches a bare directory (a namespace package), which an installed package of that name overrides")
        return None

    def _include(self, path: Path, pending: list) -> None:
        real = Path(os.path.realpath(path))
        if real in self.files:
            return
        self.visit(real, self.read(path), self.relative(real), pending)

    def _external(self, top: str, label: str) -> None:
        if top in sys.stdlib_module_names:
            return
        try:
            spec = importlib.util.find_spec(top)
        except (ImportError, ValueError, AttributeError):
            spec = None
        origin = str(getattr(spec, "origin", "") or "")
        if spec is not None and ("site-packages" in origin or "dist-packages" in origin):
            self.third_party.add(top)
            return
        raise _Refuse(f"{label} imports {top!r}, which is neither the standard library, an installed package nor a file inside the venue")

    def relative(self, path: Path) -> str:
        real = Path(os.path.realpath(path))
        try:
            return os.path.relpath(real, self.cwd)
        except ValueError:
            return str(real)


def collect(segment: tool_contracts.Segment) -> "Collected | Undeterminable":
    """The text a judge must read for an opaque `segment`, or why it cannot be read.

    python-c: the -c text. stdin-body: the heredoc / here-string body. script-file: the script. Each with
    the local-import closure inside the venue (a git worktree root, else the segment's cwd). Everything
    outside the allow-list in the module docstring is UNDETERMINABLE."""
    if segment.status != "opaque" or not segment.venue:
        return Undeterminable("the segment is not opaque in a known working directory")
    try:
        _check_environment()
        source = segment_resolver.code_source(segment.text)
        if source.kind != segment.opaque_kind:
            raise _Refuse("the segment kind and its command line disagree")
        _check_flags(source.flags)
        cwd = Path(os.path.realpath(segment.venue))
        root = Path(os.path.realpath(segment_resolver.worktree_root(str(cwd)) or cwd))
        closure = _Closure(root, cwd)
        if source.script is None:
            label = "python -c code" if source.kind == "python-c" else "python stdin code"
            blob = closure.run(label, source.code, None, cwd)
        else:
            script = Path(source.script)
            script = Path(os.path.realpath(script if script.is_absolute() else cwd / script))
            blob = closure.run(closure.relative(script), None, script, script.parent)
    except (_Refuse, segment_resolver.CodeSourceError) as exc:
        return Undeterminable(str(exc))
    except (OSError, UnicodeError) as exc:
        return Undeterminable(f"the code cannot be read: {exc}")
    return Collected(
        blob, _sha256(blob), tuple(str(path) for path in closure.files), tuple(closure.probed),
        segment_resolver.head_branch(str(root)))


# --- the rest of the command line ---------------------------------------------------------------------

def _overlap(a: str, b: str) -> bool:
    """`a` equals `b`, contains it, or lies inside it."""
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def _writes(segment: tool_contracts.Segment) -> "list[str]":
    return [r.path for r in segment.resources if isinstance(r, resources.FileResource) and r.mode == "write"]


def _others(segment: tool_contracts.Segment, line_segments: Sequence[tool_contracts.Segment]) -> "list[tool_contracts.Segment]":
    return [other for other in line_segments if other is not segment]


def line_conflict(
    collected: Collected, segment: tool_contracts.Segment, line_segments: Sequence[tool_contracts.Segment] = (),
) -> "str | None":
    """Why the text in `collected` may differ from the code that runs, or None.

    The text is read now; the line runs later. Every OTHER segment must be resolved (an opaque or
    unresolved one may write anything), and no write of the line -- the opaque segment's own redirections
    included, they run before the interpreter -- may equal or contain, or lie inside, a file that was read
    or a path the import search looked at."""
    for other in _others(segment, line_segments):
        if other.status != "resolved":
            return f"another segment of the line is {other.status} ({other.text[:60]!r}): what it writes before the code runs is unknown"
    watched = [*collected.files, *collected.probed]
    for owner in (segment, *_others(segment, line_segments)):
        for path in _writes(owner):
            hit = next((w for w in watched if _overlap(path, w)), None)
            if hit is not None:
                return f"the line writes {path}, which the code reads or imports ({hit}): the text read here may not be the text that runs"
    return None


def _may_switch_branch(root: str, segment: tool_contracts.Segment, line_segments: Sequence[tool_contracts.Segment]) -> bool:
    return any(_overlap(path, root) and root.startswith(path) for other in _others(segment, line_segments) for path in _writes(other))


# --- the cache -------------------------------------------------------------------------------------

def cache_key(code_sha256: str, venue: str, branch: "str | None" = None) -> str:
    """One verdict per (code bytes, venue, branch, model, prompt text): any of them changing changes the key."""
    return _sha256("\0".join([
        advisor.CODE_EFFECTS_PROMPT_SHA256, advisor.CODE_EFFECTS_MODEL, venue, branch or "", code_sha256]))


def _protected_cache_dir(directory: Path) -> "Path | None":
    """The cache directory, created 0700; None when it is a symlink or owned by another uid (a poisoned
    cache could hand the hook an `allow`, so an untrusted directory disables caching, never the check)."""
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        return None
    if info.st_mode & 0o077:
        try:
            os.chmod(directory, 0o700)
        except OSError:
            return None
    return directory


def _cache_read(path: Path, code_sha256: str, venue: str, branch: "str | None") -> "advisor.CodeEffectsVerdict | None":
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or (record.get("code_sha256"), record.get("venue"), record.get("branch")) != (
        code_sha256, venue, branch,
    ):
        return None
    verdict = advisor.validate_code_effects_answer(record.get("answer"))
    return verdict if verdict.answered else None


def _cache_write(path: Path, code_sha256: str, venue: str, branch: "str | None", answer: dict) -> bool:
    record = json.dumps({"code_sha256": code_sha256, "venue": venue, "branch": branch, "answer": answer}, sort_keys=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(record)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def _answer_dict(verdict: "advisor.CodeEffectsVerdict") -> dict:
    return {
        "effects": list(verdict.effects or ()), "undeterminable": verdict.effects is None, "reason": verdict.reason,
    }


def _to_resources(effects, venue: str) -> tuple:
    out = []
    for effect in effects:
        if effect["kind"] == "file":
            path = effect["path"]
            out.append(resources.FileResource(path if os.path.isabs(path) else os.path.join(venue, path), effect["op"]))
        else:
            out.append(resources.VcsRefResource(effect["remote"], effect["ref"], effect["op"]))
    return tuple(dict.fromkeys(out))


def effects_of(
    segment: tool_contracts.Segment,
    line_segments: Sequence[tool_contracts.Segment] = (),
    *,
    runner=None,
    cache_dir: "Path | str | None" = None,
    timeout: int | None = None,
) -> "Effects | Undeterminable":
    """The typed resources the opaque `segment`'s code changes, or UNDETERMINABLE.

    `line_segments` are all segments of the command line (`segment` among them, by identity); the hook
    passes them so that a sibling that rewrites the code, or may move the branch, is not overlooked.
    collect -> line check -> cache -> judge -> validated effects -> typed resources. Only an ANSWERED
    verdict is cached (a timeout or a malformed answer is not an answer and must be asked again).
    The segment's own redirections are not part of the result: they are in `segment.resources`."""
    collected = collect(segment)
    if isinstance(collected, Undeterminable):
        return collected
    blocked = line_conflict(collected, segment, line_segments)
    if blocked is not None:
        return Undeterminable(blocked)
    venue = os.path.realpath(segment.venue)
    branch = collected.branch
    directory = _protected_cache_dir(Path(cache_dir) if cache_dir is not None else config_root.agentctl_state_dir() / CACHE_DIRNAME)
    path = directory / f"{cache_key(collected.sha256, venue, branch)}.json" if directory is not None else None
    verdict = _cache_read(path, collected.sha256, venue, branch) if path is not None else None
    cached = verdict is not None
    stored = cached
    if verdict is None:
        verdict = advisor.judge_code_effects(collected.code, venue, runner, timeout=timeout, branch=branch)
        if verdict.answered and path is not None:
            stored = _cache_write(path, collected.sha256, venue, branch, _answer_dict(verdict))
    if verdict.effects is None:
        return Undeterminable(verdict.reason)
    named = _to_resources(verdict.effects, venue)
    if any(isinstance(r, resources.VcsRefResource) for r in named):
        root = os.path.realpath(segment_resolver.worktree_root(venue) or venue)
        if _may_switch_branch(root, segment, line_segments):
            return Undeterminable("another segment of the line may switch the git branch the code's commit is attributed to")
    return Effects(named, collected.sha256, cached, str(path) if stored and path is not None else None)
