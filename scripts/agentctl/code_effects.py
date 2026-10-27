"""Opaque code, read: what the code of an opaque Bash segment changes.

Difficulty removed: `segment_resolver` leaves `python3 -c`, a heredoc fed to `python3 -` and
`python3 script.py` OPAQUE -- no argv parser can say what the code writes -- and those three shapes
were most of the ~700 permission stops of the denial scan. The code text is available to the checker,
so it is read before the call instead of refused (or asked about) after it.

Rule/perception split. The model only PERCEIVES: `advisor.judge_code_effects` is given the code as
quoted data and names the file and git-ref changes it sees. Everything that decides is rule:
  * `collect` decides WHICH text is read: the code itself and nothing else. There is no import closure --
    a local module is never read, so any import that could resolve to one is UNDETERMINABLE;
  * `effects_of` refuses when anything else on the same command line (or the code's own writes) could
    change that text, or what an import in it resolves to, before it runs;
  * the judge answer is validated against a closed schema;
  * whether each named effect is covered is `resources.covers`, called by the hook, not here.

FAIL-CLOSED, with ALLOW-LISTS as the static prefilter: what is accepted is listed, everything else is
UNDETERMINABLE, and there is no path from a judge failure to an empty effect list. Accepted:
  * a python interpreter command line whose options are all in `_SAFE_FLAG_LETTERS` (every other option --
    -I -P -E -S -s -W -X -x -O ... -- changes the import path or what the code does, and is not modelled);
  * a hook environment with no PYTHONPATH, PYTHONHOME or PYTHONSAFEPATH;
  * source in utf-8 only (a PEP 263 cookie naming another encoding changes the text the interpreter runs);
  * code that imports only top-level modules on `_IMPORTABLE` (standard-library modules whose file and git
    effects the judge can name from the source; `subprocess` of anything but a literal git commit or push
    is the judge's undeterminable), by absolute, non-star imports, and touches `sys` only through
    `_SYS_SAFE`, names no `_DYNAMIC_NAMES` entry, no private (`_x`) attribute and no dunder outside
    `_DUNDER_SAFE`;
  * a search directory (`sys.path[0]`: the cwd for -c and stdin, the script's directory for a script) with
    no entry named like a standard-library module -- such a file would be run instead of the library
    module of that name, for a direct import and for the library's own imports alike.

What the judge sees is exactly what the cache key covers: the prompt data is `Collected.code`, the key
is the sha256 of it plus the venue, the current branch, the model and the prompt text.

The text is read at hook time, not run time. `Collected.files` is the script read; `write_hit` says whether
a write (a sibling segment's, an own redirection's, or one the judge names in the code itself) would change
what was read, and `effects_of` returns UNDETERMINABLE then, and when another segment of the line is not
fully resolved.

Branch. The judge is told the venue's current branch and the key covers it, so a library commit is
attributed to a branch the checker read rather than guessed; a sibling segment that may switch the branch
(it writes the worktree root, as `git checkout` / `git add` do) makes a vcs_ref answer UNDETERMINABLE.

Residual risk, stated plainly. The prefilter is a static allow-list; the judge perceives the rest. A model
that follows an instruction hidden in the code can still emit a valid but too narrow effects list, and a
call the judge misreads (a `subprocess`, `os.system` or `os.exec*` of a program it takes for harmless) is
not caught by anything here. Not modelled either: the interpreter's own implicit imports of a code with no
import statement, and a PYTHONPATH exported by the Bash shell's rc file (invisible to the hook; stage 8 reads
it from the shell snapshot).
"""
from __future__ import annotations

import ast
import codecs
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from lib import config_root

from . import advisor, resources, segment_resolver, tool_contracts

#: Size cap on the code a judge call reads. Over it is UNDETERMINABLE, never a truncated read.
MAX_TOTAL_BYTES = 32 * 1024

CACHE_DIRNAME = "code-effects-cache"

