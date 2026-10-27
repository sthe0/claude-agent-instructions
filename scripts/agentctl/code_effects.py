"""Opaque code, read: what the code of an opaque Bash segment changes.

Difficulty removed: `segment_resolver` leaves `python3 -c`, a heredoc fed to `python3 -` and
`python3 script.py` OPAQUE -- no argv parser can say what the code writes -- and those three shapes
were most of the ~700 permission stops of the denial scan. The code text is available to the checker,
so it is read before the call instead of refused (or asked about) after it.

Rule/perception split. The model only PERCEIVES: `advisor.judge_code_effects` is given the code as
quoted data and names the file and git-ref changes it sees. Everything that decides is rule:
  * `collect` decides WHICH text is read -- the code itself plus the local-import closure inside the
    venue -- and refuses (UNDETERMINABLE) any code whose text it cannot read in full;
  * the judge answer is validated against a closed schema;
  * whether each named effect is covered is `resources.covers`, called by the hook, not here.

FAIL-CLOSED end to end: every path that is not "a schema-valid answer about text that was shown in
full" returns `Undeterminable`; there is no path from a judge failure to an empty effect list.

What the judge sees is exactly what the cache key covers: the prompt data is one blob (`Collected.code`)
holding the venue-relative text of every file of the closure and the list of third-party modules, and
the key is the sha256 of that blob plus the venue, the model and the prompt text. Editing an imported
local module therefore changes the key.

Scope, deliberately narrow: Python only (`python3 -c`, a heredoc or here-string body, a script file with
its local imports). A shell body, another interpreter, a body piped from another program, an unquoted
heredoc the shell would expand, a dynamic import or exec, an import that cannot be located, code over
the size caps -- all UNDETERMINABLE.

Injection. The defence is structural: the code is delimited quoted data, the answer must match a closed
schema (an extra field or non-JSON is UNDETERMINABLE) and its effects become typed resources only after
validation. Residual risk, stated plainly: a model that follows an instruction hidden in the code can
still emit a valid but too narrow effects list; nothing here can detect a lie that fits the schema.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from lib import config_root

from . import advisor, resources, segment_resolver, tool_contracts

#: Size caps on what a judge call reads. Over a cap is UNDETERMINABLE, never a truncated read.
MAX_FILES = 8
MAX_TOTAL_BYTES = 32 * 1024

CACHE_DIRNAME = "code-effects-cache"
_PYTHON_RE = re.compile(r"^python[0-9.]*$")
_DYNAMIC_CALL_NAMES = frozenset({"exec", "eval", "compile", "__import__"})
_DYNAMIC_ATTR_NAMES = frozenset({
    "import_module", "run_module", "run_path", "exec_module", "load_module", "spec_from_file_location",
    "load_source", "load_compiled", "load_dynamic",
})
_HEADER_RE = re.compile(r"<<-?[ \t]*(['\"\\]?)")


class Collected(NamedTuple):
    """The exact text the judge will read, and its sha256."""

    code: str
    sha256: str


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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- collect: which text is read ------------------------------------------------------------------

class _Gather:
    """The import closure of one Python entry text, inside `root`."""

    def __init__(self, root: Path, cwd: Path, search_dirs: list[Path]) -> None:
        self.root = root
        self.cwd = cwd
        self.search_dirs = list(search_dirs)
        self.files: dict[Path, str] = {}
        self.third_party: set[str] = set()
        self.total = 0

    def inside(self, path: Path) -> bool:
        real = Path(os.path.realpath(path))
        return real == self.root or self.root in real.parents

    def read(self, path: Path) -> str:
        real = Path(os.path.realpath(path))
        if real in self.files:
            return self.files[real]
        if not self.inside(real):
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

    def visit(self, path: Path | None, text: str, label: str, pending: list) -> None:
        """Parse `text`, refuse dynamic code, queue its imports. `path` is None for the entry text."""
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError) as exc:
            raise _Refuse(f"{label} is not parseable Python, so its imports cannot be listed: {exc}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (isinstance(fn, ast.Name) and fn.id in _DYNAMIC_CALL_NAMES) or (
                    isinstance(fn, ast.Attribute) and fn.attr in _DYNAMIC_ATTR_NAMES
                ):
                    raise _Refuse(f"{label} runs code it builds or loads at run time (exec/eval/compile/__import__/importlib)")
                self._sys_path_edit(node)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    pending.append((alias.name.split("."), [], path, label))
            elif isinstance(node, ast.ImportFrom):
                base = path.parent if path is not None else None
                pending.append(((node.module or "").split(".") if node.module else [], [a.name for a in node.names],
                                path, label, node.level, base))

    def _sys_path_edit(self, call: ast.Call) -> None:
        """`sys.path.insert(i, "lit")` / `sys.path.append("lit")`: a literal directory joins the search path."""
        fn = call.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in ("insert", "append")):
            return
        target = fn.value
        if not (isinstance(target, ast.Attribute) and target.attr == "path"
                and isinstance(target.value, ast.Name) and target.value.id == "sys"):
            return
        arg = call.args[1] if fn.attr == "insert" and len(call.args) == 2 else (
            call.args[0] if fn.attr == "append" and len(call.args) == 1 else None)
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value:
            directory = Path(arg.value) if os.path.isabs(arg.value) else self.cwd / arg.value
            if fn.attr == "insert":
                self.search_dirs.insert(0, directory)
            else:
                self.search_dirs.append(directory)

    def resolve(self, item: tuple, pending: list) -> None:
        parts, names, importer, label = item[:4]
        level = item[4] if len(item) > 4 else 0
        if level:
            base = item[5]
            if base is None:
                raise _Refuse(f"{label} uses a relative import outside a file")
            for _ in range(level - 1):
                base = base.parent
            self._follow(parts, names, [base], pending, label, relative=True)
        else:
            self._follow(parts, names, self.search_dirs, pending, label, relative=False)

    def _follow(self, parts, names, dirs, pending, label, *, relative: bool) -> None:
        if not parts:
            for name in names:
                self._submodule(dirs[0], name, pending, label)
            return
        current = None
        for index, part in enumerate(parts):
            found = self._find(part, [current] if current is not None else dirs)
            if found is None:
                if index == 0 and not relative:
                    self._external(part, label)
                    return
                raise _Refuse(f"{label} imports {'.'.join(parts)!r}, which cannot be located")
            kind, location = found
            if kind == "package":
                self._include(location / "__init__.py", pending)
                current = location
            elif kind == "module":
                self._include(location, pending)
                current = None
                break
            else:
                current = location
        if current is not None:
            for name in names:
                self._submodule(current, name, pending, label)

    def _submodule(self, directory: Path, name: str, pending: list, label: str) -> None:
        found = self._find(name, [directory])
        if found is None:
            return
        kind, location = found
        if kind == "package":
            self._include(location / "__init__.py", pending)
        elif kind == "module":
            self._include(location, pending)

    @staticmethod
    def _find(name: str, dirs) -> "tuple[str, Path] | None":
        for directory in dirs:
            if (directory / name / "__init__.py").is_file():
                return "package", directory / name
            if (directory / f"{name}.py").is_file():
                return "module", directory / f"{name}.py"
            if (directory / name).is_dir():
                return "namespace", directory / name
        return None

    def _include(self, path: Path, pending: list) -> None:
        real = Path(os.path.realpath(path))
        if real in self.files:
            return
        text = self.read(path)
        self.visit(real, text, self.relative(real), pending)

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


class _Refuse(Exception):
    pass


def _gather(entry_label: str, entry_text: str, entry_path: Path | None, root: Path, cwd: Path,
            search_dirs: list[Path]) -> str:
    gather = _Gather(root, cwd, search_dirs)
    if len(entry_text.encode("utf-8")) > MAX_TOTAL_BYTES:
        raise _Refuse(f"the code to read exceeds {MAX_TOTAL_BYTES} bytes")
    gather.total = len(entry_text.encode("utf-8"))
    if entry_path is not None:
        gather.files[Path(os.path.realpath(entry_path))] = entry_text
    pending: list = []
    gather.visit(entry_path, entry_text, entry_label, pending)
    while pending:
        gather.resolve(pending.pop(0), pending)
    sections = [f"=== {entry_label} ===\n{entry_text}"]
    for path, text in gather.files.items():
        if entry_path is not None and path == Path(os.path.realpath(entry_path)):
            continue
        sections.append(f"=== {gather.relative(path)} ===\n{text}")
    third_party = ", ".join(sorted(gather.third_party)) or "none"
    return f"THIRD-PARTY MODULES (text not shown): {third_party}\n" + "\n".join(sections)


def _python_operand(words: list) -> "tuple[str, object] | None":
    """What a python command line runs: ('c', code) | ('script', token) | ('stdin', None)."""
    operands = words[1:]
    i = 0
    while i < len(operands) and operands[i].text.startswith("-") and operands[i].text != "-":
        flag = operands[i].text
        if flag == "--":
            i += 1
            break
        if flag.startswith("--"):
            i += 1 if flag in segment_resolver._PYTHON_VALUE_FLAGS else 0
        else:
            letters = flag[1:]
            for pos, ch in enumerate(letters):
                if ch == "c":
                    if pos + 1 < len(letters):
                        return "c", letters[pos + 1:]
                    return ("c", operands[i + 1].text) if i + 1 < len(operands) else None
                if ch == "m":
                    return None
                if ch in segment_resolver._PYTHON_VALUE_LETTERS:
                    i += 1 if pos == len(letters) - 1 else 0
                    break
        i += 1
    if i >= len(operands) or operands[i].text == "-":
        return "stdin", None
    return "script", operands[i]


def _header_end(text: str) -> int:
    """Index of the newline that ends the command line (heredoc bodies start after it)."""
    position = text.find("\n")
    while position >= 0:
        try:
            segment_resolver._Scanner(text[:position]).run()
            return position
        except segment_resolver._ScanError:
            position = text.find("\n", position + 1)
    return -1


def _stdin_text(raw, text: str) -> str:
    redirect = None
    for red in raw.redirects:
        if red.fd == "0" or (red.fd is None and red.op in segment_resolver._STDIN_OPS):
            redirect = red
    if redirect is None or redirect.target is None:
        raise _Refuse("the interpreter reads its code from a stdin that is not a heredoc or here-string of this segment")
    if sum(1 for r in raw.redirects if r.op in segment_resolver._HEREDOC_OPS or r.op == "<<<") != 1:
        raise _Refuse("more than one heredoc or here-string feeds the segment")
    if redirect.op == "<<<":
        if redirect.target.nonlit:
            raise _Refuse("the here-string holds an expansion: the code that runs is not the text read here")
        return redirect.target.text + "\n"
    if redirect.op not in segment_resolver._HEREDOC_OPS:
        raise _Refuse("stdin comes from a file or pipe, not from text in this segment")
    end = _header_end(text)
    if end < 0:
        raise _Refuse("the heredoc body is not found after the command line")
    header = text[:end]
    marks = _HEADER_RE.findall(header)
    if len(marks) != 1 or header.count("<<") != 1:
        raise _Refuse("the heredoc operator cannot be told apart from other text on the command line")
    delimiter = redirect.target.text
    body, _ = segment_resolver._read_heredoc(text, end + 1, delimiter, redirect.op == "<<-")
    if redirect.op == "<<-":
        body = "\n".join(line.lstrip("\t") for line in body.split("\n"))
    if marks[0] == "" and any(ch in body for ch in "$`\\"):
        raise _Refuse("the heredoc is unquoted and its body holds $, a backtick or a backslash: the shell rewrites it before python reads it")
    return body + "\n"


def collect(segment: tool_contracts.Segment) -> "Collected | Undeterminable":
    """The text a judge must read for an opaque `segment`, or why it cannot be read.

    python-c: the -c text. stdin-body: the heredoc / here-string body. script-file: the script. Each with
    the local-import closure inside the venue (a git worktree root, else the segment's cwd); an import that
    is neither stdlib, installed nor a file in the venue, dynamic code, a non-Python interpreter, a body
    from another program, or any size over the caps is UNDETERMINABLE."""
    if segment.status != "opaque" or not segment.venue:
        return Undeterminable("the segment is not opaque in a known working directory")
    try:
        cwd = Path(os.path.realpath(segment.venue))
        root = Path(segment_resolver._worktree_root(str(cwd)) or cwd)
        root = Path(os.path.realpath(root))
        scan = segment_resolver._Scanner(segment.text).run()
        if len(scan) != 1 or not scan[0].words:
            raise _Refuse("the segment text does not scan as one command")
        raw = scan[0]
        prog = raw.words[0].text
        if not _PYTHON_RE.match(prog):
            raise _Refuse(f"{prog!r} is not a Python interpreter: only Python code is read")
        operand = _python_operand(raw.words)
        if operand is None:
            raise _Refuse("the python command line has no code operand to read")
        kind, value = operand
        if kind == "c":
            if segment.opaque_kind != "python-c":
                raise _Refuse("the segment kind and its command line disagree")
            blob = _gather("python -c code", value, None, root, cwd, [cwd])
        elif kind == "stdin":
            if segment.opaque_kind != "stdin-body":
                raise _Refuse("the segment kind and its command line disagree")
            blob = _gather("python stdin code", _stdin_text(raw, segment.text), None, root, cwd, [cwd])
        else:
            if segment.opaque_kind != "script-file" or value.nonlit:
                raise _Refuse("the script operand is not a literal file name")
            script = Path(value.text)
            script = script if script.is_absolute() else cwd / script
            real = Path(os.path.realpath(script))
            if real != root and root not in real.parents:
                raise _Refuse("the script lies outside the venue")
            if not real.is_file():
                raise _Refuse("the script is not a regular file")
            gather_probe = _Gather(root, cwd, [real.parent])
            blob = _gather(gather_probe.relative(real), gather_probe.read(real), real, root, cwd, [real.parent])
    except _Refuse as exc:
        return Undeterminable(str(exc))
    except (segment_resolver._ScanError, OSError, UnicodeError) as exc:
        return Undeterminable(f"the code cannot be read: {exc}")
    return Collected(blob, _sha256(blob))


# --- the cache -------------------------------------------------------------------------------------

def cache_key(code_sha256: str, venue: str) -> str:
    """One verdict per (code bytes, venue, model, prompt text): any of them changing changes the key."""
    return _sha256("\0".join([advisor.CODE_EFFECTS_PROMPT_SHA256, advisor.CODE_EFFECTS_MODEL, venue, code_sha256]))


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


def _cache_read(path: Path, code_sha256: str, venue: str) -> "advisor.CodeEffectsVerdict | None":
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("code_sha256") != code_sha256 or record.get("venue") != venue:
        return None
    verdict = advisor.validate_code_effects_answer(record.get("answer"))
    return verdict if verdict.answered else None


def _cache_write(path: Path, code_sha256: str, venue: str, answer: dict) -> None:
    record = json.dumps({"code_sha256": code_sha256, "venue": venue, "answer": answer}, sort_keys=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(record)
        os.replace(tmp, path)
    except OSError:
        pass


def _answer_dict(verdict: "advisor.CodeEffectsVerdict") -> dict:
    return {
        "effects": list(verdict.effects or ()), "undeterminable": verdict.effects is None, "reason": verdict.reason,
    }


def _to_resources(effects, venue: str) -> tuple:
    out = []
    for effect in effects:
        if effect["kind"] == "file":
            path = effect["path"]
            out.append(resources.FileResource(path if os.path.isabs(path) or path.startswith("~") else os.path.join(venue, path), effect["op"]))
        else:
            out.append(resources.VcsRefResource(effect["remote"], effect["ref"], effect["op"]))
    return tuple(dict.fromkeys(out))


def effects_of(
    segment: tool_contracts.Segment, *, runner=None, cache_dir: "Path | str | None" = None, timeout: int | None = None,
) -> "Effects | Undeterminable":
    """The typed resources the opaque `segment`'s code changes, or UNDETERMINABLE.

    collect -> cache -> judge -> validated effects -> typed resources. Only an ANSWERED verdict is cached
    (a timeout or a malformed answer is not an answer and must be asked again)."""
    collected = collect(segment)
    if isinstance(collected, Undeterminable):
        return collected
    venue = os.path.realpath(segment.venue)
    directory = _protected_cache_dir(Path(cache_dir) if cache_dir is not None else config_root.agentctl_state_dir() / CACHE_DIRNAME)
    path = directory / f"{cache_key(collected.sha256, venue)}.json" if directory is not None else None
    verdict = _cache_read(path, collected.sha256, venue) if path is not None else None
    cached = verdict is not None
    if verdict is None:
        verdict = advisor.judge_code_effects(collected.code, venue, runner, timeout=timeout)
        if verdict.answered and path is not None:
            _cache_write(path, collected.sha256, venue, _answer_dict(verdict))
    if verdict.effects is None:
        return Undeterminable(verdict.reason)
    return Effects(_to_resources(verdict.effects, venue), collected.sha256, cached, str(path) if cached or (verdict.answered and path) else None)