#: The only interpreter option letters modelled: unbuffered, no .pyc, quiet. `c` ends the group.
_SAFE_FLAG_LETTERS = frozenset("uBq")
#: Environment variables that move the import search path or the cwd entry; set in the hook's environment
#: the checker cannot tell what the Bash shell's interpreter will see.
_PATH_ENV_VARS = ("PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH")
#: Source encodings the interpreter and this reader agree on.
_SOURCE_ENCODINGS = ("utf-8", "utf-8-sig")
#: The top-level modules code may import: standard-library modules whose file and git effects the judge can
#: name from the source. Everything else (operator, pickle, ctypes, types, importlib, runpy, builtins, every
#: local and third-party name ...) is not on it.
_IMPORTABLE = frozenset({
    "__future__", "os", "sys", "pathlib", "shutil", "json", "re", "io", "tempfile", "glob", "fnmatch",
    "datetime", "time", "collections", "itertools", "functools", "string", "textwrap", "math", "hashlib",
    "base64", "csv", "argparse", "subprocess", "enum", "tomllib",
})
# `typing` and `dataclasses` are left out on purpose: `typing.get_type_hints` evaluates a `ForwardRef`
# built from run-time text, so either module can run code that was never read here.
#: Names (plain or attribute) that run text built at run time, reach modules or attributes by string, or
#: move the cwd the `-c` / stdin import entry is relative to.
_DYNAMIC_NAMES = frozenset({
    "exec", "eval", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals", "vars",
    "chdir", "fchdir", "breakpoint",
})
#: The only dunders code may spell; every other dunder (`__class__`, `__dict__`, `__builtins__` ...) reaches
#: objects the judge cannot see.
_DUNDER_SAFE = frozenset({"__name__", "__init__"})
#: The only `sys` attributes code may use; `sys.path`, `sys.modules`, `sys.meta_path` ... are not on it.
_SYS_SAFE = frozenset({
    "argv", "exit", "stdin", "stdout", "stderr", "platform", "version", "version_info", "maxsize", "byteorder",
    "executable", "stdlib_module_names",
})


class Collected(NamedTuple):
    """The exact text the judge will read, its sha256, and what the text depends on."""

    code: str
    sha256: str
    #: realpaths of the file read (the script): a write to it, or to a directory holding it, changes `code`
    files: tuple
    #: top-level module names the code imports (all on `_IMPORTABLE`)
    imports: tuple
    #: realpath of the directory imports are looked up in first (`sys.path[0]`)
    search_dir: str
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


def _decode(raw: bytes, label: str) -> str:
    """The text the interpreter will parse: PEP 263 decides the encoding, and only utf-8 is read."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    except SyntaxError as exc:
        raise _Refuse(f"{label} declares a source encoding that cannot be read: {exc}") from exc
    try:
        encoding = codecs.lookup(encoding).name
    except LookupError as exc:
        raise _Refuse(f"{label} declares the unknown source encoding {encoding!r}") from exc
    if encoding not in _SOURCE_ENCODINGS:
        raise _Refuse(f"{label} declares the source encoding {encoding!r}: only utf-8 is read")
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise _Refuse(f"{label} is not valid utf-8: {exc}") from exc


def _check_module(name: str, refuse) -> str:
    top = name.split(".")[0]
    if top not in _IMPORTABLE:
        raise refuse(f"imports {name!r}, which is not on the allow-list of standard-library modules")
    return top


def _unsafe_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__") and name not in _DUNDER_SAFE


def _check_code(tree: ast.AST, label: str) -> set:
    """Allow-list over what the code may import and reference; returns the top-level modules it imports.
    Anything else could make the interpreter run text that was not read here."""
    def refuse(what: str) -> "_Refuse":
        return _Refuse(f"{label} {what}")

    imported: set[str] = set()
    sys_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(_check_module(alias.name, refuse))
                if alias.name == "sys":
                    sys_names.add(alias.asname or "sys")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise refuse("uses a relative import")
            module = node.module or ""
            imported.add(_check_module(module, refuse))
            for alias in node.names:
                if alias.name == "*":
                    raise refuse(f"star-imports from {module!r}, which hides the names it binds")
                if alias.name in _DYNAMIC_NAMES or _unsafe_dunder(alias.name) or (alias.name == "sys" and module != "sys") or (
                    module == "sys" and alias.name not in _SYS_SAFE
                ):
                    raise refuse(f"imports {alias.name!r} from {module!r}, which reaches the import machinery or run-time code")
    allowed_sys_uses: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in _DYNAMIC_NAMES or node.attr == "sys" or _unsafe_dunder(node.attr):
                raise refuse(f"references {node.attr!r}, which reaches run-time code or the import machinery")
            if node.attr.startswith("_") and node.attr not in _DUNDER_SAFE:
                raise refuse(f"references the private attribute {node.attr!r}, which can reach a module the judge cannot see")
            if isinstance(node.value, ast.Name) and node.value.id in sys_names:
                if node.attr not in _SYS_SAFE:
                    raise refuse(f"uses sys.{node.attr}, which can change what is imported")
                allowed_sys_uses.add(id(node.value))
        elif isinstance(node, ast.Name) and (node.id in _DYNAMIC_NAMES or _unsafe_dunder(node.id)):
            raise refuse(f"references {node.id!r}, which runs text built at run time")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in sys_names and id(node) not in allowed_sys_uses:
            raise refuse("uses the sys module other than through a plain attribute read")
    return imported


def _is_stdlib_name(entry: str) -> bool:
    """A directory entry the import system would resolve for a standard-library module name."""
    return entry.split(".")[0] in sys.stdlib_module_names


def _shadowing_entry(directory: Path, imported: set, own: "Path | None") -> "str | None":
    """An entry of `directory` (or of its `__pycache__`) named like a standard-library module, so that it
    would be imported instead of the library module -- by the code or by the library's own imports. The
    script's own file is not counted unless the code imports that name."""
    try:
        entries = os.listdir(directory)
        if "__pycache__" in entries:
            entries += os.listdir(directory / "__pycache__")
    except OSError as exc:
        raise _Refuse(f"{directory} cannot be listed: {exc}") from exc
    for entry in sorted(entries):
        if not _is_stdlib_name(entry):
            continue
        if own is not None and entry == own.name and entry.split(".")[0] not in imported:
            continue
        return entry
    return None


def _read_script(script: Path, root: Path, label: str) -> str:
    real = Path(os.path.realpath(script))
    if real != root and root not in real.parents:
        raise _Refuse(f"{script} lies outside the venue")
    try:
        if real.stat().st_size > MAX_TOTAL_BYTES:
            raise _Refuse(f"the code to read exceeds {MAX_TOTAL_BYTES} bytes")
        raw = real.read_bytes()
    except OSError as exc:
        raise _Refuse(f"{script} cannot be read as text: {exc}") from exc
    return _decode(raw, label)


def collect(segment: tool_contracts.Segment) -> "Collected | Undeterminable":
    """The text a judge must read for an opaque `segment`, or why it cannot be read.

    python-c: the -c text. stdin-body: the heredoc / here-string body. script-file: the script's own text
    (no imported file is ever read). Everything outside the allow-lists in the module docstring is
    UNDETERMINABLE."""
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
        script = None
        if source.script is None:
            label = "python -c code" if source.kind == "python-c" else "python stdin code"
            text = _decode(source.code.encode("utf-8"), label)
            search_dir = cwd
        else:
            script = Path(source.script)
            script = Path(os.path.realpath(script if script.is_absolute() else cwd / script))
            label = os.path.relpath(script, cwd)
            text = _read_script(script, root, label)
            search_dir = script.parent
        if len(text.encode("utf-8")) > MAX_TOTAL_BYTES:
            raise _Refuse(f"the code to read exceeds {MAX_TOTAL_BYTES} bytes")
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError) as exc:
            raise _Refuse(f"{label} is not parseable Python, so its imports cannot be listed: {exc}") from exc
        imported = _check_code(tree, label)
        if imported:
            shadow = _shadowing_entry(search_dir, imported, script)
            if shadow is not None:
                raise _Refuse(f"{search_dir} holds {shadow!r}, which would be imported instead of the standard-library module of that name")
    except (_Refuse, segment_resolver.CodeSourceError) as exc:
        return Undeterminable(str(exc))
    except (OSError, UnicodeError) as exc:
        return Undeterminable(f"the code cannot be read: {exc}")
    return Collected(
        text, _sha256(text), (str(script),) if script is not None else (), tuple(sorted(imported)), str(search_dir),
        segment_resolver.head_branch(str(root)))


# --- the rest of the command line, and the code's own writes ---------------------------------------------

def _within(inner: str, outer: str) -> bool:
    """`inner` is `outer` or lies inside it."""
    return inner == outer or inner.startswith(outer.rstrip(os.sep) + os.sep)


def write_hit(path: str, collected: Collected) -> "str | None":
    """What a write to `path` would change in what was read, or None.

    The script (or a directory holding it) rewritten changes `collected.code`. When the code imports
    anything, the search directory itself, or an entry in it named like a standard-library module (or a
    `__pycache__`), changes which file an import resolves to."""
    path = os.path.normpath(path)
    for read in collected.files:
        if _within(read, path):
            return f"the script {read}"
    if collected.imports:
        search = collected.search_dir
        if _within(search, path):
            return f"the directory {search}, in which imports are looked up"
        if _within(path, search):
            first = os.path.relpath(path, search).split(os.sep)[0]
            if first == "__pycache__" or _is_stdlib_name(first):
                return f"{os.path.join(search, first)}, which would be imported instead of a standard-library module"
    return None


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
    included, they run before the interpreter -- may pass `write_hit`."""
    for other in _others(segment, line_segments):
        if other.status != "resolved":
            return f"another segment of the line is {other.status} ({other.text[:60]!r}): what it writes before the code runs is unknown"
    for owner in (segment, *_others(segment, line_segments)):
        for path in _writes(owner):
            hit = write_hit(path, collected)
            if hit is not None:
                return f"the line writes {path}, which is {hit}: the text read here may not be the text that runs"
    return None


def _may_switch_branch(root: str, segment: tool_contracts.Segment, line_segments: Sequence[tool_contracts.Segment]) -> bool:
    """A sibling writes the worktree root or a directory above it (`git checkout`, `git add` ...)."""
    return any(_within(root, path) for other in _others(segment, line_segments) for path in _writes(other))


# --- the cache -------------------------------------------------------------------------------------

def cache_key(code_sha256: str, venue: str, branch: "str | None") -> str:
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
    collect -> line check -> cache -> judge -> validated effects -> own-write check -> typed resources.
    Only an ANSWERED verdict is cached (a timeout or a malformed answer is not an answer and must be asked
    again). The segment's own redirections are not part of the result: they are in `segment.resources`."""
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
    for resource in named:
        if isinstance(resource, resources.FileResource):
            own = write_hit(resource.path, collected)
            if own is not None:
                return Undeterminable(f"the code writes {resource.path}, which is {own}: what it imports or runs changes under it")
    if any(isinstance(r, resources.VcsRefResource) for r in named):
        root = os.path.realpath(segment_resolver.worktree_root(venue) or venue)
        if _may_switch_branch(root, segment, line_segments):
            return Undeterminable("another segment of the line may switch the git branch the code's commit is attributed to")
    return Effects(named, collected.sha256, cached, str(path) if stored and path is not None else None)
